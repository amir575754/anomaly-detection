"""
Database and Redis queries for the detection engine. Handles telemetry
fetching, implant registry loading, and Redis window key discovery.
"""

import logging
import os
import sys

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.keys import parse_window_key

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


