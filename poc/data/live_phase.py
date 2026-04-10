"""
Live phase — streams new snapshots with anomaly injection at ANOMALY_RATE.
"""

import logging
import os
import random
import sys
import time

from confluent_kafka import Producer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.kafka_utils import publish
from data.anomalies import inject_random_anomaly
from data.profiles import generate_snapshot

logger = logging.getLogger(__name__)


def enforce_rate_limit(
    window_start: float,
    window_count: int,
    max_events_per_second: int,
) -> tuple[float, int]:
    """Sleep if needed to enforce the rate limit. Returns updated (window_start, window_count)."""
    if max_events_per_second <= 0 or window_count < max_events_per_second:
        return window_start, window_count
    elapsed = time.monotonic() - window_start
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    return window_start + 1.0, 0


def generate_live_event(
    implant_id: str,
    group_id: str,
) -> tuple[dict, bool]:
    """Generate a live snapshot, possibly anomalous. Returns (snapshot, is_anomaly)."""
    snapshot = generate_snapshot(implant_id, group_id)
    if random.random() < config.ANOMALY_RATE:
        anomalous_snapshot = inject_random_anomaly(snapshot)
        if anomalous_snapshot is not None:
            logger.debug(
                "Injected anomaly: implant=%s type=%s injector=%s",
                implant_id, snapshot["metadata"]["type"],
                anomalous_snapshot["ground_truth"].get("injector_tag"),
            )
            return anomalous_snapshot, True
    return snapshot, False


def has_reached_event_cap(total_published: int) -> bool:
    """Return True if the live phase should stop due to the event cap."""
    return config.LIVE_PHASE_MAX_EVENTS > 0 and total_published >= config.LIVE_PHASE_MAX_EVENTS


def publish_single_live_event(
    producer: Producer,
    implant_id: str,
    group_id: str,
    total_published: int,
    total_anomalies: int,
    window_start: float,
    window_count: int,
) -> tuple[int, int, float, int]:
    """Publish one live event and return updated counters."""
    snapshot, is_anomaly = generate_live_event(implant_id, group_id)
    if is_anomaly:
        total_anomalies += 1
    publish(producer, snapshot)
    total_published += 1
    window_count += 1
    window_start, window_count = enforce_rate_limit(
        window_start, window_count, config.LIVE_PHASE_MAX_EVENTS_PER_SECOND,
    )
    return total_published, total_anomalies, window_start, window_count


def publish_live_round(
    producer: Producer,
    implants: list[tuple[str, str]],
    total_published: int,
    total_anomalies: int,
    window_start: float,
    window_count: int,
) -> tuple[int, int, float, int, bool]:
    """Publish one event per implant. Returns updated counters and whether cap was reached."""
    for implant_id, group_id in implants:
        total_published, total_anomalies, window_start, window_count = (
            publish_single_live_event(
                producer, implant_id, group_id,
                total_published, total_anomalies, window_start, window_count,
            )
        )
        if has_reached_event_cap(total_published):
            return total_published, total_anomalies, window_start, window_count, True
    return total_published, total_anomalies, window_start, window_count, False


def stream_live_rounds(producer: Producer, implants: list[tuple[str, str]]) -> None:
    """Run the live publishing loop until cap is reached or interrupted."""
    total_published = 0
    total_anomalies = 0
    window_start = time.monotonic()
    window_count = 0
    # Tracks the next event count at which we should emit a progress log.
    # Using a threshold variable rather than modulo arithmetic because each
    # round publishes len(implants) events at once, so total_published jumps
    # in steps that may skip over an exact multiple of the log interval.
    next_log_at = config.INGESTOR_LOG_INTERVAL_MESSAGES
    while True:
        total_published, total_anomalies, window_start, window_count, capped = (
            publish_live_round(
                producer, implants, total_published, total_anomalies,
                window_start, window_count,
            )
        )
        if capped:
            producer.flush()
            log_live_summary(total_published, total_anomalies, "reached event cap")
            return
        if total_published >= next_log_at:
            producer.flush()
            log_live_summary(total_published, total_anomalies, "progress")
            next_log_at += config.INGESTOR_LOG_INTERVAL_MESSAGES


def run_live_phase(producer: Producer, implants: list[tuple[str, str]]) -> None:
    """Stream rate-limited snapshots with anomaly injection until cap or interrupt."""
    logger.info(
        "Live phase started — anomaly rate: %.1f%%, rate limit: %s/sec, max: %s",
        config.ANOMALY_RATE * 100,
        config.LIVE_PHASE_MAX_EVENTS_PER_SECOND or "unlimited",
        config.LIVE_PHASE_MAX_EVENTS or "unlimited",
    )
    try:
        stream_live_rounds(producer, implants)
    except KeyboardInterrupt:
        logger.info("Live phase stopped by user")


def log_live_summary(total_published: int, total_anomalies: int, reason: str) -> None:
    """Log a one-line summary of the live phase."""
    rate = total_anomalies / total_published * 100 if total_published else 0
    logger.info(
        "Live phase %s — %d events, %d anomalies (%.1f%%)",
        reason, total_published, total_anomalies, rate,
    )
