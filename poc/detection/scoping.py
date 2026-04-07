"""
Baseline scope resolution. Determines whether each telemetry row should be
scored against a per-implant or per-group baseline, and pre-loads the
corresponding model artefacts from Redis.
"""

import logging
import os
import sys
from datetime import datetime, timezone

import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.model_cache import load_model_from_redis

logger = logging.getLogger(__name__)


def resolve_baseline_scope(
    redis_connection: redis_module.Redis,
    implant_id: str,
    group_id: str,
    first_seen: datetime,
    config_type: str,
    now: datetime,
) -> tuple[str, str]:
    """
    Determine whether to use the per-implant or per-group baseline.
    Returns (scope, scope_id).
    """
    days_since_first_seen = (now - first_seen).total_seconds() / config.SECONDS_PER_DAY

    if days_since_first_seen >= config.IMPLANT_BASELINE_MIN_DAYS:
        implant_model_key = f"model:implant:{implant_id}:{config_type}"
        if redis_connection.exists(implant_model_key):
            logger.debug(
                "Implant %s has %.1f days of data — using per-implant baseline",
                implant_id, days_since_first_seen,
            )
            return "implant", implant_id

    logger.debug(
        "Implant %s has %.1f days (need %d) — falling back to group %s baseline",
        implant_id, days_since_first_seen, config.IMPLANT_BASELINE_MIN_DAYS, group_id,
    )
    return "group", group_id


def resolve_scope_for_row(
    redis_connection: redis_module.Redis,
    row: dict,
    implant_registry: dict[str, dict],
    now: datetime,
    scope_map: dict[tuple[str, str], tuple[str, str]],
    needed: set[tuple[str, str, str]],
) -> str | None:
    """Resolve and record the baseline scope for a single row. Returns 'excluded', 'unknown', or None on success."""
    config_type = row["config_type"]
    if config_type in config.DETECTION_EXCLUDED_CONFIG_TYPES:
        return "excluded"
    implant = implant_registry.get(row["implant_id"])
    if implant is None:
        return "unknown"
    scope, scope_id = resolve_baseline_scope(
        redis_connection, row["implant_id"], implant["group_id"],
        implant["first_seen"], config_type, now,
    )
    scope_map[(row["implant_id"], config_type)] = (scope, scope_id)
    needed.add((scope, scope_id, config_type))
    return None


def resolve_scopes_for_batch(
    redis_connection: redis_module.Redis,
    telemetry_rows: list[dict],
    implant_registry: dict[str, dict],
) -> tuple[dict[tuple, tuple], set[tuple], int, int]:
    """Map each (implant, config_type) to its baseline scope. Returns (scope_map, needed_models, skipped_excluded, skipped_unknown)."""
    now = datetime.now(timezone.utc)
    needed: set[tuple[str, str, str]] = set()
    scope_map: dict[tuple[str, str], tuple[str, str]] = {}
    skipped_excluded = 0
    skipped_unknown = 0

    for row in telemetry_rows:
        result = resolve_scope_for_row(redis_connection, row, implant_registry, now, scope_map, needed)
        if result == "excluded":
            skipped_excluded += 1
        elif result == "unknown":
            skipped_unknown += 1

    return scope_map, needed, skipped_excluded, skipped_unknown


def load_needed_models(
    redis_connection: redis_module.Redis,
    needed: set[tuple],
) -> dict[tuple, dict | None]:
    """Load all required models from Redis into a cache dict."""
    cache: dict[tuple, dict | None] = {}
    for scope, scope_id, config_type in needed:
        cache[(scope, scope_id, config_type)] = load_model_from_redis(
            redis_connection, scope, scope_id, config_type,
        )
    loaded = sum(1 for model in cache.values() if model is not None)
    logger.info("Loaded %d / %d models from Redis", loaded, len(needed))
    return cache


def build_model_cache(
    redis_connection: redis_module.Redis,
    telemetry_rows: list[dict],
    implant_registry: dict[str, dict],
) -> tuple[dict[tuple, dict | None], dict[tuple, tuple]]:
    """Pre-load every model hash needed to score this batch."""
    scope_map, needed, skipped_excluded, skipped_unknown = resolve_scopes_for_batch(
        redis_connection, telemetry_rows, implant_registry,
    )
    model_cache = load_needed_models(redis_connection, needed)
    logger.info(
        "Model cache built: %d excluded, %d unknown implant",
        skipped_excluded, skipped_unknown,
    )
    return model_cache, scope_map
