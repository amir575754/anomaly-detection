"""
Detection engine. Runs in a loop: retrains stale baselines, fetches unscored
telemetry from PostgreSQL, scores each event against IQR fences and Isolation
Forest, and prints detected anomalies to the CLI.

Uses cursor-based fetching (last processed telemetry ID) instead of wall-clock
windows so that bursts of data during the demo's fast-forward mode are never
missed.
"""

import logging
import os
import sys
import time

import psycopg2
import psycopg2.extensions
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.queries import fetch_all_implants, fetch_unscored_telemetry
from detection.scoring_pipeline import retrain_stale_baselines, score_telemetry_batch

logger = logging.getLogger(__name__)


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


def wait_for_baseline_cursor(redis_connection: redis_module.Redis) -> int:
    """Poll Redis until the baseline cursor exists. Returns last_processed_id."""
    baseline_cursor = redis_connection.get("detector:baseline_cursor")
    logged_once = False
    while baseline_cursor is None:
        if not logged_once:
            logger.info("Waiting for baseline phase to complete...")
            logged_once = True
        time.sleep(1)
        baseline_cursor = redis_connection.get("detector:baseline_cursor")
    last_processed_id = int(baseline_cursor)
    logger.info("Baseline cursor found — starting detection from telemetry id %d", last_processed_id)
    return last_processed_id


def connect_infrastructure() -> tuple[psycopg2.extensions.connection, redis_module.Redis]:
    """Create and return database and Redis connections."""
    logger.info("Connecting to PostgreSQL and Redis")
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )
    return db_connection, redis_connection


def execute_tick(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    last_processed_id: int,
    tick_number: int,
    consecutive_failures: int,
) -> tuple[int, bool, int]:
    """Run one tick with error handling. Returns (last_processed_id, has_more, consecutive_failures)."""
    try:
        last_processed_id, has_more = run_single_tick(
            db_connection, redis_connection, last_processed_id,
        )
        return last_processed_id, has_more, 0
    except Exception:
        consecutive_failures += 1
        backoff = min(consecutive_failures * 5, 30)
        logger.warning(
            "Tick #%d failed (%d consecutive) — backing off %.0fs",
            tick_number, consecutive_failures, backoff, exc_info=True,
        )
        time.sleep(backoff)
        return last_processed_id, False, consecutive_failures


def run_tick_loop(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    last_processed_id: int,
) -> None:
    """Run the detection loop indefinitely, with exponential back-off on failures."""
    tick_number = 0
    consecutive_failures = 0
    while True:
        tick_number += 1
        tick_start = time.monotonic()
        logger.debug("=== Tick #%d ===", tick_number)
        last_processed_id, has_more, consecutive_failures = execute_tick(
            db_connection, redis_connection, last_processed_id, tick_number, consecutive_failures,
        )
        elapsed = time.monotonic() - tick_start
        if elapsed > 2.0:
            logger.info("Tick #%d done in %.1fs", tick_number, elapsed)
        if not has_more:
            time.sleep(config.DETECTION_IDLE_SLEEP_SECONDS)


def run() -> None:
    """Orchestrate infrastructure setup, baseline wait, and detection loop."""
    db_connection, redis_connection = connect_infrastructure()
    try:
        last_processed_id = wait_for_baseline_cursor(redis_connection)
        run_tick_loop(db_connection, redis_connection, last_processed_id)
    finally:
        db_connection.close()
        redis_connection.close()


if __name__ == "__main__":
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s — %(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )
    run()
