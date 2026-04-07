"""
Orchestrates the two-phase demo run:
  1. Baseline phase — publishes clean synthetic snapshots, waits for the
     ingestor to finish, and bootstraps the initial baselines.
  2. Live phase — streams new snapshots with anomaly injection at ANOMALY_RATE.
"""

import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extensions
import redis as redis_module
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.anomalies import inject_random_anomaly
from data.profiles import generate_snapshot, sample_config_type
from detection.baseline import refresh_baseline
from detection.keys import parse_window_key

logger = logging.getLogger(__name__)

_MINUTES_PER_SNAPSHOT: int = (24 * 60) // config.SNAPSHOTS_PER_IMPLANT_PER_DAY


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


def publish(producer: Producer, snapshot: dict) -> None:
    # poll(0) services delivery callbacks, preventing the internal queue
    # from filling up when producing faster than the broker can acknowledge
    producer.poll(0)
    producer.produce(
        config.KAFKA_TOPIC,
        value=json.dumps(snapshot).encode("utf-8"),
    )


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
    """Refresh baselines for all window keys populated during ingestion."""
    logger.info("Connecting to PostgreSQL and Redis for baseline bootstrap")
    db_connection = psycopg2.connect(config.DATABASE_DSN)
    redis_connection = redis_module.Redis(
        host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=False
    )
    try:
        raw_keys = list(redis_connection.scan_iter(match="window:*"))
        logger.info("Bootstrapping baselines for %d window keys", len(raw_keys))
        results = [try_refresh_single_baseline(redis_connection, db_connection, k) for k in raw_keys]
        succeeded = sum(results)
        logger.info(
            "Baseline bootstrap complete: %d succeeded, %d failed out of %d keys",
            succeeded, len(results) - succeeded, len(raw_keys),
        )
    finally:
        db_connection.close()
        redis_connection.close()


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
        if total_published % config.INGESTOR_LOG_INTERVAL_MESSAGES == 0:
            producer.flush()
            log_live_summary(total_published, total_anomalies, "progress")


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
    logger.info("=== Baseline: generating synthetic history ===")
    total_baseline_events = run_baseline_phase(producer, implants)
    logger.info("=== Baseline: waiting for ingestor ===")
    wait_for_ingestor(total_baseline_events)
    logger.info("=== Baseline: bootstrapping models ===")
    bootstrap_baselines()
    save_baseline_cursor()
    logger.info("=== Live: streaming with anomaly injection ===")
    run_live_phase(producer, implants)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    main()
