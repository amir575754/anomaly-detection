"""
Detection summary tracker. Accumulates stats across a scoring batch
and prints a rich summary table on demand.
"""

import os
import sys
from collections import Counter

from rich.console import Console
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

from detection.models import ScoredEvent, Severity

console = Console()


class DetectionSummary:
    """Tracks detection statistics for a single scoring batch."""

    def __init__(self) -> None:
        self.severity_counts: Counter[str] = Counter()
        self.config_type_counts: Counter[str] = Counter()
        self.total_scored: int = 0
        self.total_printed: int = 0
        self.true_positives: int = 0
        self.false_positives: int = 0
        self.missed_anomalies: int = 0
        self.injector_detected: Counter[str] = Counter()
        self.injector_detected_iqr: Counter[str] = Counter()
        self.injector_detected_if: Counter[str] = Counter()
        self.injector_detected_lof: Counter[str] = Counter()
        self.injector_injected: Counter[str] = Counter()

    def record_scored(self, event: ScoredEvent) -> None:
        """Record every scored event to count total injected anomalies."""
        row = event.telemetry_row
        if row.get("is_anomaly") and row.get("injector_tag"):
            tag = _normalize_tag(row["injector_tag"])
            self.injector_injected[tag] += 1

    def record(self, event: ScoredEvent, severity: Severity) -> None:
        """Record a printed detection."""
        self.total_printed += 1
        self.severity_counts[severity.value] += 1
        row = event.telemetry_row
        self.config_type_counts[row["config_type"]] += 1
        if row.get("is_anomaly") and row.get("injector_tag"):
            self.true_positives += 1
            tag = _normalize_tag(row["injector_tag"])
            self.injector_detected[tag] += 1
            if event.deviating_features:
                self.injector_detected_iqr[tag] += 1
            if event.isolation_forest_score >= config.ISOLATION_FOREST_REPORTING_THRESHOLD:
                self.injector_detected_if[tag] += 1
            if event.lof_score >= config.LOF_REPORTING_THRESHOLD:
                self.injector_detected_lof[tag] += 1
        else:
            self.false_positives += 1

    def record_miss(self, event: ScoredEvent) -> None:
        """Record an injected anomaly that was not detected."""
        self.missed_anomalies += 1

    def print_summary(self) -> None:
        if self.total_printed == 0:
            return
        console.print()
        console.rule("[bold bright_white on blue]  DETECTION SUMMARY  [/]", style="blue")
        console.print()
        self._print_injector_table()
        self._print_severity_table()
        self._print_overall_stats()

    def _print_severity_table(self) -> None:
        table = Table(title="[bold]Severity Breakdown[/bold]", show_lines=False, border_style="dim")
        table.add_column("Severity", style="bold")
        table.add_column("Count", justify="right")
        table.add_column("Bar", min_width=20)
        total = max(sum(self.severity_counts.values()), 1)
        for level in ("HIGH", "MEDIUM", "LOW"):
            count = self.severity_counts.get(level, 0)
            color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "cyan"}[level]
            bar_len = int(count / total * 20)
            bar = f"[{color}]{'█' * bar_len}{'░' * (20 - bar_len)}[/{color}]"
            table.add_row(f"[{color}]{level}[/{color}]", str(count), bar)
        console.print(table)

    def _print_injector_table(self) -> None:
        all_tags = sorted(
            set(self.injector_injected) | set(self.injector_detected),
            key=lambda t: -self.injector_injected.get(t, 0),
        )
        if not all_tags:
            return
        table = Table(
            title="[bold]Injector Detection Breakdown[/bold]",
            show_lines=False, border_style="dim",
        )
        table.add_column("Injector", style="bold")
        table.add_column("Injected", justify="right")
        table.add_column("Detected", justify="right")
        table.add_column("Rate", justify="right")
        table.add_column("IQR", justify="right", style="yellow")
        table.add_column("IF", justify="right", style="magenta")
        table.add_column("LOF", justify="right", style="cyan")
        for tag in all_tags:
            injected = self.injector_injected.get(tag, 0)
            detected = self.injector_detected.get(tag, 0)
            rate_val = detected / injected * 100 if injected > 0 else 0
            rate_color = "green" if rate_val >= 70 else "yellow" if rate_val >= 40 else "red"
            rate = f"[{rate_color}]{rate_val:.0f}%[/{rate_color}]" if injected > 0 else "—"
            iqr_count = self.injector_detected_iqr.get(tag, 0)
            if_count = self.injector_detected_if.get(tag, 0)
            lof_count = self.injector_detected_lof.get(tag, 0)
            table.add_row(tag, str(injected), str(detected), rate,
                          str(iqr_count), str(if_count), str(lof_count))

        total_injected = sum(self.injector_injected.values())
        total_detected = sum(self.injector_detected.values())
        total_iqr = sum(self.injector_detected_iqr.values())
        total_if = sum(self.injector_detected_if.values())
        total_lof = sum(self.injector_detected_lof.values())
        total_rate_val = total_detected / total_injected * 100 if total_injected > 0 else 0
        total_color = "green" if total_rate_val >= 70 else "yellow" if total_rate_val >= 40 else "red"
        total_rate = f"[{total_color} bold]{total_rate_val:.0f}%[/{total_color} bold]" if total_injected > 0 else "—"
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]", str(total_injected), str(total_detected),
            total_rate, str(total_iqr), str(total_if), str(total_lof),
        )
        console.print(table)

    def _print_overall_stats(self) -> None:
        total_injected = sum(self.injector_injected.values())
        total_detected = sum(self.injector_detected.values())
        detection_rate = total_detected / total_injected if total_injected > 0 else 0
        precision = self.true_positives / self.total_printed if self.total_printed > 0 else 0
        fp_rate = self.false_positives / self.total_printed if self.total_printed > 0 else 0

        table = Table(title="[bold]Overall Performance[/bold]", show_lines=False, border_style="dim")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_column("", min_width=8)
        table.add_row("Events scored", str(self.total_scored), "")
        table.add_row("Alerts fired", str(self.total_printed), "")
        table.add_row(
            "[green]True positives[/green]",
            f"[green bold]{self.true_positives}[/green bold]",
            "",
        )
        table.add_row(
            "[red]False positives[/red]",
            f"[red bold]{self.false_positives}[/red bold]",
            "",
        )
        table.add_section()
        det_color = "green" if detection_rate >= 0.70 else "yellow" if detection_rate >= 0.50 else "red"
        table.add_row(
            "[bold]Detection rate[/bold]",
            f"[{det_color} bold]{detection_rate:.1%}[/{det_color} bold]",
            f"({total_detected}/{total_injected})",
        )
        prec_color = "green" if precision >= 0.60 else "yellow" if precision >= 0.40 else "red"
        table.add_row(
            "[bold]Precision[/bold]",
            f"[{prec_color} bold]{precision:.1%}[/{prec_color} bold]",
            f"(TP/alerts)",
        )
        fp_color = "green" if fp_rate < 0.40 else "yellow" if fp_rate < 0.60 else "red"
        table.add_row(
            "[bold]FP rate[/bold]",
            f"[{fp_color} bold]{fp_rate:.1%}[/{fp_color} bold]",
            f"(FP/alerts)",
        )
        console.print(table)


def _normalize_tag(tag: str) -> str:
    return tag.split(":")[0] if ":" in tag else tag
