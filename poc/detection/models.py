"""
Detection domain models. Pure data structures with no heavy dependencies —
any module can import these without pulling in sklearn or shap.
"""

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict


class Severity(enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


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
class ShapContribution:
    feature_name: str
    contribution: float  # positive = pushes toward anomalous


@dataclass
class FeatureDeviation:
    feature_name: str
    observed_value: float
    expected_median: float
    lower_fence: float
    upper_fence: float
    iqr_deviation: float  # how many IQR units outside the fence


@dataclass
class ScoredEvent:
    telemetry_row: TelemetryRow
    deviating_features: list[FeatureDeviation]
    isolation_forest_score: float
    baseline_used: str     # "implant" or "group"
    shap_contributions: list[ShapContribution] = field(default_factory=list)
