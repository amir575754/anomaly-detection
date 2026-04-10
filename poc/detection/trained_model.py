"""
TrainedModel container. Wraps a trained Isolation Forest, Local Outlier Factor,
and Mahalanobis distance baseline with their scaler, feature metadata,
calibration stats, and SHAP explainer.
"""

from dataclasses import dataclass

import numpy as np
import shap
from scipy import stats as scipy_stats
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler


@dataclass
class TrainedModel:
    """Trained IF + LOF + Mahalanobis ensemble with scoring and explanation support."""
    model: IsolationForest
    lof_model: LocalOutlierFactor
    active_features: list[str]
    scaler: StandardScaler
    sorted_train_scores: np.ndarray
    sorted_lof_scores: np.ndarray
    shap_explainer: shap.TreeExplainer
    mahal_mean: np.ndarray
    mahal_cov_inv: np.ndarray
    mahal_feature_count: int

    def build_scaled_vector(self, features: dict[str, float]) -> np.ndarray:
        """Build a feature vector scaled with the training scaler."""
        raw_vector = np.array([[features.get(name, 0.0) for name in self.active_features]])
        return self.scaler.transform(raw_vector)

    def build_raw_vector(self, features: dict[str, float]) -> np.ndarray:
        """Build an unscaled feature vector for Mahalanobis distance."""
        return np.array([features.get(name, 0.0) for name in self.active_features])

    def percentile_score(self, raw_decision_score: float) -> float:
        """Convert a raw IF decision_function score to a percentile-based anomaly score."""
        rank = int(np.searchsorted(self.sorted_train_scores, raw_decision_score))
        percentile = rank / len(self.sorted_train_scores)
        return 1.0 - percentile

    def lof_percentile_score(self, raw_lof_score: float) -> float:
        """Convert a raw LOF score_samples value to a percentile-based anomaly score."""
        rank = int(np.searchsorted(self.sorted_lof_scores, raw_lof_score))
        percentile = rank / len(self.sorted_lof_scores)
        return 1.0 - percentile

    def mahalanobis_p_value(self, features: dict[str, float]) -> float:
        """Compute the chi-squared p-value from Mahalanobis distance.

        Returns a value in [0, 1] where lower values indicate more anomalous
        observations. Under multivariate normality, Mahalanobis distance
        squared follows a chi-squared distribution with n_features degrees
        of freedom. This gives principled thresholds: p < 0.01 means the
        observation is in the 1% most unusual region.
        """
        raw = self.build_raw_vector(features)
        diff = raw - self.mahal_mean
        mahal_sq = float(diff @ self.mahal_cov_inv @ diff)
        p_value = float(1.0 - scipy_stats.chi2.cdf(mahal_sq, df=self.mahal_feature_count))
        return p_value
