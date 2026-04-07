"""
Detection engine. Runs in a loop: retrains stale baselines, fetches unscored
telemetry from PostgreSQL, scores each event against IQR fences and Isolation
Forest, and prints detected anomalies to the CLI.

Uses cursor-based fetching (last processed telemetry ID) instead of wall-clock
windows so that bursts of data during the demo's fast-forward mode are never
missed.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.baseline import load_model_from_redis, refresh_baseline, should_retrain
from detection.keys import parse_window_key
from detection.models import ScoredEvent, Severity
from detection.scoring import (
    determine_severity,
    explain_isolation_forest,
    score_with_iqr,
    score_with_isolation_forest,
)

logger = logging.getLogger(__name__)


def discover_window_keys(redis_connection: redis_module.Redis) -> list[tuple[str, str, str]]:
    """Return all (scope, scope_id, config_type) tuples that have window data."""
    results = []
    for raw_key in redis_connection.scan_iter(match="window:*"):
        parsed = parse_window_key(raw_key)
        if parsed is not None:
            results.append(parsed)
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
    trained_model = cached_model["trained_model"]
    isolation_forest_score = score_with_isolation_forest(features, trained_model)

    shap_contributions = []
    if isolation_forest_score >= config.ISOLATION_FOREST_MEDIUM_THRESHOLD:
        shap_contributions = explain_isolation_forest(features, trained_model)

    if deviations or shap_contributions:
        logger.debug(
            "Telemetry id=%s implant=%s: %d IQR deviations, IF score=%.3f, %d SHAP features",
            row.get("id"), implant_id, len(deviations), isolation_forest_score,
            len(shap_contributions),
        )

    return ScoredEvent(
        telemetry_row=row,  # type: ignore[arg-type]  # RealDictCursor row is structurally compatible
        deviating_features=deviations,
        isolation_forest_score=isolation_forest_score,
        baseline_used=scope,
        shap_contributions=shap_contributions,
    )


def retrain_stale_baselines(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
) -> None:
    """Check all window keys and retrain any baselines that need refreshing."""
    window_keys = discover_window_keys(redis_connection)
    retrained = 0
    failures: list[tuple[str, str, str, Exception]] = []
    for scope, scope_id, config_type in window_keys:
        if should_retrain(redis_connection, scope, scope_id, config_type):
            try:
                refresh_baseline(
                    redis_connection, db_connection, scope, scope_id, config_type
                )
                retrained += 1
            except Exception as exception:
                logger.error(
                    "Baseline refresh failed for %s:%s:%s", scope, scope_id, config_type,
                    exc_info=True,
                )
                failures.append((scope, scope_id, config_type, exception))
    if retrained > 0:
        logger.info("Retrained %d / %d baselines this tick", retrained, len(window_keys))
    if failures:
        logger.warning(
            "%d baseline refresh(es) failed: %s",
            len(failures),
            ", ".join(f"{s}:{sid}:{ct}" for s, sid, ct, _ in failures),
        )


def score_all_rows(
    telemetry_rows: list[dict],
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> list[ScoredEvent]:
    """Score each telemetry row, returning only successfully scored events."""
    scored_events = []
    failures: list[tuple[int | None, Exception]] = []
    for row in telemetry_rows:
        try:
            scored = score_row_with_cache(row, scope_map, model_cache)
            if scored is not None:
                scored_events.append(scored)
        except Exception as exception:
            logger.error("Scoring failed for row id=%s", row.get("id"), exc_info=True)
            failures.append((row.get("id"), exception))
    if failures:
        logger.warning(
            "%d row(s) failed scoring: %s",
            len(failures),
            ", ".join(str(row_id) for row_id, _ in failures),
        )
    return scored_events


SEVERITY_COLORS = {
    Severity.HIGH: "\033[91m",    # red
    Severity.MEDIUM: "\033[93m",  # yellow
    Severity.LOW: "\033[96m",     # cyan
}
RESET_COLOR = "\033[0m"
DIM = "\033[2m"


def format_detection(event: ScoredEvent, severity: Severity) -> str:
    """Format a scored event as a human-readable CLI detection line."""
    row = event.telemetry_row
    color = SEVERITY_COLORS.get(severity, "")
    lines = [
        f"{color}[{severity.value}]{RESET_COLOR} "
        f"{row['implant_id']} ({row['group_id']}) {row['config_type']}"
    ]
    for deviation in event.deviating_features:
        lines.append(
            f"  ├─ IQR: {deviation.feature_name} = {deviation.observed_value:.4g} "
            f"{DIM}(expected {deviation.lower_fence:.4g}–{deviation.upper_fence:.4g}, "
            f"median {deviation.expected_median:.4g}){RESET_COLOR}"
        )
    if event.shap_contributions and not event.deviating_features:
        shap_parts = ", ".join(
            f"{c.feature_name} ({c.contribution:+.3f})"
            for c in event.shap_contributions
        )
        lines.append(f"  ├─ SHAP: {shap_parts}")
    lines.append(f"  └─ IF score: {event.isolation_forest_score:.3f}")
    return "\n".join(lines)


def print_detections(scored_events: list[ScoredEvent]) -> int:
    """Determine severity and print detected anomalies. Returns count printed.

    Prints HIGH (IQR + IF agree), MEDIUM only when IQR flagged something,
    and LOW (IF-only with high score). Skips MEDIUM events with no IQR
    evidence to keep the CLI readable without deduplication.
    """
    printed = 0
    suppressed = 0
    for event in scored_events:
        severity = determine_severity(event.deviating_features, event.isolation_forest_score)
        if severity is None:
            continue
        if severity == Severity.MEDIUM and not event.deviating_features:
            suppressed += 1
            continue
        print(format_detection(event, severity))
        printed += 1
    if suppressed:
        logger.debug("Suppressed %d IF-only MEDIUM events (no IQR evidence)", suppressed)
    return printed


def score_telemetry_batch(
    redis_connection: redis_module.Redis,
    telemetry_rows: list[dict],
    implant_registry: dict[str, dict],
) -> None:
    """Score a batch of telemetry rows and print detected anomalies."""
    model_cache, scope_map = build_model_cache(
        redis_connection, telemetry_rows, implant_registry,
    )
    scored_events = score_all_rows(telemetry_rows, scope_map, model_cache)
    detected = print_detections(scored_events)
    logger.info(
        "Scored %d rows, %d detections printed",
        len(telemetry_rows), detected,
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

    implant_registry = fetch_all_implants(db_connection)
    score_telemetry_batch(redis_connection, telemetry_rows, implant_registry)
    new_cursor = max(row["id"] for row in telemetry_rows)
    has_more = len(telemetry_rows) >= config.MAX_ROWS_PER_DETECTION_TICK
    logger.info("Advanced cursor to telemetry id %d (has_more=%s)", new_cursor, has_more)
    return new_cursor, has_more


MAX_CONSECUTIVE_FAILURES = 5


def run() -> None:
    logger.info("Connecting to PostgreSQL and Redis")
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )

    try:
        baseline_cursor = redis_connection.get("detector:baseline_cursor")
        while baseline_cursor is None:
            logger.info("Waiting for baseline phase to complete (no cursor in Redis yet)...")
            time.sleep(config.DETECTION_IDLE_SLEEP_SECONDS)
            baseline_cursor = redis_connection.get("detector:baseline_cursor")

        last_processed_id = int(baseline_cursor)
        logger.info("Baseline cursor found — starting detection from telemetry id %d", last_processed_id)

        tick_number = 0
        consecutive_failures = 0
        while True:
            tick_number += 1
            tick_start = time.monotonic()
            logger.info("=== Tick #%d ===", tick_number)
            has_more = False
            try:
                last_processed_id, has_more = run_single_tick(
                    db_connection, redis_connection, last_processed_id,
                )
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.critical("Tick #%d failed (%d consecutive)", tick_number, consecutive_failures, exc_info=True)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Aborting after {MAX_CONSECUTIVE_FAILURES} consecutive tick failures"
                    )
            logger.info("Tick #%d done in %.1fs", tick_number, time.monotonic() - tick_start)
            if has_more:
                logger.info("Backlog detected — running next tick immediately")
            else:
                time.sleep(config.DETECTION_IDLE_SLEEP_SECONDS)
    finally:
        db_connection.close()
        redis_connection.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    run()
