import enum
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


class Severity(enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Label(enum.Enum):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    EXPECTED_DEVIATION = "expected_deviation"


class TelemetryRow(TypedDict):
    id: int
    implant_id: str
    group_id: str
    config_type: str
    received_at: datetime
    features: dict[str, float] | str
    is_anomaly: bool
    injector_tag: str | None
    ingested_at: datetime


@dataclass
class FeatureDeviation:
    feature_name: str
    observed_value: float
    expected_median: float
    lower_fence: float
    upper_fence: float
    iqr_multiplier: float  # how many IQR units outside the fence


@dataclass
class Alert:
    alert_id: str          # UUID as string
    implant_id: str
    group_id: str
    config_type: str
    timestamp: datetime
    window_start: datetime
    window_end: datetime
    severity: Severity
    baseline_used: str     # "implant" or "group"
    deviating_features: list[FeatureDeviation]
    isolation_forest_score: float
    explanation: str
    label: Label | None = None


@dataclass
class ScoredEvent:
    telemetry_row: TelemetryRow
    deviating_features: list[FeatureDeviation]
    isolation_forest_score: float
    baseline_used: str     # "implant" or "group"
