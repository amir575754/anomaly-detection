"""
Scoring functions. Pure computation — takes a feature vector and a trained
model, returns deviation details and anomaly scores. No DB or Redis access.
"""

from __future__ import annotations

import numpy as np

import config
from alerts.models import FeatureDeviation, ShapContribution
from detection.models import TrainedModel


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
