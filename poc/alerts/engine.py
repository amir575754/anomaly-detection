"""
Alert engine. Takes a list of ScoredEvents, applies fatigue mitigation
(suppression check -> deduplication -> rate limiting), determines severity,
generates human-readable explanations, and persists Alert records to PostgreSQL.
Also handles operator label application and false-positive suppression.
"""

import hashlib
import json
import logging
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extensions

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from alerts.models import Alert, FeatureDeviation, Label, ScoredEvent, Severity

logger = logging.getLogger(__name__)


def determine_severity(
    deviating_features: list[FeatureDeviation],
    isolation_forest_score: float,
) -> Severity | None:
    """
    Determine alert severity per the table in CLAUDE.md.
    Returns None if neither model finds anything worth alerting.
    """
    iqr_has_significant_deviation = (
        len(deviating_features) >= config.IQR_SIGNIFICANT_FEATURE_COUNT
        or any(
            deviation.iqr_multiplier > config.IQR_SIGNIFICANT_MULTIPLIER
            for deviation in deviating_features
        )
    )
    iqr_has_any_deviation = len(deviating_features) >= 1
    isolation_forest_is_high = isolation_forest_score > config.ISOLATION_FOREST_HIGH_THRESHOLD
    isolation_forest_is_medium = (
        config.ISOLATION_FOREST_MEDIUM_THRESHOLD
        <= isolation_forest_score
        <= config.ISOLATION_FOREST_HIGH_THRESHOLD
    )

    if iqr_has_significant_deviation and isolation_forest_is_high:
        return Severity.HIGH
    if iqr_has_any_deviation or isolation_forest_is_medium:
        return Severity.MEDIUM
    if isolation_forest_is_high and not iqr_has_any_deviation:
        return Severity.LOW
    return None


def generate_explanation(
    implant_id: str,
    group_id: str,
    deviating_features: list[FeatureDeviation],
    severity: Severity,
) -> str:
    """Build the human-readable alert explanation shown to NOC operators."""
    parts = [f"Implant {implant_id} (group {group_id}):"]
    for deviation in deviating_features:
        parts.append(
            f"`{deviation.feature_name}` is {deviation.observed_value:.4g} "
            f"(expected {deviation.lower_fence:.4g}\u2013{deviation.upper_fence:.4g}, "
            f"median {deviation.expected_median:.4g})."
        )
    parts.append(f"Severity: {severity.value}.")
    return " ".join(parts)


def compute_pattern_hash(
    implant_id: str,
    config_type: str,
    deviating_features: list[FeatureDeviation],
) -> str:
    """Stable hash of (implant, config type, sorted deviating feature names)."""
    feature_names = ",".join(
        sorted(deviation.feature_name for deviation in deviating_features)
    )
    key = f"{implant_id}:{config_type}:{feature_names}"
    return hashlib.sha256(key.encode()).hexdigest()[:config.PATTERN_HASH_LENGTH]


def fetch_suppressed_hashes(
    db_connection: psycopg2.extensions.connection,
) -> set[str]:
    """Pre-fetch active (non-expired) suppressed pattern hashes."""
    with db_connection.cursor() as cursor:
        if config.SUPPRESSION_DECAY_HOURS > 0:
            cursor.execute(
                """
                SELECT pattern_hash FROM suppressed_patterns
                WHERE suppressed_at > NOW() - (%s * INTERVAL '1 hour')
                """,
                (config.SUPPRESSION_DECAY_HOURS,),
            )
        else:
            cursor.execute("SELECT pattern_hash FROM suppressed_patterns")
        hashes = {row[0] for row in cursor.fetchall()}
    logger.debug("Loaded %d active suppressed pattern hashes", len(hashes))
    return hashes


