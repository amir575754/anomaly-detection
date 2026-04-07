"""
Detection summary tracker. Accumulates stats across a scoring batch
and prints a rich summary table on demand.
"""

from collections import Counter

from rich.console import Console
from rich.table import Table

from detection.models import ScoredEvent, Severity

console = Console()


class DetectionSummary:
    """Tracks detection statistics for a single scoring batch."""

    def __init__(self) -> None:
        self.severity_counts: Counter[str] = Counter()
        self.injector_counts: Counter[str] = Counter()
        self.config_type_counts: Counter[str] = Counter()
        self.total_scored: int = 0
        self.total_printed: int = 0
        self.total_suppressed: int = 0
        self.true_positives: int = 0
        self.false_positives: int = 0

    def record(self, event: ScoredEvent, severity: Severity) -> None:
        """Record a printed detection."""
        self.total_printed += 1
        self.severity_counts[severity.value] += 1
        row = event.telemetry_row
        self.config_type_counts[row["config_type"]] += 1
        if row.get("is_anomaly") and row.get("injector_tag"):
            self.true_positives += 1
            injector_tag = row["injector_tag"]
            normalized = injector_tag.split(":")[0] if ":" in injector_tag else injector_tag
            self.injector_counts[normalized] += 1
        else:
            self.false_positives += 1

    def record_suppressed(self) -> None:
        self.total_suppressed += 1

    def print_summary(self) -> None:
        """Print a rich summary of all tracked detections."""
        if self.total_printed == 0 and self.total_suppressed == 0:
            return

        console.rule("[bold]Detection Summary[/bold]")

        severity_table = Table(title="Severity Breakdown", show_lines=False)
        severity_table.add_column("Severity", style="bold")
        severity_table.add_column("Count", justify="right")
        for level in ("HIGH", "MEDIUM", "LOW"):
            count = self.severity_counts.get(level, 0)
            color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "cyan"}[level]
            severity_table.add_row(f"[{color}]{level}[/{color}]", str(count))

        config_table = Table(title="Config Type Breakdown", show_lines=False)
        config_table.add_column("Config Type", style="bold")
        config_table.add_column("Count", justify="right")
        for config_type, count in self.config_type_counts.most_common():
            config_table.add_row(config_type, str(count))

        console.print(severity_table)
        console.print(config_table)

        if self.injector_counts:
            injector_table = Table(title="Top Injectors Detected", show_lines=False)
            injector_table.add_column("Injector Tag", style="bold")
            injector_table.add_column("Count", justify="right")
            for tag, count in self.injector_counts.most_common(10):
                injector_table.add_row(tag, str(count))
            console.print(injector_table)

        stats_table = Table(title="Overall Stats", show_lines=False)
        stats_table.add_column("Metric", style="bold")
        stats_table.add_column("Value", justify="right")
        stats_table.add_row("Scored", str(self.total_scored))
        stats_table.add_row("Detected", str(self.total_printed))
        stats_table.add_row("Suppressed (IF-only MEDIUM)", str(self.total_suppressed))
        stats_table.add_row("[green]True positives[/green]", f"[green]{self.true_positives}[/green]")
        stats_table.add_row("[red]False positives[/red]", f"[red]{self.false_positives}[/red]")
        if self.total_printed > 0:
            fp_rate = self.false_positives / self.total_printed
            stats_table.add_row("FP rate", f"{fp_rate:.1%}")
        console.print(stats_table)
