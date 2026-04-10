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

import psycopg2
import psycopg2.extensions
import redis as redis_module
from confluent_kafka import Consumer, KafkaError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ingestion.parser import prepare_batch_rows
from ingestion.writers import write_to_postgresql, write_to_redis

logger = logging.getLogger(__name__)


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


def flush_and_clear_batch(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
    batch: list[dict],
) -> int:
    """Flush the batch to storage and clear it. Returns number of messages flushed."""
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
    """Main consume-and-batch loop. Runs until interrupted.

    Pulls up to INGESTOR_CONSUME_BATCH_SIZE messages per Kafka call instead
    of polling one at a time. Flushes to storage when the batch reaches
    INGESTOR_BATCH_SIZE or after INGESTOR_IDLE_POLLS_BEFORE_FLUSH empty polls.
    """
    processed = 0
    last_logged_at_count = 0
    consecutive_empty_polls = 0
    batch: list[dict] = []
    while True:
        messages = consumer.consume(
            num_messages=config.INGESTOR_CONSUME_BATCH_SIZE,
            timeout=config.KAFKA_POLL_TIMEOUT_SECONDS,
        )
        if messages:
            consecutive_empty_polls = 0
            for message in messages:
                payload = extract_payload(message)
                if payload is not None:
                    batch.append(payload)
        else:
            consecutive_empty_polls += 1

        should_flush = (
            len(batch) >= config.INGESTOR_BATCH_SIZE
            or (batch and consecutive_empty_polls >= config.INGESTOR_IDLE_POLLS_BEFORE_FLUSH)
        )
        if should_flush:
            processed += flush_and_clear_batch(db_connection, redis_connection, batch)
            consecutive_empty_polls = 0
        if processed - last_logged_at_count >= config.INGESTOR_LOG_INTERVAL_MESSAGES:
            logger.info("Processed %d messages total", processed)
            last_logged_at_count = processed


def connect_infrastructure() -> tuple[psycopg2.extensions.connection, redis_module.Redis, Consumer]:
    """Establish connections to PostgreSQL, Redis, and Kafka."""
    logger.info("Connecting to PostgreSQL, Redis, and Kafka")
    db_connection = psycopg2.connect(config.DATABASE_DSN)
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
        redis_connection.close()


if __name__ == "__main__":
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s — %(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )
    run()
