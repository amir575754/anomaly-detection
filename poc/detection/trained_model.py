"""
TrainedModel container. Wraps a trained Isolation Forest with its scaler,
feature metadata, calibration stats, and SHAP explainer. Separated from
models.py to avoid forcing sklearn/shap imports on lightweight consumers.
"""

from dataclasses import dataclass

import numpy as np
import shap
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler


@dataclass
class TrainedModel:
    """A trained Isolation Forest with all metadata needed for scoring and explanation."""
    model: IsolationForest
    active_features: list[str]
    scaler: StandardScaler
    train_score_mean: float
    train_score_std: float
    shap_explainer: shap.TreeExplainer

    def build_scaled_vector(self, features: dict[str, float]) -> np.ndarray:
        """Build a feature vector scaled with the training scaler."""
        raw_vector = np.array([[features.get(name, 0.0) for name in self.active_features]])
        return self.scaler.transform(raw_vector)
