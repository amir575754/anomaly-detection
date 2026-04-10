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
    group_id: str       # joined from implants table at query time
    config_type: str
    received_at: datetime
    features: dict[str, float] | str
    is_anomaly: bool
    injector_tag: str | None
    ingested_at: datetime


@dataclass
class ShapContribution:
    feature_name: str
    # Sign convention matches Isolation Forest's decision_function: lower =
    # more anomalous, so negative SHAP contributions push the decision toward
    # anomalous and positive contributions push it toward normal. Consumers
    # (output rendering, summary counting) rely on this convention — do not
    # flip without also flipping output.py and summary.py.
    contribution: float


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
    lof_score: float
    mahalanobis_p_value: float
    if_predicts_anomaly: bool
    lof_predicts_anomaly: bool
    baseline_used: str     # "implant" or "group"
    shap_contributions: list[ShapContribution] = field(default_factory=list)
    # Severity is computed once during scoring so downstream consumers
    # (output rendering, summary counting) don't recompute it. None means
    # "below the alert threshold" — not an anomaly to report.
    severity: Severity | None = None
