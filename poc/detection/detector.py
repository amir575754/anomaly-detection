"""
Detection engine. Runs in a loop: retrains stale baselines, fetches unscored
telemetry from PostgreSQL, scores each event against IQR fences and Isolation
Forest, then passes ScoredEvents to the alert engine.

Uses cursor-based fetching (last processed telemetry ID) instead of wall-clock
windows so that bursts of data during the demo's fast-forward mode are never
missed.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import psycopg2
import psycopg2.extensions
import psycopg2.extras
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from alerts.engine import process_scored_events
from alerts.models import FeatureDeviation, ScoredEvent
from detection.baseline import load_model_from_redis, refresh_baseline, should_retrain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def discover_window_keys(redis_connection: redis_module.Redis) -> list[tuple[str, str, str]]:
    """Return all (scope, scope_id, config_type) tuples that have window data."""
    results = []
    for raw_key in redis_connection.scan_iter(match="window:*"):
        parts = raw_key.decode("utf-8").split(":", 3)
        if len(parts) == 4:
            _, scope, scope_id, config_type = parts
            results.append((scope, scope_id, config_type))
    logger.debug("Discovered %d window keys in Redis", len(results))
    return results


def fetch_all_implants(
    db_connection: psycopg2.extensions.connection,
) -> dict[str, dict]:
    """Fetch every implant's group_id and first_seen in one query."""
    with db_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute("SELECT implant_id, group_id, first_seen FROM implants")
        registry = {row["implant_id"]: dict(row) for row in cursor.fetchall()}
    logger.debug("Loaded %d implants from registry", len(registry))
    return registry


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
        config_type = row["config_type"]
        if config_type in config.DETECTION_EXCLUDED_CONFIG_TYPES:
            skipped_excluded += 1
            continue
        implant = implant_registry.get(row["implant_id"])
        if implant is None:
            skipped_unknown += 1
            continue
        scope, scope_id = resolve_baseline_scope(
            redis_connection, row["implant_id"], implant["group_id"],
            implant["first_seen"], config_type, now,
        )
        scope_map[(row["implant_id"], config_type)] = (scope, scope_id)
        needed.add((scope, scope_id, config_type))

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


def compute_iqr_deviation(observed_value: float, fence_data: dict) -> float | None:
    """Return the IQR deviation in fence units, or None if the value is within bounds."""
    lower_fence = fence_data["lower_fence"]
    upper_fence = fence_data["upper_fence"]

    if lower_fence <= observed_value <= upper_fence:
        return None

    iqr = fence_data["IQR"]
    if iqr <= 0:
        return config.ZERO_IQR_DEVIATION_SENTINEL

    distance_outside = (
        lower_fence - observed_value
        if observed_value < lower_fence
        else observed_value - upper_fence
    )
    return distance_outside / iqr


def score_with_iqr(features: dict[str, float], fences: dict) -> list[FeatureDeviation]:
    """Check each feature against IQR fences; return the list of deviations."""
    deviations = []
    for feature_name, observed_value in features.items():
        if feature_name not in fences:
            continue
        iqr_units = compute_iqr_deviation(observed_value, fences[feature_name])
        if iqr_units is not None:
            deviations.append(FeatureDeviation(
                feature_name=feature_name,
                observed_value=observed_value,
                expected_median=fences[feature_name]["median"],
                lower_fence=fences[feature_name]["lower_fence"],
                upper_fence=fences[feature_name]["upper_fence"],
                iqr_multiplier=iqr_units,
            ))
    return deviations


def score_with_isolation_forest(features: dict[str, float], model) -> float:
    """
    Return a normalised anomaly score in [0, 1] where 1 is most anomalous.

    Uses z-score normalization against the training data's score distribution,
    mapped through a sigmoid. Normal points cluster around 0.5; genuine
    anomalies that are multiple standard deviations from the training mean
    produce scores approaching 1.0.
    """
    active_features = model.active_features
    vector = np.array([[features.get(name, 0.0) for name in active_features]])
    raw_score = model.decision_function(vector)[0]

    standard_deviation = max(model.train_score_std, 1e-6)
    z_score = (model.train_score_mean - raw_score) / standard_deviation
    sigmoid = 1.0 / (1.0 + np.exp(-z_score))
    return float(max(0.0, min(1.0, sigmoid)))


