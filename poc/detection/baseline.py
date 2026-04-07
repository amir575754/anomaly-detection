"""
Baseline management: computes IQR fences and trains Isolation Forest models
from the Redis sliding windows, then caches results back to Redis and
persists IQR stats to PostgreSQL.

NOTE: sklearn IsolationForest objects are serialized with pickle (as specified
in CLAUDE.md) because sklearn models cannot be round-tripped through JSON.
The Redis instance is local and internal — pickle is acceptable in this context.
"""

import json
import logging
import os
import pickle  # required: sklearn models are not JSON-serializable
import sys
from datetime import datetime, timezone

import numpy as np
import psycopg2.extensions
import redis as redis_module
import shap
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.trained_model import TrainedModel

logger = logging.getLogger(__name__)


def compute_single_feature_fence(
    feature_vectors: list[dict[str, float]],
    feature_name: str,
    iqr_multiplier: float | None = None,
) -> dict:
    """Compute Q1, Q3, IQR, median, and fences for one feature.

    Fences are clamped using robust percentile bounds (P1/P99) so they never
    extend beyond the bulk of the training distribution.  This prevents
    features with narrow ranges (e.g. jitter_percentage 0.10-0.25) from
    getting fences that reach into impossible territory (e.g. -0.035),
    while remaining resistant to outlier contamination in the sliding window.
    """
    multiplier = iqr_multiplier if iqr_multiplier is not None else config.IQR_MULTIPLIER
    values = np.array([vector.get(feature_name, 0.0) for vector in feature_vectors])
    p1, q1, median_value, q3, p99 = np.percentile(values, [1, 25, 50, 75, 99])
    iqr = float(q3 - q1)
    raw_lower = float(q1) - multiplier * iqr
    raw_upper = float(q3) + multiplier * iqr
    return {
        "Q1": float(q1),
        "Q3": float(q3),
        "IQR": iqr,
        "median": float(median_value),
        "lower_fence": max(raw_lower, float(p1)),
        "upper_fence": min(raw_upper, float(p99)),
    }


def compute_iqr_fences(
    feature_vectors: list[dict[str, float]],
    config_type: str | None = None,
) -> dict[str, dict]:
    """Compute per-feature IQR fences, using per-config-type multiplier overrides."""
    if len(feature_vectors) < config.MINIMUM_TRAINING_SAMPLES:
        raise ValueError(
            f"Need at least {config.MINIMUM_TRAINING_SAMPLES} samples; "
            f"got {len(feature_vectors)}"
        )
    iqr_multiplier = config.CONFIG_TYPE_IQR_OVERRIDES.get(config_type) if config_type else None
    feature_names = list(feature_vectors[0].keys())
    logger.debug(
        "Computing IQR fences for %d features across %d samples (multiplier=%s)",
        len(feature_names), len(feature_vectors),
        iqr_multiplier or config.IQR_MULTIPLIER,
    )
    return {
        feature_name: compute_single_feature_fence(
            feature_vectors, feature_name, iqr_multiplier
        )
        for feature_name in feature_names
    }


def filter_low_variance_features(
    feature_vectors: list[dict[str, float]],
    feature_names: list[str],
) -> list[str]:
    """Return only features whose variance exceeds FEATURE_VARIANCE_THRESHOLD."""
    active = []
    for name in feature_names:
        values = [vector.get(name, 0.0) for vector in feature_vectors]
        variance = np.var(values)
        if variance >= config.FEATURE_VARIANCE_THRESHOLD:
            active.append(name)
        else:
            logger.debug("Dropping low-variance feature %r (variance=%.6f)", name, variance)
    logger.debug(
        "Feature filter: %d/%d features retained (threshold=%.4f)",
        len(active), len(feature_names), config.FEATURE_VARIANCE_THRESHOLD,
    )
    return active


