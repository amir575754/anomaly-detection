"""
Isolation Forest training — pure ML, no I/O.

Handles feature selection, matrix construction, model fitting,
and training score calibration via empirical CDF.
"""

import logging
import os
import sys

import numpy as np
import shap
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
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
        "n_estimators=%d, contamination=%s, max_features=%s",
        matrix.shape[0], matrix.shape[1],
        config.ISOLATION_FOREST_ESTIMATORS,
        config.ISOLATION_FOREST_CONTAMINATION,
        config.ISOLATION_FOREST_MAX_FEATURES,
    )
    model = IsolationForest(
        n_estimators=config.ISOLATION_FOREST_ESTIMATORS,
        contamination=config.ISOLATION_FOREST_CONTAMINATION,
        max_features=config.ISOLATION_FOREST_MAX_FEATURES,
        random_state=config.ISOLATION_FOREST_RANDOM_STATE,
    )
    model.fit(matrix)
    return model


def fit_local_outlier_factor(matrix: np.ndarray) -> LocalOutlierFactor:
    """Create and fit a LOF model in novelty detection mode."""
    n_neighbors = min(config.LOF_N_NEIGHBORS, matrix.shape[0] - 1)
    logger.debug(
        "Training LOF: %d samples x %d features, n_neighbors=%d",
        matrix.shape[0], matrix.shape[1], n_neighbors,
    )
    model = LocalOutlierFactor(
        n_neighbors=n_neighbors,
        contamination=config.LOF_CONTAMINATION,
        novelty=True,
    )
    model.fit(matrix)
    return model


def compute_sorted_lof_scores(
    model: LocalOutlierFactor,
    matrix: np.ndarray,
) -> np.ndarray:
    """Compute and sort LOF scores on training data for percentile scoring."""
    train_scores = model.score_samples(matrix)
    sorted_scores = np.sort(train_scores)
    logger.debug(
        "LOF calibration: min=%.4f, median=%.4f, max=%.4f (%d samples)",
        sorted_scores[0], np.median(sorted_scores), sorted_scores[-1], len(sorted_scores),
    )
    return sorted_scores


def compute_sorted_train_scores(
    model: IsolationForest,
    matrix: np.ndarray,
) -> np.ndarray:
    """Compute and sort decision_function scores on training data for percentile scoring."""
    train_scores = model.decision_function(matrix)
    sorted_scores = np.sort(train_scores)
    logger.debug(
        "IF calibration: min=%.4f, median=%.4f, max=%.4f (%d samples)",
        sorted_scores[0], np.median(sorted_scores), sorted_scores[-1], len(sorted_scores),
    )
    return sorted_scores


def compute_mahalanobis_params(
    feature_vectors: list[dict[str, float]],
    active_features: list[str],
) -> tuple[np.ndarray, np.ndarray, int]:
    """Compute mean vector and inverse covariance matrix for Mahalanobis distance."""
    raw_matrix = np.array(
        [[vector.get(name, 0.0) for name in active_features] for vector in feature_vectors]
    )
    mean = np.mean(raw_matrix, axis=0)
    cov = np.cov(raw_matrix, rowvar=False)
    # Regularize covariance to prevent singularity on low-variance features
    cov += np.eye(cov.shape[0]) * 1e-6
    cov_inv = np.linalg.inv(cov)
    logger.debug("Mahalanobis: %d features, condition number: %.2e", len(active_features), np.linalg.cond(cov))
    return mean, cov_inv, len(active_features)


def train_isolation_forest(feature_vectors: list[dict[str, float]]) -> TrainedModel:
    """Fit IF + LOF + Mahalanobis ensemble and return a TrainedModel."""
    if not feature_vectors:
        raise ValueError("Cannot train models on zero samples")

    active_features = select_active_features(feature_vectors)
    matrix, scaler = build_training_matrix(feature_vectors, active_features)
    model = fit_isolation_forest(matrix)
    lof_model = fit_local_outlier_factor(matrix)
    sorted_train_scores = compute_sorted_train_scores(model, matrix)
    sorted_lof_scores = compute_sorted_lof_scores(lof_model, matrix)
    mahal_mean, mahal_cov_inv, feature_count = compute_mahalanobis_params(
        feature_vectors, active_features,
    )
    return TrainedModel(
        model=model,
        lof_model=lof_model,
        active_features=active_features,
        scaler=scaler,
        sorted_train_scores=sorted_train_scores,
        sorted_lof_scores=sorted_lof_scores,
        shap_explainer=shap.TreeExplainer(model),
        mahal_mean=mahal_mean,
        mahal_cov_inv=mahal_cov_inv,
        mahal_feature_count=feature_count,
    )
