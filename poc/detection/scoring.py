"""
Scoring functions. Pure computation — takes a feature vector and a trained
model, returns deviation details and anomaly scores. No DB or Redis access.
"""

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


def score_with_iqr(features: dict[str, float], fences: dict) -> tuple[list[FeatureDeviation], int]:
    """Check each feature against IQR fences.

    Returns (hard_deviations, soft_deviation_count):
    - hard_deviations: features with deviation >= MINIMUM_IQR_DEVIATION
    - soft_deviation_count: count of features with any deviation (including boundary hits)
    """
    hard_deviations = []
    soft_deviation_count = 0
    for feature_name, observed_value in features.items():
        if feature_name not in fences:
            continue
        iqr_units = compute_iqr_deviation(observed_value, fences[feature_name])
        if iqr_units is not None:
            soft_deviation_count += 1
            if iqr_units >= config.MINIMUM_IQR_DEVIATION:
                hard_deviations.append(FeatureDeviation(
                    feature_name=feature_name,
                    observed_value=observed_value,
                    expected_median=fences[feature_name]["median"],
                    lower_fence=fences[feature_name]["lower_fence"],
                    upper_fence=fences[feature_name]["upper_fence"],
                    iqr_deviation=iqr_units,
                ))
    return hard_deviations, soft_deviation_count


def score_with_isolation_forest(features: dict[str, float], trained_model: TrainedModel) -> float:
    """
    Return a percentile-based anomaly score in [0, 1] where 1 is most anomalous.

    Uses the empirical CDF of training scores: a score of 0.95 means the
    observation is more anomalous than 95% of training data. This gives
    exact calibration — threshold 0.95 guarantees ≤5% FP rate on clean data
    drawn from the same distribution.
    """
    vector = trained_model.build_scaled_vector(features)
    raw_score = trained_model.model.decision_function(vector)[0]
    return float(trained_model.percentile_score(raw_score))


def score_with_lof(features: dict[str, float], trained_model: TrainedModel) -> float:
    """Return a percentile-based LOF anomaly score in [0, 1] where 1 is most anomalous."""
    vector = trained_model.build_scaled_vector(features)
    raw_score = trained_model.lof_model.score_samples(vector)[0]
    return float(trained_model.lof_percentile_score(raw_score))


def predict_anomaly(features: dict[str, float], trained_model: TrainedModel) -> tuple[bool, bool]:
    """Use the models' built-in predict() for hard anomaly classification.

    Returns (if_anomalous, lof_anomalous). These are calibrated to the
    contamination rate — with contamination=0.02, roughly 2% of training
    data is classified as anomalous.
    """
    vector = trained_model.build_scaled_vector(features)
    if_pred = trained_model.model.predict(vector)[0] == -1
    lof_pred = trained_model.lof_model.predict(vector)[0] == -1
    return if_pred, lof_pred


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



def determine_severity(
    deviating_features: list[FeatureDeviation],
    if_predicts_anomaly: bool,
    lof_predicts_anomaly: bool,
    mahalanobis_p_value: float = 1.0,
) -> Severity | None:
    """
    Determine detection severity using corroboration between detector families.

    Four independent detector families vote:
    1. IQR (statistical, per-feature) — captured in deviating_features
    2. Isolation Forest predict() — calibrated to contamination rate
    3. LOF predict() — calibrated to contamination rate
    4. Mahalanobis distance — p < 0.01 counts as a vote

    Requiring 2+ families to agree keeps the false positive rate low.
    IQR significant is trusted alone because it requires either multiple
    deviating features or extreme single-feature deviation.
    """
    iqr_significant, iqr_any = classify_iqr_signals(deviating_features)
    mahal_anomalous = mahalanobis_p_value < config.MAHALANOBIS_P_VALUE_MEDIUM
    ml_votes = sum([if_predicts_anomaly, lof_predicts_anomaly, mahal_anomalous])

    # IQR significant + any ML → HIGH
    if iqr_significant and ml_votes >= 1:
        return Severity.HIGH

    # 2+ ML families independently agree → HIGH
    if ml_votes >= 2:
        return Severity.HIGH

    # IQR significant alone → MEDIUM
    if iqr_significant:
        return Severity.MEDIUM

    # IQR any (hard) + one ML vote → MEDIUM (corroboration)
    if iqr_any and ml_votes >= 1:
        return Severity.MEDIUM

    return None
