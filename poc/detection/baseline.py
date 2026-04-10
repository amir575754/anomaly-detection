"""
Baseline management: orchestration layer.

Reads sliding windows from Redis, delegates fence computation and model
training to detection.fences and detection.training, caches results via
detection.model_cache, and persists IQR stats to PostgreSQL.
"""

import json
import logging
import os
import sys

import psycopg2.extensions
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.fences import compute_iqr_fences
from detection.model_cache import save_model_to_redis
from detection.training import train_isolation_forest

logger = logging.getLogger(__name__)


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