def train_isolation_forest(feature_vectors: list[dict[str, float]]) -> TrainedModel:
    """Fit an Isolation Forest and return a TrainedModel with all scoring metadata."""
    if not feature_vectors:
        raise ValueError("Cannot train IsolationForest on zero samples")

    all_feature_names = sorted(feature_vectors[0].keys())
    active_features = filter_low_variance_features(feature_vectors, all_feature_names)

    if not active_features:
        logger.warning("All features dropped by variance filter — using all features")
        active_features = all_feature_names

    raw_matrix = np.array(
        [[vector.get(name, 0.0) for name in active_features] for vector in feature_vectors]
    )
    scaler = StandardScaler()
    matrix = scaler.fit_transform(raw_matrix)

    logger.debug(
        "Training IsolationForest: %d samples x %d features, "
        "n_estimators=%d, contamination=%s",
        matrix.shape[0], matrix.shape[1],
        config.ISOLATION_FOREST_ESTIMATORS,
        config.ISOLATION_FOREST_CONTAMINATION,
    )
    model = IsolationForest(
        n_estimators=config.ISOLATION_FOREST_ESTIMATORS,
        contamination=config.ISOLATION_FOREST_CONTAMINATION,
        random_state=config.ISOLATION_FOREST_RANDOM_STATE,
    )
    model.fit(matrix)

    train_scores = model.decision_function(matrix)
    train_score_mean = float(np.mean(train_scores))
    train_score_std = float(np.std(train_scores))

    logger.debug(
        "IsolationForest trained: active_features=%d, "
        "train_score_mean=%.4f, train_score_std=%.4f",
        len(active_features), train_score_mean, train_score_std,
    )
    return TrainedModel(
        model=model,
        active_features=active_features,
        scaler=scaler,
        train_score_mean=train_score_mean,
        train_score_std=train_score_std,
        shap_explainer=shap.TreeExplainer(model),
    )


def read_window_vectors(
    redis_connection: redis_module.Redis,
    window_key: str,
) -> list[dict[str, float]] | None:
    """Read and parse feature vectors from a Redis window. Returns None if insufficient data."""
    raw_vectors: list[bytes] = redis_connection.lrange(window_key, 0, -1)  # type: ignore[assignment]
    if not raw_vectors:
        logger.warning("Empty window for %r — skipping", window_key)
        return None

    feature_vectors = [json.loads(vector) for vector in raw_vectors]
    if len(feature_vectors) < config.MINIMUM_TRAINING_SAMPLES:
        logger.warning(
            "Window %r has only %d samples (need %d) — skipping",
            window_key, len(feature_vectors), config.MINIMUM_TRAINING_SAMPLES,
        )
        return None
    logger.debug("Read %d vectors from %r", len(feature_vectors), window_key)
    return feature_vectors


def save_model_to_redis(
    redis_connection: redis_module.Redis,
    scope: str,
    scope_id: str,
    config_type: str,
    fences: dict,
    trained_model: TrainedModel,
    sample_count: int,
) -> None:
    """Serialize and cache fences + trained model to the Redis model hash."""
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    writes_key = f"writes:{scope}:{scope_id}:{config_type}"
    current_writes: bytes | None = redis_connection.get(writes_key)  # type: ignore[assignment]
    redis_connection.hset(model_key, mapping={
        "iqr_fences": json.dumps(fences),
        "isolation_forest": pickle.dumps(trained_model),  # noqa: S301 — internal Redis, see module docstring
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_sample_size": str(sample_count),
        "trained_at_writes": str(current_writes.decode() if current_writes else 0),
    })
    logger.debug(
        "Saved model to Redis: %s (sample_count=%d, writes=%s)",
        model_key, sample_count, current_writes,
    )


