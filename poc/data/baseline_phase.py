"""
Baseline phase — generates clean synthetic history, waits for the ingestor
to finish processing, and bootstraps initial baselines in Redis.
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extensions
import redis as redis_module
from confluent_kafka import Producer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.kafka_utils import publish
from data.profiles import generate_snapshot, sample_config_type
from detection.baseline import refresh_baseline
from detection.keys import parse_window_key

logger = logging.getLogger(__name__)

_MINUTES_PER_SNAPSHOT: int = (24 * 60) // config.SNAPSHOTS_PER_IMPLANT_PER_DAY


def generate_baseline_snapshot(
    implant_id: str,
    group_id: str,
    event_time: datetime,
) -> dict:
    """Generate a single backdated clean snapshot for the baseline phase."""
    config_type = sample_config_type()
    snapshot = generate_snapshot(implant_id, group_id, config_type)
    snapshot["metadata"]["received_at"] = event_time.strftime("%d-%m-%Y %H:%M:%S")
    return snapshot


def generate_baseline_event_times(now: datetime) -> list[datetime]:
    """Return all event timestamps for the baseline period."""
    times = []
    for day_offset in range(config.BASELINE_DAYS):
        day_start = now - timedelta(days=config.BASELINE_DAYS - day_offset)
        for snapshot_index in range(config.SNAPSHOTS_PER_IMPLANT_PER_DAY):
            times.append(day_start + timedelta(minutes=snapshot_index * _MINUTES_PER_SNAPSHOT))
    return times


def publish_implant_baseline(
    producer: Producer,
    implant_id: str,
    group_id: str,
    now: datetime,
) -> int:
    """Publish all baseline snapshots for one implant. Returns count published."""
    logger.debug(
        "Generating baseline for implant %s (group %s): %d days x %d snapshots/day",
        implant_id, group_id, config.BASELINE_DAYS, config.SNAPSHOTS_PER_IMPLANT_PER_DAY,
    )
    event_times = generate_baseline_event_times(now)
    for event_time in event_times:
        publish(producer, generate_baseline_snapshot(implant_id, group_id, event_time))
    return len(event_times)


def run_baseline_phase(producer: Producer, implants: list[tuple[str, str]]) -> int:
    """Publish backdated clean snapshots for all implants. Returns total count."""
    logger.info(
        "Baseline phase: %d implants x %d days x %d snapshots/day",
        len(implants), config.BASELINE_DAYS, config.SNAPSHOTS_PER_IMPLANT_PER_DAY,
    )
    now = datetime.now(timezone.utc)
    total_published = 0

    for implant_id, group_id in implants:
        total_published += publish_implant_baseline(producer, implant_id, group_id, now)
        if total_published % config.INGESTOR_LOG_INTERVAL_MESSAGES == 0:
            producer.flush()
            logger.info("Baseline: %d events published", total_published)

    producer.flush()
    logger.info("Baseline phase complete — %d events published", total_published)
    return total_published


def poll_telemetry_count(db_connection: psycopg2.extensions.connection) -> int:
    """Return the current row count in the telemetry table."""
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM telemetry")
        row = cursor.fetchone()
        return row[0] if row else 0


def poll_until_caught_up(
    db_connection: psycopg2.extensions.connection,
    expected_count: int,
    target: int,
    deadline: float,
) -> None:
    """Poll telemetry count until target is reached or deadline expires."""
    last_logged_at = 0.0
    while time.time() < deadline:
        current_count = poll_telemetry_count(db_connection)
        if current_count >= target:
            logger.info("Ingestor caught up — %d rows", current_count)
            return
        now = time.time()
        if now - last_logged_at >= config.INGESTOR_LOG_INTERVAL_SECONDS:
            logger.info(
                "Ingestor progress: %d / %d (%.1f%%)",
                current_count, expected_count,
                100 * current_count / expected_count if expected_count > 0 else 0.0,
            )
            last_logged_at = now
        time.sleep(config.INGESTOR_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Ingestor did not finish within deadline")


def wait_for_ingestor(expected_count: int, timeout_seconds: int | None = None) -> None:
    """Block until PostgreSQL telemetry row count reaches expected_count."""
    if timeout_seconds is None:
        timeout_seconds = config.INGESTOR_WAIT_TIMEOUT_SECONDS
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    target = int(expected_count * config.INGESTOR_COMPLETION_THRESHOLD)
    logger.info("Waiting for ingestor to process %d baseline events...", expected_count)
    try:
        poll_until_caught_up(db_connection, expected_count, target, time.time() + timeout_seconds)
    finally:
        db_connection.close()


def save_baseline_cursor() -> None:
    """Store the current max telemetry ID so the detector knows where live data starts."""
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )
    try:
        with db_connection.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM telemetry")
            row = cursor.fetchone()
            max_id = row[0] if row else 0
        redis_connection.set("detector:baseline_cursor", str(max_id))
        logger.info("Saved baseline cursor to Redis: telemetry id %d", max_id)
    finally:
        db_connection.close()
        redis_connection.close()


def try_refresh_single_baseline(
    redis_connection: redis_module.Redis,
    db_connection: psycopg2.extensions.connection,
    raw_key: bytes,
) -> bool:
    """Attempt to refresh a single baseline from a window key. Returns True on success."""
    parsed = parse_window_key(raw_key)
    if parsed is None:
        logger.warning("Unparseable window key: %r", raw_key)
        return False
    scope, scope_id, config_type = parsed
    try:
        refresh_baseline(redis_connection, db_connection, scope, scope_id, config_type)
        return True
    except Exception:
        logger.error("Bootstrap failed for %s:%s:%s", scope, scope_id, config_type, exc_info=True)
        return False


def bootstrap_baselines() -> None:
    """Refresh group-level baselines only for fast startup.

    Per-implant baselines are trained lazily by the detector on its first
    tick — this cuts bootstrap from ~90 models to ~15, saving over a minute.
    """
    logger.info("Connecting to PostgreSQL and Redis for baseline bootstrap")
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False
    )
    try:
        raw_keys = [
            k for k in redis_connection.scan_iter(match="window:group:*")
        ]
        logger.info("Bootstrapping %d group baselines (per-implant deferred to detector)", len(raw_keys))
        results = [try_refresh_single_baseline(redis_connection, db_connection, k) for k in raw_keys]
        succeeded = sum(results)
        logger.info(
            "Baseline bootstrap complete: %d succeeded, %d failed out of %d keys",
            succeeded, len(results) - succeeded, len(raw_keys),
        )
    finally:
        db_connection.close()
        redis_connection.close()
