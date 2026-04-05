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
from sklearn.ensemble import IsolationForest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

logger = logging.getLogger(__name__)


def compute_single_feature_fence(
    feature_vectors: list[dict[str, float]],
    feature_name: str,
) -> dict:
    """Compute Q1, Q3, IQR, median, and fences for one feature."""
    values = np.array([vector.get(feature_name, 0.0) for vector in feature_vectors])
    q1, median_value, q3 = np.percentile(values, [25, 50, 75])
    iqr = float(q3 - q1)
    return {
        "Q1": float(q1),
        "Q3": float(q3),
        "IQR": iqr,
        "median": float(median_value),
        "lower_fence": float(q1) - config.IQR_MULTIPLIER * iqr,
        "upper_fence": float(q3) + config.IQR_MULTIPLIER * iqr,
    }


def compute_iqr_fences(feature_vectors: list[dict[str, float]]) -> dict[str, dict]:
    """Compute per-feature IQR fences from a list of feature vectors."""
    if len(feature_vectors) < config.MINIMUM_TRAINING_SAMPLES:
        raise ValueError(
            f"Need at least {config.MINIMUM_TRAINING_SAMPLES} samples; "
            f"got {len(feature_vectors)}"
        )
    feature_names = list(feature_vectors[0].keys())
    logger.debug("Computing IQR fences for %d features across %d samples",
                 len(feature_names), len(feature_vectors))
    return {
        feature_name: compute_single_feature_fence(feature_vectors, feature_name)
        for feature_name in feature_names
    }


def train_isolation_forest(feature_vectors: list[dict[str, float]]) -> IsolationForest:
    """
    Fit and return an IsolationForest on the full feature matrix.

    Attaches a `feature_column_order` attribute to the trained model so the
    scorer can reconstruct the vector in the same column order at scoring time.
    """
    if not feature_vectors:
        raise ValueError("Cannot train IsolationForest on zero samples")

    feature_names = sorted(feature_vectors[0].keys())
    matrix = np.array(
        [[vector.get(name, 0.0) for name in feature_names] for vector in feature_vectors]
    )
    logger.debug(
        "Training IsolationForest: %d samples x %d features, "
        "n_estimators=%d, contamination=%.3f",
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
    model.feature_column_order = feature_names  # type: ignore[attr-defined]
    logger.debug("IsolationForest training complete")
    return model


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
    model: IsolationForest,
    sample_count: int,
) -> None:
    """Serialize and cache fences + model to the Redis model hash."""
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    writes_key = f"writes:{scope}:{scope_id}:{config_type}"
    current_writes: bytes | None = redis_connection.get(writes_key)  # type: ignore[assignment]
    redis_connection.hset(model_key, mapping={
        "iqr_fences": json.dumps(fences),
        "isolation_forest": pickle.dumps(model),  # noqa: S301 — internal Redis, see module docstring
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

    fences = compute_iqr_fences(feature_vectors)
    model = train_isolation_forest(feature_vectors)

    save_model_to_redis(
        redis_connection, scope, scope_id, config_type,
        fences, model, len(feature_vectors),
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
    Load cached IQR fences and IsolationForest from Redis.
    Returns None if the model has not been trained yet.
    """
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    raw_hash: dict[bytes, bytes] = redis_connection.hgetall(model_key)  # type: ignore[assignment]
    if not raw_hash:
        logger.debug("No cached model found for %s", model_key)
        return None

    logger.debug("Loaded model from Redis: %s", model_key)
    return {
        "iqr_fences": json.loads(raw_hash[b"iqr_fences"]),
        "isolation_forest": pickle.loads(raw_hash[b"isolation_forest"]),  # noqa: S301 — internal Redis, see module docstring
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
