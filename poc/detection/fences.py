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


def clamp_lower_fence(raw_lower: float, p1: float) -> float:
    """Clamp the lower fence at P1 so it never extends below the training distribution.

    Only the lower fence is clamped — this prevents fences from reaching into
    impossible territory for narrow-range features (e.g. jitter_percentage).
    The upper fence uses the raw IQR calculation to avoid guaranteed false
    positives on the top percentile of normal values.
    """
    return max(raw_lower, p1)


def compute_single_feature_fence(
    feature_vectors: list[dict[str, float]],
    feature_name: str,
    iqr_multiplier: float | None = None,
) -> dict:
    """Compute Q1, Q3, IQR, median, and fences for one feature.

    The lower fence is clamped at P1 so it never extends below the bulk of
    the training distribution — this prevents narrow-range features (e.g.
    jitter_percentage 0.10-0.25) from getting fences in impossible territory.
    """
    multiplier = iqr_multiplier if iqr_multiplier is not None else config.IQR_MULTIPLIER
    values = np.array([vector.get(feature_name, 0.0) for vector in feature_vectors])
    p1, q1, median_value, q3 = np.percentile(values, [1, 25, 50, 75])
    iqr = float(q3 - q1)
    raw_lower = float(q1) - multiplier * iqr
    return {
        "Q1": float(q1),
        "Q3": float(q3),
        "IQR": iqr,
        "median": float(median_value),
        "lower_fence": clamp_lower_fence(raw_lower, float(p1)),
        "upper_fence": float(q3) + multiplier * iqr,
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
    active = []
    for name in feature_names:
        values = [vector.get(name, 0.0) for vector in feature_vectors]
        variance = np.var(values)
        if variance >= config.FEATURE_VARIANCE_THRESHOLD:
            active.append(name)
        else:
            logger.debug("Dropping low-variance feature %r (variance=%.6f)", name, variance)
    logger.debug(
        "Feature filter: %d/%d features retained (threshold=%.4f)",
        len(active), len(feature_names), config.FEATURE_VARIANCE_THRESHOLD,
    )
    return active
