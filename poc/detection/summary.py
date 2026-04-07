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

    def record(self, event: ScoredEvent, severity: Severity) -> None:
        """Record a printed detection."""
        self.total_printed += 1
        self.severity_counts[severity.value] += 1
        row = event.telemetry_row
        self.config_type_counts[row["config_type"]] += 1
        injector_tag = row.get("injector_tag")
        if injector_tag and row.get("is_anomaly"):
            self.injector_counts[injector_tag] += 1

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

        detection_rate = (
            f"{self.total_printed / self.total_scored:.1%}"
            if self.total_scored > 0
            else "N/A"
        )
        console.print(
            f"\n[bold]Overall:[/bold] scored={self.total_scored}  "
            f"detected={self.total_printed}  suppressed={self.total_suppressed}  "
            f"detection_rate={detection_rate}\n"
        )