def persist_baseline_to_postgresql(
    db_connection: psycopg2.extensions.connection,
    scope: str,
    scope_id: str,
    config_type: str,
    fences: dict,
) -> None:
    """Upsert IQR fence stats to the baselines table."""
    with db_connection:
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO baselines (scope, scope_id, config_type, updated_at, stats)
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (scope, scope_id, config_type)
                DO UPDATE SET updated_at = EXCLUDED.updated_at, stats = EXCLUDED.stats
                """,
                (scope, scope_id, config_type, json.dumps(fences)),
            )
    logger.debug("Persisted baseline stats to PostgreSQL: %s:%s:%s", scope, scope_id, config_type)


def slice_window_for_scope(
    feature_vectors: list[dict[str, float]],
    scope: str,
) -> list[dict[str, float]]:
    """Trim the feature vector list to the appropriate scope-specific window size."""
    if scope == "implant":
        max_size = config.BASELINE_WINDOW_SIZE_IMPLANT
    else:
        max_size = config.BASELINE_WINDOW_SIZE_GROUP

    if len(feature_vectors) > max_size:
        logger.debug(
            "Slicing %s window from %d to %d vectors",
            scope, len(feature_vectors), max_size,
        )
        return feature_vectors[-max_size:]
    return feature_vectors


def refresh_baseline(
    redis_connection: redis_module.Redis,
    db_connection: psycopg2.extensions.connection,
    scope: str,
    scope_id: str,
    config_type: str,
) -> None:
    """Retrain IQR fences and Isolation Forest from the Redis window."""
    window_key = f"window:{scope}:{scope_id}:{config_type}"
    logger.debug("Refreshing baseline for %s", window_key)
    feature_vectors = read_window_vectors(redis_connection, window_key)
    if feature_vectors is None:
        return

    feature_vectors = slice_window_for_scope(feature_vectors, scope)
    fences = compute_iqr_fences(feature_vectors, config_type)
    trained_model = train_isolation_forest(feature_vectors)

    save_model_to_redis(
        redis_connection, scope, scope_id, config_type,
        fences, trained_model, len(feature_vectors),
    )
    persist_baseline_to_postgresql(db_connection, scope, scope_id, config_type, fences)
    logger.info("Refreshed %s:%s:%s (%d samples)", scope, scope_id, config_type, len(feature_vectors))


def load_model_from_redis(
    redis_connection: redis_module.Redis,
    scope: str,
    scope_id: str,
    config_type: str,
) -> dict | None:
    """
    Load cached IQR fences and TrainedModel from Redis.
    Returns None if the model has not been trained yet.
    """
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    raw_hash: dict[bytes, bytes] = redis_connection.hgetall(model_key)  # type: ignore[assignment]
    if not raw_hash:
        logger.debug("No cached model found for %s", model_key)
        return None

    trained_model: TrainedModel = pickle.loads(raw_hash[b"isolation_forest"])  # noqa: S301 — internal Redis, see module docstring
    logger.debug("Loaded model from Redis: %s", model_key)
    return {
        "iqr_fences": json.loads(raw_hash[b"iqr_fences"]),
        "trained_model": trained_model,
    }


def should_retrain(
    redis_connection: redis_module.Redis,
    scope: str,
    scope_id: str,
    config_type: str,
) -> bool:
    """
    Return True if no model exists or enough new data has arrived since the
    last training to justify retraining.

    Uses a monotonic write counter (writes:{scope}:{scope_id}:{config_type})
    that the ingestor increments on every rpush. This counter never resets
    when ltrim caps the window, so it reliably tracks new arrivals even at
    steady state.
    """
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    trained_at_writes: bytes | None = redis_connection.hget(model_key, "trained_at_writes")  # type: ignore[assignment]

    if trained_at_writes is None:
        logger.debug("No model exists for %s — retrain required", model_key)
        return True

    window_writes_key = f"writes:{scope}:{scope_id}:{config_type}"
    current_writes: bytes | None = redis_connection.get(window_writes_key)  # type: ignore[assignment]
    if current_writes is None:
        logger.debug("No write counter for %s — retrain required", window_writes_key)
        return True

    delta = int(current_writes) - int(trained_at_writes)
    needs_retrain = delta >= config.RETRAIN_THRESHOLD
    if needs_retrain:
        logger.debug(
            "Retrain triggered for %s: %d new writes since last training (threshold=%d)",
            model_key, delta, config.RETRAIN_THRESHOLD,
        )
    return needs_retrain
