"""
CLI formatting and printing for detection results. Formats scored events
as human-readable, color-coded terminal output.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.models import ScoredEvent, Severity
from detection.scoring import determine_severity

logger = logging.getLogger(__name__)


SEVERITY_COLORS = {
    Severity.HIGH: "\033[91m",    # red
    Severity.MEDIUM: "\033[93m",  # yellow
    Severity.LOW: "\033[96m",     # cyan
}
RESET_COLOR = "\033[0m"
DIM = "\033[2m"


def format_iqr_lines(event: ScoredEvent) -> list[str]:
    """Format IQR deviation lines for a detection."""
    return [
        f"  ├─ IQR: {d.feature_name} = {d.observed_value:.4g} "
        f"{DIM}(expected {d.lower_fence:.4g}–{d.upper_fence:.4g}, "
        f"median {d.expected_median:.4g}){RESET_COLOR}"
        for d in event.deviating_features
    ]


def format_shap_line(event: ScoredEvent) -> str | None:
    """Format a SHAP contribution line, or None if not applicable."""
    if not event.shap_contributions or event.deviating_features:
        return None
    shap_parts = ", ".join(
        f"{c.feature_name} ({c.contribution:+.3f})" for c in event.shap_contributions
    )
    return f"  ├─ SHAP: {shap_parts}"


def format_detection(event: ScoredEvent, severity: Severity) -> str:
    """Format a scored event as a human-readable CLI detection line."""
    row = event.telemetry_row
    color = SEVERITY_COLORS.get(severity, "")
    lines = [
        f"{color}[{severity.value}]{RESET_COLOR} "
        f"{row['implant_id']} ({row['group_id']}) {row['config_type']}"
    ]
    lines.extend(format_iqr_lines(event))
    shap_line = format_shap_line(event)
    if shap_line is not None:
        lines.append(shap_line)
    lines.append(f"  └─ IF score: {event.isolation_forest_score:.3f}")
    return "\n".join(lines)


def print_detections(scored_events: list[ScoredEvent]) -> int:
    """Determine severity and print detected anomalies. Returns count printed.

    Prints HIGH (IQR + IF agree), MEDIUM only when IQR flagged something,
    and LOW (IF-only with high score). Skips MEDIUM events with no IQR
    evidence to keep the CLI readable without deduplication.
    """
    printed = 0
    suppressed = 0
    for event in scored_events:
        severity = determine_severity(event.deviating_features, event.isolation_forest_score)
        if severity is None:
            continue
        if severity == Severity.MEDIUM and not event.deviating_features:
            suppressed += 1
            continue
        print(format_detection(event, severity))
        printed += 1
    if suppressed:
        logger.debug("Suppressed %d IF-only MEDIUM events (no IQR evidence)", suppressed)
    return printed
