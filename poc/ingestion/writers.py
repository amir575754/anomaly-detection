"""
Storage writers. Bulk-insert rows into PostgreSQL and push feature vectors
to Redis sliding windows in pipelined batches.
"""

import logging

import psycopg2.extensions
import psycopg2.extras
import redis as redis_module

import config

logger = logging.getLogger(__name__)


def write_to_postgresql(
    db_connection: psycopg2.extensions.connection,
    implant_rows: list,
    telemetry_rows: list,
) -> None:
    """Bulk-insert implant and telemetry rows."""
    logger.debug(
        "Writing to PostgreSQL: %d implant upserts, %d telemetry inserts",
        len(implant_rows), len(telemetry_rows),
    )
    with db_connection:
        with db_connection.cursor() as cursor:
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO implants (implant_id, group_id, first_seen, last_seen, status)
                VALUES %s
                ON CONFLICT (implant_id)
                DO UPDATE SET
                    first_seen = LEAST(implants.first_seen, EXCLUDED.first_seen),
                    last_seen = GREATEST(implants.last_seen, EXCLUDED.last_seen)
                """,
                implant_rows,
                template="(%s, %s, %s, %s, 'active')",
            )
            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO telemetry
                    (implant_id, config_type, received_at, raw, features, is_anomaly, injector_tag)
                VALUES %s
                """,
                telemetry_rows,
            )
    logger.debug("PostgreSQL write complete")


def write_to_redis(
    redis_connection: redis_module.Redis,
    window_commands: list[tuple[str, str]],
) -> None:
    """Push feature vectors to sliding windows and increment write counters."""
    pipeline = redis_connection.pipeline(transaction=False)
    writes_counter: dict[str, int] = {}

    for key, features_json in window_commands:
        pipeline.rpush(key, features_json)
        pipeline.ltrim(key, -config.BASELINE_WINDOW_SIZE, -1)
        writes_counter[key] = writes_counter.get(key, 0) + 1

    for window_key, count in writes_counter.items():
        writes_key = "writes:" + window_key.split(":", 1)[1]
        pipeline.incrby(writes_key, count)

    pipeline.execute()
    logger.debug(
        "Redis pipeline: %d window pushes across %d keys",
        len(window_commands), len(writes_counter),
    )
