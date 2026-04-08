"""
IQR fence computation — pure math, no I/O.

Computes per-feature statistical fences (Q1, Q3, IQR, median, lower/upper)
with percentile clamping to prevent fences from extending into impossible
territory for narrow-range features.
"""

import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import

logger = logging.getLogger(__name__)


def compute_single_feature_fence(
    feature_vectors: list[dict[str, float]],
    feature_name: str,
    iqr_multiplier: float | None = None,
) -> dict:
    """Compute Q1, Q3, IQR, median, and fences for one feature.

    When IQR is zero (constant or near-constant features like booleans),
    falls back to MAD (Median Absolute Deviation) scaled to approximate
    the IQR. If MAD is also zero (truly constant feature), uses a tight
    epsilon band around the median so any deviation is caught.
    """
    multiplier = iqr_multiplier if iqr_multiplier is not None else config.IQR_MULTIPLIER
    values = np.array([vector.get(feature_name, 0.0) for vector in feature_vectors])
    p1, q1, median_value, q3, p99 = np.percentile(values, [1, 25, 50, 75, 99])
    iqr = float(q3 - q1)

    if iqr > 0:
        raw_lower = float(q1) - multiplier * iqr
        raw_upper = float(q3) + multiplier * iqr
        lower_fence = max(raw_lower, float(p1))
        upper_fence = min(raw_upper, float(p99))
    else:
        mad = float(np.median(np.abs(values - median_value)))
        if mad > 0:
            pseudo_iqr = mad * 1.4826
            iqr = pseudo_iqr
            lower_fence = float(median_value) - multiplier * pseudo_iqr
            upper_fence = float(median_value) + multiplier * pseudo_iqr
        else:
            val_min = float(np.min(values))
            val_max = float(np.max(values))
            if val_min < val_max:
                # Feature varies in training but IQR and MAD are both 0 —
                # use training range with a small margin as the fence.
                data_range = val_max - val_min
                iqr = data_range
                lower_fence = val_min - config.ZERO_IQR_EPSILON
                upper_fence = val_max + config.ZERO_IQR_EPSILON
            else:
                # Truly constant feature — any deviation is anomalous
                iqr = config.ZERO_IQR_EPSILON
                lower_fence = float(median_value) - config.ZERO_IQR_EPSILON
                upper_fence = float(median_value) + config.ZERO_IQR_EPSILON

    return {
        "Q1": float(q1),
        "Q3": float(q3),
        "IQR": iqr,
        "median": float(median_value),
        "lower_fence": lower_fence,
        "upper_fence": upper_fence,
    }


def compute_iqr_fences(
    feature_vectors: list[dict[str, float]],
    config_type: str | None = None,
) -> dict[str, dict]:
    """Compute per-feature IQR fences, using per-config-type multiplier overrides."""
    if len(feature_vectors) < config.MINIMUM_TRAINING_SAMPLES:
        raise ValueError(
            f"Need at least {config.MINIMUM_TRAINING_SAMPLES} samples; "
            f"got {len(feature_vectors)}"
        )
    iqr_multiplier = config.CONFIG_TYPE_IQR_OVERRIDES.get(config_type) if config_type else None
    feature_names = list(feature_vectors[0].keys())
    logger.debug(
        "Computing IQR fences for %d features across %d samples (multiplier=%s)",
        len(feature_names), len(feature_vectors),
        iqr_multiplier or config.IQR_MULTIPLIER,
    )
    return {
        feature_name: compute_single_feature_fence(
            feature_vectors, feature_name, iqr_multiplier
        )
        for feature_name in feature_names
    }


def filter_low_variance_features(
    feature_vectors: list[dict[str, float]],
    feature_names: list[str],
) -> list[str]:
    """Return only features whose variance exceeds FEATURE_VARIANCE_THRESHOLD."""
    retained_features = []
    for name in feature_names:
        values = [vector.get(name, 0.0) for vector in feature_vectors]
        variance = np.var(values)
        if variance >= config.FEATURE_VARIANCE_THRESHOLD:
            retained_features.append(name)
        else:
            logger.debug("Dropping low-variance feature %r (variance=%.6f)", name, variance)
    logger.debug(
        "Feature filter: %d/%d features retained (threshold=%.4f)",
        len(retained_features), len(feature_names), config.FEATURE_VARIANCE_THRESHOLD,
    )
    return retained_features