def fetch_unscored_telemetry(
    db_connection: psycopg2.extensions.connection,
    last_processed_id: int,
) -> list[dict]:
    """
    Fetch telemetry rows that haven't been scored yet, identified by their
    auto-incrementing ID being greater than last_processed_id.
    """
    with db_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT t.*, i.group_id
            FROM telemetry t
            JOIN implants i ON t.implant_id = i.implant_id
            WHERE t.id > %s
            ORDER BY t.id
            LIMIT %s
            """,
            (last_processed_id, config.MAX_ROWS_PER_DETECTION_TICK),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    logger.debug(
        "Fetched %d unscored telemetry rows (after id %d, limit %d)",
        len(rows), last_processed_id, config.MAX_ROWS_PER_DETECTION_TICK,
    )
    return rows


def get_max_telemetry_id(db_connection: psycopg2.extensions.connection) -> int:
    """Return the current maximum telemetry ID, or 0 if the table is empty."""
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM telemetry")
        row = cursor.fetchone()
        return row[0] if row else 0


def score_row_with_cache(
    row: dict,
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> ScoredEvent | None:
    """Score one telemetry row using pre-loaded scope map and model cache."""
    implant_id = row["implant_id"]
    config_type = row["config_type"]
    features = (
        row["features"] if isinstance(row["features"], dict)
        else json.loads(row["features"])
    )

    scope_entry = scope_map.get((implant_id, config_type))
    if scope_entry is None:
        return None
    scope, scope_id = scope_entry

    cached_model = model_cache.get((scope, scope_id, config_type))
    if cached_model is None:
        return None

    deviations = score_with_iqr(features, cached_model["iqr_fences"])
    isolation_forest_score = score_with_isolation_forest(
        features, cached_model["isolation_forest"]
    )

    if deviations:
        logger.debug(
            "Telemetry id=%s implant=%s: %d IQR deviations, IF score=%.3f",
            row.get("id"), implant_id, len(deviations), isolation_forest_score,
        )

    return ScoredEvent(
        telemetry_row=row,  # type: ignore[arg-type]  # RealDictCursor row is structurally compatible
        deviating_features=deviations,
        isolation_forest_score=isolation_forest_score,
        baseline_used=scope,
    )


def retrain_stale_baselines(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
) -> None:
    """Check all window keys and retrain any baselines that need refreshing."""
    window_keys = discover_window_keys(redis_connection)
    retrained = 0
    for scope, scope_id, config_type in window_keys:
        if should_retrain(redis_connection, scope, scope_id, config_type):
            try:
                refresh_baseline(
                    redis_connection, db_connection, scope, scope_id, config_type
                )
                retrained += 1
            except Exception:
                logger.error(
                    "Baseline refresh failed for %s:%s:%s", scope, scope_id, config_type,
                    exc_info=True,
                )
    if retrained > 0:
        logger.info("Retrained %d / %d baselines this tick", retrained, len(window_keys))


def score_all_rows(
    telemetry_rows: list[dict],
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> list[ScoredEvent]:
    """Score each telemetry row, returning only successfully scored events."""
    scored_events = []
    for row in telemetry_rows:
        try:
            scored = score_row_with_cache(row, scope_map, model_cache)
            if scored is not None:
                scored_events.append(scored)
        except Exception:
            logger.error("Scoring failed for row id=%s", row.get("id"), exc_info=True)
    return scored_events


def score_telemetry_batch(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    telemetry_rows: list[dict],
) -> None:
    """Score a batch of telemetry rows and pass results to the alert engine."""
    implant_registry = fetch_all_implants(db_connection)
    model_cache, scope_map = build_model_cache(
        redis_connection, telemetry_rows, implant_registry,
    )
    scored_events = score_all_rows(telemetry_rows, scope_map, model_cache)
    anomalous = sum(1 for event in scored_events if event.deviating_features)
    logger.info("Scored %d / %d rows (%d with deviations)",
                len(scored_events), len(telemetry_rows), anomalous)

    now = datetime.now(timezone.utc)
    process_scored_events(
        db_connection, scored_events,
        now - timedelta(minutes=config.DETECTION_WINDOW_MINUTES), now,
    )


def run_single_tick(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    last_processed_id: int,
) -> tuple[int, bool]:
    """Execute one detection tick. Returns (updated last_processed_id, has_more_rows)."""
    retrain_stale_baselines(db_connection, redis_connection)
    telemetry_rows = fetch_unscored_telemetry(db_connection, last_processed_id)
    if not telemetry_rows:
        logger.info("No new telemetry since id %d", last_processed_id)
        return last_processed_id, False

    score_telemetry_batch(db_connection, redis_connection, telemetry_rows)
    new_cursor = max(row["id"] for row in telemetry_rows)
    has_more = len(telemetry_rows) >= config.MAX_ROWS_PER_DETECTION_TICK
    logger.info("Advanced cursor to telemetry id %d (has_more=%s)", new_cursor, has_more)
    return new_cursor, has_more


def run() -> None:
    logger.info("Connecting to PostgreSQL and Redis")
    db_connection = psycopg2.connect(config.DB_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )

    baseline_cursor = redis_connection.get("detector:baseline_cursor")
    if baseline_cursor is not None:
        last_processed_id = int(baseline_cursor)
        logger.info("Resuming from baseline cursor stored in Redis: %d", last_processed_id)
    else:
        last_processed_id = get_max_telemetry_id(db_connection)
    logger.info("Detector ready — starting from telemetry id %d", last_processed_id)

    tick_number = 0
    while True:
        tick_number += 1
        tick_start = time.monotonic()
        logger.info("=== Tick #%d ===", tick_number)
        has_more = False
        try:
            last_processed_id, has_more = run_single_tick(
                db_connection, redis_connection, last_processed_id,
            )
        except Exception:
            logger.critical("Tick #%d failed", tick_number, exc_info=True)
        logger.info("Tick #%d done in %.1fs", tick_number, time.monotonic() - tick_start)
        if has_more:
            logger.info("Backlog detected — running next tick immediately")
        else:
            time.sleep(config.DETECTION_IDLE_SLEEP_SECONDS)


if __name__ == "__main__":
    run()
