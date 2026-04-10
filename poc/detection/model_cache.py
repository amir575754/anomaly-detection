"""
Redis model serialization and caching.

Handles saving/loading trained models (IQR fences + Isolation Forest) to Redis,
and deciding when retraining is needed based on write counters.

NOTE: sklearn IsolationForest objects are serialized with pickle (as specified
in CLAUDE.md) because sklearn models cannot be round-tripped through JSON.
The Redis instance is local and internal — pickle is acceptable in this context.
"""

import json
import logging
import pickle  # required: sklearn models are not JSON-serializable
from datetime import datetime, timezone

import redis as redis_module

import config
from detection.trained_model import TrainedModel

logger = logging.getLogger(__name__)


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


def read_write_counters(
    redis_connection: redis_module.Redis,
    scope: str,
    scope_id: str,
    config_type: str,
) -> tuple[int, int] | None:
    """Read trained-at and current write counters from Redis. Returns None if either is missing."""
    model_key = f"model:{scope}:{scope_id}:{config_type}"
    trained_at_writes: bytes | None = redis_connection.hget(model_key, "trained_at_writes")  # type: ignore[assignment]
    if trained_at_writes is None:
        logger.debug("No model exists for %s — retrain required", model_key)
        return None

    window_writes_key = f"writes:{scope}:{scope_id}:{config_type}"
    current_writes: bytes | None = redis_connection.get(window_writes_key)  # type: ignore[assignment]
    if current_writes is None:
        logger.debug("No write counter for %s — retrain required", window_writes_key)
        return None

    return int(trained_at_writes), int(current_writes)


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
    counters = read_write_counters(redis_connection, scope, scope_id, config_type)
    if counters is None:
        return True

    trained_at, current = counters
    delta = current - trained_at
    needs_retrain = delta >= config.RETRAIN_THRESHOLD
    if needs_retrain:
        model_key = f"model:{scope}:{scope_id}:{config_type}"
        logger.debug(
            "Retrain triggered for %s: %d new writes since last training (threshold=%d)",
            model_key, delta, config.RETRAIN_THRESHOLD,
        )
    return needs_retrain
