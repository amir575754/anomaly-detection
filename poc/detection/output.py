"""
CLI formatting and printing for detection results. Uses rich for
color-coded panels with IQR deviations, SHAP contributions, and IF scores.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from detection.models import ScoredEvent, Severity
from detection.scoring import determine_severity
from detection.summary import DetectionSummary

logger = logging.getLogger(__name__)
console = Console()

SEVERITY_BORDER = {
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
}


def _build_iqr_table(event: ScoredEvent) -> Table | None:
    """Build a mini table of IQR feature deviations."""
    if not event.deviating_features:
        return None
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1))
    table.add_column("feature", style="bold")
    table.add_column("value", justify="right")
    table.add_column("range", style="dim")
    for deviation in event.deviating_features:
        table.add_row(
            deviation.feature_name,
            f"{deviation.observed_value:.4g}",
            f"(expected {deviation.lower_fence:.4g}\u2013{deviation.upper_fence:.4g}, "
            f"median {deviation.expected_median:.4g})",
        )
    return table


def _build_shap_table(event: ScoredEvent) -> Table | None:
    """Build a mini table of SHAP contributions."""
    if not event.shap_contributions:
        return None
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1))
    table.add_column("feature", style="bold")
    table.add_column("contribution", justify="right")
    for contribution in event.shap_contributions:
        color = "red" if contribution.contribution > 0 else "green"
        table.add_row(
            contribution.feature_name,
            f"[{color}]{contribution.contribution:+.3f}[/{color}]",
        )
    return table


def _build_panel(event: ScoredEvent, severity: Severity) -> Panel:
    """Build a rich Panel for a single detection."""
    row = event.telemetry_row
    color = SEVERITY_BORDER[severity]
    title = (
        f"[bold {color}]{severity.value}[/bold {color}] "
        f"{row['implant_id']} ({row['group_id']}) {row['config_type']}"
    )

    injector_tag = row.get("injector_tag") if row.get("is_anomaly") else None
    subtitle = f"[bold green]TP: {injector_tag}[/bold green]" if injector_tag else None

    iqr_table = _build_iqr_table(event)
    shap_table = _build_shap_table(event)

    parts: list[Text | Table] = []
    if iqr_table is not None:
        parts.append(Text("IQR Deviations:", style="bold underline"))
        parts.append(iqr_table)
    if shap_table is not None:
        parts.append(Text("SHAP Contributions:", style="bold underline"))
        parts.append(shap_table)

    scores_text = Text()
    scores_text.append("IF: ", style="bold")
    scores_text.append(f"{event.isolation_forest_score:.3f}", style="bold magenta")
    scores_text.append("  LOF: ", style="bold")
    scores_text.append(f"{event.lof_score:.3f}", style="bold magenta")
    scores_text.append("  Mahal p: ", style="bold")
    p_color = (
        "red" if event.mahalanobis_p_value < config.MAHALANOBIS_P_VALUE_HIGH
        else "yellow" if event.mahalanobis_p_value < config.MAHALANOBIS_P_VALUE_MEDIUM
        else "green"
    )
    scores_text.append(f"{event.mahalanobis_p_value:.1e}", style=f"bold {p_color}")
    parts.append(scores_text)
    panel_body = Group(*parts)

    return Panel(panel_body, title=title, subtitle=subtitle, border_style=color, expand=False)


def print_detections(scored_events: list[ScoredEvent]) -> int:
    """Determine severity and print detected anomalies. Returns count printed."""
    summary = DetectionSummary()
    summary.total_scored = len(scored_events)
    for event in scored_events:
        summary.record_scored(event)
        severity = determine_severity(
            event.deviating_features,
            event.if_predicts_anomaly,
            event.lof_predicts_anomaly,
        )
        if severity is None:
            row = event.telemetry_row
            if row.get("is_anomaly") and row.get("injector_tag"):
                summary.record_miss(event)
            continue
        console.print(_build_panel(event, severity))
        summary.record(event, severity)
    if summary.total_scored > 0:
        summary.print_summary()
    return summary.total_printed