def fetch_recent_pattern_hashes(
    db_connection: psycopg2.extensions.connection,
    dedup_cutoff: datetime,
) -> set[str]:
    """Pre-fetch pattern hashes of alerts within the dedup window."""
    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT details->>'pattern_hash'
            FROM alerts
            WHERE timestamp >= %s
            """,
            (dedup_cutoff,),
        )
        hashes = {row[0] for row in cursor.fetchall() if row[0] is not None}
    logger.debug(
        "Loaded %d recent pattern hashes (dedup cutoff: %s)",
        len(hashes), dedup_cutoff.isoformat(),
    )
    return hashes


def persist_alert(
    db_connection: psycopg2.extensions.connection,
    alert: Alert,
    pattern_hash: str,
) -> None:
    details = {
        "pattern_hash": pattern_hash,
        "deviating_features": [
            {
                "feature_name": deviation.feature_name,
                "observed_value": deviation.observed_value,
                "expected_median": deviation.expected_median,
                "lower_fence": deviation.lower_fence,
                "upper_fence": deviation.upper_fence,
                "iqr_multiplier": deviation.iqr_multiplier,
            }
            for deviation in alert.deviating_features
        ],
        "isolation_forest_score": alert.isolation_forest_score,
    }

    with db_connection:
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO alerts (
                    alert_id, implant_id, group_id, config_type, timestamp,
                    window_start, window_end, severity, baseline_used,
                    details, explanation, label, labeled_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    alert.alert_id,
                    alert.implant_id,
                    alert.group_id,
                    alert.config_type,
                    alert.timestamp,
                    alert.window_start,
                    alert.window_end,
                    alert.severity.value,
                    alert.baseline_used,
                    json.dumps(details),
                    alert.explanation,
                    alert.label.value if alert.label else None,
                    None,
                ),
            )
    logger.debug("Persisted alert %s to PostgreSQL", alert.alert_id)


def should_skip_event(
    pattern_hash: str,
    implant_id: str,
    suppressed_hashes: set[str],
    recent_hashes: set[str],
    alert_count_by_implant: Counter,
) -> str | None:
    """Return the skip reason, or None if the event should generate an alert."""
    if pattern_hash in suppressed_hashes:
        logger.debug("Skipping suppressed pattern %s for implant %s", pattern_hash, implant_id)
        return "suppressed"
    if pattern_hash in recent_hashes:
        logger.debug("Skipping duplicate pattern %s for implant %s", pattern_hash, implant_id)
        return "duplicate"
    if alert_count_by_implant[implant_id] >= config.RATE_LIMIT_PER_WINDOW:
        logger.debug("Rate limit reached for implant %s", implant_id)
        return "rate_limited"
    return None


def build_alert(
    event: ScoredEvent,
    severity: Severity,
    window_start: datetime,
    window_end: datetime,
) -> Alert:
    """Construct an Alert from a scored event."""
    row = event.telemetry_row
    return Alert(
        alert_id=str(uuid.uuid4()),
        implant_id=row["implant_id"],
        group_id=row["group_id"],
        config_type=row["config_type"],
        timestamp=datetime.now(timezone.utc),
        window_start=window_start,
        window_end=window_end,
        severity=severity,
        baseline_used=event.baseline_used,
        deviating_features=event.deviating_features,
        isolation_forest_score=event.isolation_forest_score,
        explanation=generate_explanation(
            row["implant_id"], row["group_id"], event.deviating_features, severity,
        ),
        label=None,
    )


def process_scored_events(
    db_connection: psycopg2.extensions.connection,
    scored_events: list[ScoredEvent],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """
    Filter, deduplicate, and persist alerts for a batch of scored events.
    Fatigue mitigation applied in order: suppression -> deduplication -> rate limiting.
    """
    logger.info("Processing %d scored events for alert generation", len(scored_events))
    dedup_cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.DEDUP_WINDOW_MINUTES)
    suppressed_hashes = fetch_suppressed_hashes(db_connection)
    recent_hashes = fetch_recent_pattern_hashes(db_connection, dedup_cutoff)
    alert_count_by_implant: Counter[str] = Counter()
    skip_counts: Counter[str] = Counter()

    for event in scored_events:
        severity = determine_severity(event.deviating_features, event.isolation_forest_score)
        if severity is None:
            skip_counts["no_severity"] += 1
            continue

        row = event.telemetry_row
        pattern_hash = compute_pattern_hash(row["implant_id"], row["config_type"], event.deviating_features)
        skip_reason = should_skip_event(
            pattern_hash, row["implant_id"], suppressed_hashes, recent_hashes, alert_count_by_implant,
        )
        if skip_reason:
            skip_counts[skip_reason] += 1
            continue

        alert = build_alert(event, severity, window_start, window_end)
        persist_alert(db_connection, alert, pattern_hash)
        recent_hashes.add(pattern_hash)
        alert_count_by_implant[row["implant_id"]] += 1
        logger.info(
            "ALERT CREATED: %s [%s] implant=%s config=%s IF=%.3f",
            alert.alert_id, severity.value, alert.implant_id,
            alert.config_type, event.isolation_forest_score,
        )

    logger.info(
        "Alert generation complete: %d created, %s",
        alert_count_by_implant.total(),
        ", ".join(f"{count} {reason}" for reason, count in skip_counts.items()),
    )


def apply_label(
    db_connection: psycopg2.extensions.connection,
    alert_id: str,
    label: Label,
) -> None:
    """Apply an operator label to an alert and trigger suppression if warranted."""
    logger.info("Applying label %r to alert %s", label.value, alert_id)
    with db_connection:
        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE alerts
                SET label = %s, labeled_at = NOW()
                WHERE alert_id = %s
                RETURNING details->>'pattern_hash'
                """,
                (label.value, alert_id),
            )
            row = cursor.fetchone()

    if row is None:
        logger.warning("Alert %s not found — label not applied", alert_id)
        return

    logger.debug("Label %r applied to alert %s (pattern_hash=%s)", label.value, alert_id, row[0])
    if label in (Label.FALSE_POSITIVE, Label.EXPECTED_DEVIATION):
        _check_and_suppress_pattern(db_connection, row[0])


