"""
Scoring functions. Pure computation — takes a feature vector and a trained
model, returns deviation details and anomaly scores. No DB or Redis access.
"""

import numpy as np

import config
from alerts.models import FeatureDeviation


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
                iqr_multiplier=iqr_units,
            ))
    return deviations


def score_with_isolation_forest(features: dict[str, float], model) -> float:
    """
    Return a normalised anomaly score in [0, 1] where 1 is most anomalous.

    Uses z-score normalization against the training data's score distribution,
    mapped through a sigmoid. Normal points cluster around 0.5; genuine
    anomalies that are multiple standard deviations from the training mean
    produce scores approaching 1.0.
    """
    active_features = model.active_features
    vector = np.array([[features.get(name, 0.0) for name in active_features]])
    raw_score = model.decision_function(vector)[0]

    standard_deviation = max(model.train_score_std, 1e-6)
    z_score = (model.train_score_mean - raw_score) / standard_deviation
    sigmoid = 1.0 / (1.0 + np.exp(-z_score))
    return float(max(0.0, min(1.0, sigmoid)))
