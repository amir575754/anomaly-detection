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
    """Build a mini table of SHAP contributions.

    Negative SHAP values push toward anomalous (IF decision_function
    convention: lower = more anomalous). More negative = bigger
    contribution to the anomaly.
    """
    if not event.shap_contributions:
        return None
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1))
    table.add_column("feature", style="bold")
    table.add_column("contribution", justify="right")
    table.add_column("direction", style="dim")
    for contribution in event.shap_contributions:
        color = "red" if contribution.contribution < 0 else "green"
        direction = "anomalous" if contribution.contribution < 0 else "normal"
        table.add_row(
            contribution.feature_name,
            f"[{color}]{contribution.contribution:+.3f}[/{color}]",
            f"[{color}]{direction}[/{color}]",
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

    from detection.scoring import classify_iqr_signals
    iqr_significant, _iqr_any = classify_iqr_signals(event.deviating_features)
    iqr_label = "FLAGGED" if iqr_significant else "FLAGGED (weak)" if event.deviating_features else "ok"
    iqr_label_color = "red" if event.deviating_features else "dim"

    parts: list[Text | Table] = []
    if iqr_table is not None:
        iqr_header = Text()
        iqr_header.append("IQR Deviations ", style="bold underline")
        iqr_header.append(f"[{iqr_label}]", style=iqr_label_color)
        parts.append(iqr_header)
        parts.append(iqr_table)
    if shap_table is not None:
        parts.append(Text("SHAP Contributions:", style="bold underline"))
        parts.append(shap_table)

    scores_text = Text()

    if_flag = "FLAGGED" if event.if_predicts_anomaly else "ok"
    if_color = "red" if event.if_predicts_anomaly else "dim"
    scores_text.append("IF: ", style="bold")
    scores_text.append(f"{event.isolation_forest_score:.3f} ", style="bold magenta")
    scores_text.append(f"[{if_flag}]", style=if_color)

    lof_flag = "FLAGGED" if event.lof_predicts_anomaly else "ok"
    lof_color = "red" if event.lof_predicts_anomaly else "dim"
    scores_text.append("  LOF: ", style="bold")
    scores_text.append(f"{event.lof_score:.3f} ", style="bold magenta")
    scores_text.append(f"[{lof_flag}]", style=lof_color)

    mahal_flagged = event.mahalanobis_p_value < config.MAHALANOBIS_P_VALUE_MEDIUM
    mahal_flag = "FLAGGED" if mahal_flagged else "ok"
    p_color = (
        "red" if event.mahalanobis_p_value < config.MAHALANOBIS_P_VALUE_HIGH
        else "yellow" if mahal_flagged
        else "dim"
    )
    scores_text.append("  Mahal p: ", style="bold")
    scores_text.append(f"{event.mahalanobis_p_value:.1e} ", style=f"bold {p_color}")
    scores_text.append(f"[{mahal_flag}]", style=p_color)

    parts.append(scores_text)
    panel_body = Group(*parts)

    return Panel(panel_body, title=title, subtitle=subtitle, border_style=color, expand=False)


def print_detections(scored_events: list[ScoredEvent]) -> int:
    """Print detected anomalies. Returns count printed.

    Severity is read from the event (computed once in scoring_pipeline) so
    this function and the scoring loop can never disagree on whether an
    event is an alert.
    """
    summary = DetectionSummary()
    summary.total_scored = len(scored_events)
    for event in scored_events:
        summary.record_scored(event)
        if event.severity is None:
            row = event.telemetry_row
            if row.get("is_anomaly") and row.get("injector_tag"):
                summary.record_miss(event)
            continue
        console.print(_build_panel(event, event.severity))
        summary.record(event, event.severity)
    if summary.total_scored > 0:
        summary.print_summary()
    return summary.total_printed
