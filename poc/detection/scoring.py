"""
Scoring functions. Pure computation — takes a feature vector and a trained
model, returns deviation details and anomaly scores. No DB or Redis access.
"""

from __future__ import annotations

import numpy as np

import config
from detection.models import FeatureDeviation, Severity, ShapContribution
from detection.trained_model import TrainedModel


def compute_iqr_deviation(observed_value: float, fence_data: dict) -> float | None:
    """Return the IQR deviation in fence units, or None if the value is within bounds."""
    lower_fence = fence_data["lower_fence"]
    upper_fence = fence_data["upper_fence"]

    if lower_fence <= observed_value <= upper_fence:
        return None

    iqr = fence_data["IQR"]
    if iqr <= 0:
        return config.ZERO_IQR_DEVIATION_SENTINEL

    distance_outside = (
        lower_fence - observed_value
        if observed_value < lower_fence
        else observed_value - upper_fence
    )
    return distance_outside / iqr


def score_with_iqr(features: dict[str, float], fences: dict) -> list[FeatureDeviation]:
    """Check each feature against IQR fences; return the list of deviations."""
    deviations = []
    for feature_name, observed_value in features.items():
        if feature_name not in fences:
            continue
        iqr_units = compute_iqr_deviation(observed_value, fences[feature_name])
        if iqr_units is not None:
            deviations.append(FeatureDeviation(
                feature_name=feature_name,
                observed_value=observed_value,
                expected_median=fences[feature_name]["median"],
                lower_fence=fences[feature_name]["lower_fence"],
                upper_fence=fences[feature_name]["upper_fence"],
                iqr_deviation=iqr_units,
            ))
    return deviations


def score_with_isolation_forest(features: dict[str, float], trained_model: TrainedModel) -> float:
    """
    Return a normalised anomaly score in [0, 1] where 1 is most anomalous.

    Uses z-score normalization against the training data's score distribution,
    mapped through a sigmoid. Normal points cluster around 0.5; genuine
    anomalies that are multiple standard deviations from the training mean
    produce scores approaching 1.0.
    """
    vector = trained_model.build_scaled_vector(features)
    raw_score = trained_model.model.decision_function(vector)[0]

    standard_deviation = max(trained_model.train_score_std, 1e-6)
    z_score = (trained_model.train_score_mean - raw_score) / standard_deviation
    sigmoid = 1.0 / (1.0 + np.exp(-z_score))
    return float(max(0.0, min(1.0, sigmoid)))


def explain_isolation_forest(
    features: dict[str, float],
    trained_model: TrainedModel,
) -> list[ShapContribution]:
    """Compute per-feature SHAP contributions for an Isolation Forest prediction.

    Returns the top contributing features sorted by absolute impact, descending.
    """
    vector = trained_model.build_scaled_vector(features)
    shap_values = trained_model.shap_explainer.shap_values(vector)[0]

    contributions = [
        ShapContribution(feature_name=name, contribution=float(value))
        for name, value in zip(trained_model.active_features, shap_values)
    ]
    contributions.sort(key=lambda c: abs(c.contribution), reverse=True)
    return contributions[:config.SHAP_TOP_N_FEATURES]


def classify_iqr_signals(
    deviating_features: list[FeatureDeviation],
) -> tuple[bool, bool]:
    """Return (has_significant_deviation, has_any_deviation) from IQR results."""
    has_any = len(deviating_features) >= 1
    has_significant = (
        len(deviating_features) >= config.IQR_SIGNIFICANT_FEATURE_COUNT
        or any(d.iqr_deviation > config.IQR_SIGNIFICANT_MULTIPLIER for d in deviating_features)
    )
    return has_significant, has_any


def classify_isolation_forest_signals(score: float) -> tuple[bool, bool]:
    """Return (is_high, is_medium) from the Isolation Forest score."""
    is_high = score > config.ISOLATION_FOREST_HIGH_THRESHOLD
    is_medium = config.ISOLATION_FOREST_MEDIUM_THRESHOLD <= score <= config.ISOLATION_FOREST_HIGH_THRESHOLD
    return is_high, is_medium


def determine_severity(
    deviating_features: list[FeatureDeviation],
    isolation_forest_score: float,
) -> Severity | None:
    """
    Determine detection severity from IQR deviations and IF score.
    Returns None if neither model finds anything worth reporting.
    """
    iqr_significant, iqr_any = classify_iqr_signals(deviating_features)
    if_high, if_medium = classify_isolation_forest_signals(isolation_forest_score)
    if iqr_significant and if_high:
        return Severity.HIGH
    if iqr_any or if_medium:
        return Severity.MEDIUM
    if if_high and not iqr_any:
        return Severity.LOW
    return None
