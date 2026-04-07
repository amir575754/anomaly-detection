"""
Reset all system state for a fresh demo run.

Clears PostgreSQL tables (telemetry, baselines, implants), flushes all Redis
keys (window:*, model:*, writes:*, detector:*), and deletes the Kafka topic
so the generator recreates it on next startup.

Usage:
    python reset.py          # interactive confirmation
    python reset.py --force  # skip confirmation
"""

import argparse
import logging
import sys
import time

import psycopg2
import redis as redis_module
from confluent_kafka.admin import AdminClient

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [reset] %(message)s",
)
logger = logging.getLogger(__name__)

_APPLICATION_TABLES = (
    "baselines", "telemetry", "implants",
)

_REDIS_KEY_PATTERNS = ("window:*", "model:*", "writes:*", "detector:*")


def log_table_row_counts(database_connection) -> None:
    """Log the current row count for each application table."""
    with database_connection.cursor() as cursor:
        for table_name in _APPLICATION_TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            result = cursor.fetchone()
            row_count = result[0] if result else 0
            logger.info("  %s: %d rows to delete", table_name, row_count)


def terminate_active_connections(database_connection) -> None:
    """Terminate non-idle connections to the current database."""
    with database_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid != pg_backend_pid()
              AND state != 'idle'
            """
        )
        if cursor.rowcount > 0:
            logger.info("PostgreSQL: terminated %d active connections", cursor.rowcount)


def truncate_all_tables(database_connection) -> None:
    """Truncate all application tables with CASCADE."""
    table_list = ", ".join(_APPLICATION_TABLES)
    with database_connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE TABLE {table_list} CASCADE")
    logger.info("PostgreSQL: all tables truncated successfully")


def reset_postgresql() -> None:
    """Truncate all application tables, terminating other connections first."""
    logger.info("PostgreSQL: connecting to %s", config.DATABASE_DSN.split("@")[-1])
    database_connection = psycopg2.connect(config.DATABASE_DSN)
    database_connection.autocommit = True
    try:
        log_table_row_counts(database_connection)
        terminate_active_connections(database_connection)
        truncate_all_tables(database_connection)
    except Exception:
        logger.exception("PostgreSQL: reset failed")
        raise
    finally:
        database_connection.close()


def reset_redis() -> None:
    """Delete all window:*, model:*, and writes:* keys."""
    logger.info("Redis: connecting to %s:%d", config.REDIS_HOST, config.REDIS_PORT)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )

    try:
        total_deleted = 0
        for pattern in _REDIS_KEY_PATTERNS:
            keys = list(redis_connection.scan_iter(match=pattern))
            if keys:
                redis_connection.delete(*keys)
                logger.info("  %s: deleted %d keys", pattern, len(keys))
                total_deleted += len(keys)
            else:
                logger.info("  %s: no keys found", pattern)

        logger.info("Redis: deleted %d keys total", total_deleted)
    finally:
        redis_connection.close()


def reset_kafka() -> None:
    """Delete the telemetry topic so the generator recreates it on next startup."""
    logger.info("Kafka: connecting to %s", config.KAFKA_BOOTSTRAP)
    admin = AdminClient({"bootstrap.servers": config.KAFKA_BOOTSTRAP})

    existing_topics = admin.list_topics(timeout=10).topics
    if config.KAFKA_TOPIC not in existing_topics:
        logger.info("Kafka: topic %r does not exist, nothing to delete", config.KAFKA_TOPIC)
        return

    logger.info("Kafka: deleting topic %r", config.KAFKA_TOPIC)
    futures = admin.delete_topics([config.KAFKA_TOPIC])
    for topic, future in futures.items():
        try:
            future.result()
            logger.info("Kafka: topic %r deleted successfully", topic)
        except Exception:
            logger.exception("Kafka: failed to delete topic %r", topic)

    logger.info("Kafka: waiting 3 seconds for broker to complete deletion")
    time.sleep(3)


def confirm_reset() -> bool:
    """Ask the user to confirm the reset. Returns True if confirmed."""
    answer = input(
        "This will DELETE all telemetry, baselines, Redis state, "
        "and Kafka messages.\nType 'yes' to confirm: "
    )
    return answer.strip().lower() == "yes"


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset all demo state")
    parser.add_argument("--force", action="store_true", help="Skip confirmation")
    arguments = parser.parse_args()

    if not arguments.force and not confirm_reset():
        print("Aborted.")
        sys.exit(1)

    logger.info("Starting full system reset")
    start_time = time.monotonic()

    reset_postgresql()
    reset_redis()
    reset_kafka()

    elapsed = time.monotonic() - start_time
    logger.info("Reset complete in %.1f seconds", elapsed)


if __name__ == "__main__":
    main()
