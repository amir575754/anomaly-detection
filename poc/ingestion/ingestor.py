"""
Kafka consumer that writes telemetry to PostgreSQL and maintains per-scope
feature vector sliding windows in Redis.

Performance: messages are accumulated in memory and flushed in batches to
reduce per-message round-trip overhead against PostgreSQL and Redis.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import redis as redis_module
from confluent_kafka import Consumer, KafkaError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from extraction.extractor import extract_features

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def parse_received_at(timestamp_string: str) -> datetime:
    """Parse the DD-MM-YYYY HH:MM:SS[.ff] format used in telemetry metadata."""
    for format_string in ("%d-%m-%Y %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(timestamp_string, format_string).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp format: {timestamp_string!r}")


def prepare_batch_rows(batch: list[dict]) -> tuple[list, list, list[tuple[str, str]]]:
    """
    Parse a batch of raw Kafka messages into rows for PostgreSQL and Redis.
    Returns (implant_rows, telemetry_rows, window_commands).
    """
    implant_map: dict[str, tuple] = {}
    telemetry_rows = []
    window_commands: list[tuple[str, str]] = []

    for message in batch:
        implant_row, telemetry_row, commands = parse_message(message)
        implant_id = implant_row[0]
        existing = implant_map.get(implant_id)
        if existing is None:
            implant_map[implant_id] = implant_row
        else:
            implant_map[implant_id] = (
                implant_id,
                implant_row[1],
                min(existing[2], implant_row[2]),
                max(existing[3], implant_row[3]),
            )
        telemetry_rows.append(telemetry_row)
        window_commands.extend(commands)

    implant_rows = list(implant_map.values())
    logger.debug(
        "Prepared batch: %d unique implants, %d telemetry rows, %d window commands",
        len(implant_rows), len(telemetry_rows), len(window_commands),
    )
    return implant_rows, telemetry_rows, window_commands


def build_window_commands(
    implant_id: str, group_id: str, config_type: str, features_json: str,
) -> list[tuple[str, str]]:
    """Build Redis window push commands for both implant and group scopes."""
    return [
        (f"window:implant:{implant_id}:{config_type}", features_json),
        (f"window:group:{group_id}:{config_type}", features_json),
    ]


def parse_message(message: dict) -> tuple[tuple, tuple, list[tuple[str, str]]]:
    """Extract implant row, telemetry row, and window commands from one message."""
    metadata = message["metadata"]
    implant_id = str(metadata["implant_id"])
    group_id = str(metadata["group_id"])
    config_type = metadata["type"]
    received_at = parse_received_at(metadata["received_at"])
    ground_truth = message.get("ground_truth", {})

    features_json = json.dumps(extract_features(message["configuration"], config_type))
    is_anomaly = bool(ground_truth.get("is_anomaly", False))

    implant_row = (implant_id, group_id, received_at, received_at)
    telemetry_row = (
        implant_id, config_type, received_at,
        json.dumps(message), features_json, is_anomaly,
        ground_truth.get("injector_tag"),
    )
    return implant_row, telemetry_row, build_window_commands(implant_id, group_id, config_type, features_json)


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


def flush_batch(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    batch: list[dict],
) -> None:
    """Write a batch of parsed messages to PostgreSQL and Redis in bulk."""
    if not batch:
        return
    logger.debug("Flushing batch of %d messages", len(batch))
    implant_rows, telemetry_rows, window_commands = prepare_batch_rows(batch)
    write_to_postgresql(db_connection, implant_rows, telemetry_rows)
    write_to_redis(redis_connection, window_commands)


def make_consumer() -> Consumer:
    """Create and configure a Kafka consumer."""
    logger.info(
        "Connecting to Kafka at %s, topic %r, group 'ingestor-group'",
        config.KAFKA_BOOTSTRAP, config.KAFKA_TOPIC,
    )
    consumer = Consumer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "group.id": "ingestor-group",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([config.KAFKA_TOPIC])
    logger.info("Kafka consumer subscribed to topic %r", config.KAFKA_TOPIC)
    return consumer


def flush_if_needed(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    batch: list[dict],
) -> int:
    """Flush the batch if non-empty; return number of messages flushed."""
    if not batch:
        return 0
    flush_batch(db_connection, redis_connection, batch)
    count = len(batch)
    batch.clear()
    return count


_BENIGN_KAFKA_ERRORS = frozenset({
    KafkaError._PARTITION_EOF,
    KafkaError.UNKNOWN_TOPIC_OR_PART,
})


def extract_payload(message) -> dict | None:
    """
    Extract the JSON payload from a Kafka message.
    Returns None if the message is empty or carries a benign status code.
    Raises on unexpected Kafka errors.
    """
    if message is None:
        return None
    error = message.error()
    if error is not None:
        if error.code() in _BENIGN_KAFKA_ERRORS:
            logger.debug("Benign Kafka status: %s", error)
            return None
        logger.critical("Fatal Kafka error: %s", error)
        raise RuntimeError(f"Kafka error: {error}")
    return json.loads(message.value().decode("utf-8"))


def consume_loop(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    consumer: Consumer,
) -> None:
    """Main consume-and-batch loop. Runs until interrupted."""
    processed = 0
    last_logged_at_count = 0
    batch: list[dict] = []

    while True:
        payload = extract_payload(
            consumer.poll(timeout=config.KAFKA_POLL_TIMEOUT_SECONDS)
        )

        if payload is not None:
            batch.append(payload)

        batch_is_full = len(batch) >= config.INGESTOR_BATCH_SIZE
        idle_with_pending_data = payload is None and batch

        if batch_is_full or idle_with_pending_data:
            if idle_with_pending_data:
                logger.debug(
                    "Idle flush: %d messages pending (below batch size %d)",
                    len(batch), config.INGESTOR_BATCH_SIZE,
                )
            processed += flush_if_needed(db_connection, redis_connection, batch)

        if processed - last_logged_at_count >= config.INGESTOR_LOG_INTERVAL_MESSAGES:
            logger.info("Processed %d messages total", processed)
            last_logged_at_count = processed


def connect_infrastructure() -> tuple[psycopg2.extensions.connection, redis_module.Redis, Consumer]:
    """Establish connections to PostgreSQL, Redis, and Kafka."""
    logger.info("Connecting to PostgreSQL, Redis, and Kafka")
    db_connection = psycopg2.connect(config.DB_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False,
    )
    consumer = make_consumer()
    logger.info("All connections established")
    return db_connection, redis_connection, consumer


def run() -> None:
    db_connection, redis_connection, consumer = connect_infrastructure()
    logger.info("Ingestor ready — batch size %d", config.INGESTOR_BATCH_SIZE)

    try:
        consume_loop(db_connection, redis_connection, consumer)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    except Exception:
        logger.critical("Unrecoverable error", exc_info=True)
        raise
    finally:
        consumer.close()
        db_connection.close()


if __name__ == "__main__":
    run()
