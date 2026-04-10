"""
Orchestrates the two-phase demo run:
  1. Baseline phase — publishes clean synthetic snapshots, waits for the
     ingestor to finish, and bootstraps the initial baselines.
  2. Live phase — streams new snapshots with anomaly injection at ANOMALY_RATE.
"""

import logging
import os
import sys

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.baseline_phase import (
    bootstrap_baselines,
    run_baseline_phase,
    save_baseline_cursor,
    wait_for_ingestor,
)
from data.live_phase import run_live_phase

logger = logging.getLogger(__name__)


def build_implant_list() -> list[tuple[str, str]]:
    """Return list of (implant_id, group_id) pairs from IMPLANT_GROUPS config."""
    return [
        (f"implant_{group}_{index:03d}", group)
        for group, count in config.IMPLANT_GROUPS.items()
        for index in range(count)
    ]


def ensure_topic_exists(bootstrap: str, topic: str) -> None:
    logger.info("Checking Kafka topic %r on %s", topic, bootstrap)
    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing_topics = admin.list_topics(timeout=10).topics
    if topic not in existing_topics:
        futures = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        for future in futures.values():
            future.result()
        logger.info("Created Kafka topic %r", topic)
    else:
        logger.debug("Kafka topic %r already exists", topic)


def make_producer() -> Producer:
    return Producer({
        "bootstrap.servers": config.KAFKA_BOOTSTRAP,
        "linger.ms": 5,
        "batch.size": 65536,
        "queue.buffering.max.messages": 500000,
    })


def setup_generator() -> tuple[Producer, list[tuple[str, str]]]:
    """Initialise the Kafka producer and build the implant list."""
    implants = build_implant_list()
    logger.info(
        "%d implants across %d groups: %s",
        len(implants), len(config.IMPLANT_GROUPS),
        ", ".join(f"{group}={count}" for group, count in config.IMPLANT_GROUPS.items()),
    )
    ensure_topic_exists(config.KAFKA_BOOTSTRAP, config.KAFKA_TOPIC)
    producer = make_producer()
    logger.info("Kafka producer connected to %s", config.KAFKA_BOOTSTRAP)
    return producer, implants


def main() -> None:
    producer, implants = setup_generator()
    try:
        logger.info("=== Baseline: generating synthetic history ===")
        total_baseline_events = run_baseline_phase(producer, implants)
        logger.info("=== Baseline: waiting for ingestor ===")
        wait_for_ingestor(total_baseline_events)
        logger.info("=== Baseline: bootstrapping models ===")
        bootstrap_baselines()
        save_baseline_cursor()
        logger.info("=== Live: streaming with anomaly injection ===")
        run_live_phase(producer, implants)
    finally:
        # Flush any queued messages before exit so partial baseline data
        # isn't lost on error. Producer has no explicit close() — flush is
        # sufficient for confluent_kafka cleanup.
        producer.flush()


if __name__ == "__main__":
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s — %(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )
    main()
