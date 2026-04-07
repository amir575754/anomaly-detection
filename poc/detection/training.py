"""
Isolation Forest training — pure ML, no I/O.

Handles feature selection, matrix construction, model fitting,
and training score calibration.
"""

import logging
import os
import sys

import numpy as np
import shap
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.fences import filter_low_variance_features
from detection.trained_model import TrainedModel

logger = logging.getLogger(__name__)


def select_active_features(
    feature_vectors: list[dict[str, float]],
) -> list[str]:
    """Pick features with sufficient variance, falling back to all features."""
    all_feature_names = sorted(feature_vectors[0].keys())
    active_features = filter_low_variance_features(feature_vectors, all_feature_names)
    if not active_features:
        logger.warning("All features dropped by variance filter — using all features")
        return all_feature_names
    return active_features


def build_training_matrix(
    feature_vectors: list[dict[str, float]],
    active_features: list[str],
) -> tuple[np.ndarray, StandardScaler]:
    """Build a raw matrix from feature vectors and scale it."""
    raw_matrix = np.array(
        [[vector.get(name, 0.0) for name in active_features] for vector in feature_vectors]
    )
    scaler = StandardScaler()
    matrix = scaler.fit_transform(raw_matrix)
    return matrix, scaler


def fit_isolation_forest(matrix: np.ndarray) -> IsolationForest:
    """Create and fit an IsolationForest on the scaled training matrix."""
    logger.debug(
        "Training IsolationForest: %d samples x %d features, "
        "n_estimators=%d, contamination=%s",
        matrix.shape[0], matrix.shape[1],
        config.ISOLATION_FOREST_ESTIMATORS,
        config.ISOLATION_FOREST_CONTAMINATION,
    )
    model = IsolationForest(
        n_estimators=config.ISOLATION_FOREST_ESTIMATORS,
        contamination=config.ISOLATION_FOREST_CONTAMINATION,
        random_state=config.ISOLATION_FOREST_RANDOM_STATE,
    )
    model.fit(matrix)
    return model


def compute_training_score_stats(
    model: IsolationForest,
    matrix: np.ndarray,
    active_feature_count: int,
) -> tuple[float, float]:
    """Compute mean and std of decision_function scores on the training data."""
    train_scores = model.decision_function(matrix)
    train_score_mean = float(np.mean(train_scores))
    train_score_std = float(np.std(train_scores))
    logger.debug(
        "IsolationForest trained: active_features=%d, "
        "train_score_mean=%.4f, train_score_std=%.4f",
        active_feature_count, train_score_mean, train_score_std,
    )
    return train_score_mean, train_score_std


def train_isolation_forest(feature_vectors: list[dict[str, float]]) -> TrainedModel:
    """Fit an Isolation Forest and return a TrainedModel with all scoring metadata."""
    if not feature_vectors:
        raise ValueError("Cannot train IsolationForest on zero samples")

    active_features = select_active_features(feature_vectors)
    matrix, scaler = build_training_matrix(feature_vectors, active_features)
    model = fit_isolation_forest(matrix)
    train_score_mean, train_score_std = compute_training_score_stats(model, matrix, len(active_features))
    return TrainedModel(
        model=model,
        active_features=active_features,
        scaler=scaler,
        train_score_mean=train_score_mean,
        train_score_std=train_score_std,
        shap_explainer=shap.TreeExplainer(model),
    )
