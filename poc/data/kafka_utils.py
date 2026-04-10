"""Shared Kafka helpers used by both baseline and live phases."""

import json

from confluent_kafka import Producer

import config


def publish(producer: Producer, snapshot: dict) -> None:
    # poll(0) services delivery callbacks, preventing the internal queue
    # from filling up when producing faster than the broker can acknowledge
    producer.poll(0)
    producer.produce(
        config.KAFKA_TOPIC,
        value=json.dumps(snapshot).encode("utf-8"),
    )