def _check_and_suppress_pattern(
    db_connection: psycopg2.extensions.connection,
    pattern_hash: str,
) -> None:
    """If this pattern has accumulated enough suppression-eligible labels, suppress it."""
    if pattern_hash is None:
        return

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FROM alerts
            WHERE details->>'pattern_hash' = %s
              AND label IN ('false_positive', 'expected_deviation')
            """,
            (pattern_hash,),
        )
        result = cursor.fetchone()
        eligible_label_count = result[0] if result else 0

    logger.debug(
        "Pattern %s has %d suppression-eligible labels (threshold=%d)",
        pattern_hash, eligible_label_count, config.SUPPRESSION_LABEL_THRESHOLD,
    )

    if eligible_label_count >= config.SUPPRESSION_LABEL_THRESHOLD:
        with db_connection:
            with db_connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO suppressed_patterns (pattern_hash, suppressed_at, label_count)
                    VALUES (%s, NOW(), %s)
                    ON CONFLICT (pattern_hash)
                    DO UPDATE SET label_count = EXCLUDED.label_count
                    """,
                    (pattern_hash, eligible_label_count),
                )
        logger.warning(
            "Pattern %s SUPPRESSED after %d eligible labels — "
            "no further alerts will be raised for this pattern",
            pattern_hash, eligible_label_count,
        )


def clear_all_suppressions(
    db_connection: psycopg2.extensions.connection,
) -> int:
    """Remove all suppressed patterns. Returns the number of patterns cleared."""
    with db_connection:
        with db_connection.cursor() as cursor:
            cursor.execute("DELETE FROM suppressed_patterns")
            cleared_count = cursor.rowcount
    logger.warning("Cleared %d suppressed patterns", cleared_count)
    return cleared_count
