"""
Scoring and retraining pipeline. Extracts features from telemetry rows,
resolves cached models, scores events against IQR fences and Isolation Forest,
and retrains stale baselines.
"""

import json
import logging
import os
import sys

import psycopg2.extensions
import redis as redis_module

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — path setup required before import
from detection.baseline import refresh_baseline
from detection.model_cache import should_retrain
from detection.models import ScoredEvent
from detection.output import print_detections
from detection.queries import discover_window_keys, fetch_all_implants
from detection.scoping import build_model_cache
from detection.scoring import (
    explain_isolation_forest,
    predict_anomaly,
    score_with_iqr,
    score_with_isolation_forest,
    score_with_lof,
)

logger = logging.getLogger(__name__)


def extract_features_from_row(row: dict) -> dict[str, float]:
    """Parse the features column, handling both dict and JSON string formats."""
    if isinstance(row["features"], dict):
        return row["features"]
    return json.loads(row["features"])


def resolve_cached_model(
    row: dict,
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> tuple[str, dict] | None:
    """Look up the baseline scope and cached artefacts for a row. Returns (scope, artefacts_dict) or None."""
    scope_entry = scope_map.get((row["implant_id"], row["config_type"]))
    if scope_entry is None:
        return None
    scope, scope_id = scope_entry
    cached_model = model_cache.get((scope, scope_id, row["config_type"]))
    if cached_model is None:
        return None
    return scope, cached_model


def score_row_with_cache(
    row: dict,
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> ScoredEvent | None:
    """Score one telemetry row using pre-loaded scope map and model cache."""
    features = extract_features_from_row(row)
    resolved = resolve_cached_model(row, scope_map, model_cache)
    if resolved is None:
        return None
    scope, cached_model = resolved

    deviations, _soft_deviation_count = score_with_iqr(features, cached_model["iqr_fences"])
    trained_model = cached_model["trained_model"]
    isolation_forest_score = score_with_isolation_forest(features, trained_model)
    lof_score = score_with_lof(features, trained_model)
    mahalanobis_p_value = trained_model.mahalanobis_p_value(features)
    if_predicts_anomaly, lof_predicts_anomaly = predict_anomaly(features, trained_model)
    any_signal = if_predicts_anomaly or lof_predicts_anomaly or len(deviations) > 0
    shap_contributions = (explain_isolation_forest(features, trained_model)
                          if any_signal else [])
    return ScoredEvent(
        telemetry_row=row, deviating_features=deviations,
        isolation_forest_score=isolation_forest_score, lof_score=lof_score,
        mahalanobis_p_value=mahalanobis_p_value,
        if_predicts_anomaly=if_predicts_anomaly,
        lof_predicts_anomaly=lof_predicts_anomaly,
        baseline_used=scope,
        shap_contributions=shap_contributions,
    )


def try_retrain_baseline(
    redis_connection: redis_module.Redis,
    db_connection: psycopg2.extensions.connection,
    scope: str,
    scope_id: str,
    config_type: str,
) -> Exception | None:
    """Attempt to retrain a single baseline. Returns the exception on failure, None on success."""
    try:
        refresh_baseline(redis_connection, db_connection, scope, scope_id, config_type)
        return None
    except Exception as exception:
        logger.error(
            "Baseline refresh failed for %s:%s:%s", scope, scope_id, config_type,
            exc_info=True,
        )
        return exception


def retrain_stale_baselines(
    db_connection: psycopg2.extensions.connection,
    redis_connection: redis_module.Redis,
) -> None:
    """Check all window keys and retrain any baselines that need refreshing."""
    window_keys = discover_window_keys(redis_connection)
    retrained = 0
    failed_scopes: list[tuple[str, str, str]] = []
    for scope, scope_id, config_type in window_keys:
        if not should_retrain(redis_connection, scope, scope_id, config_type):
            continue
        if try_retrain_baseline(redis_connection, db_connection, scope, scope_id, config_type) is None:
            retrained += 1
        else:
            failed_scopes.append((scope, scope_id, config_type))
    if retrained > 0:
        logger.info("Retrained %d / %d baselines this tick", retrained, len(window_keys))
    if failed_scopes:
        failed_keys = ", ".join(f"{s}:{sid}:{ct}" for s, sid, ct in failed_scopes)
        logger.warning("%d baseline refresh(es) failed: %s", len(failed_scopes), failed_keys)


def try_score_row(
    row: dict,
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> ScoredEvent | None:
    """Score a single row, logging and returning None on failure."""
    try:
        return score_row_with_cache(row, scope_map, model_cache)
    except Exception:
        logger.error("Scoring failed for row id=%s", row.get("id"), exc_info=True)
        return None


def score_all_rows(
    telemetry_rows: list[dict],
    scope_map: dict[tuple, tuple],
    model_cache: dict[tuple, dict | None],
) -> list[ScoredEvent]:
    """Score each telemetry row, returning only successfully scored events."""
    scored_events = []
    failure_ids: list[int | None] = []
    for row in telemetry_rows:
        scored = try_score_row(row, scope_map, model_cache)
        if scored is not None:
            scored_events.append(scored)
        else:
            row_id = row.get("id")
            if row_id is not None:
                failure_ids.append(row_id)
    if failure_ids:
        logger.warning(
            "%d row(s) failed scoring: %s",
            len(failure_ids),
            ", ".join(str(row_id) for row_id in failure_ids),
        )
    return scored_events


def score_telemetry_batch(
    redis_connection: redis_module.Redis,
    telemetry_rows: list[dict],
    implant_registry: dict[str, dict],
) -> None:
    """Score a batch of telemetry rows and print detected anomalies."""
    model_cache, scope_map = build_model_cache(
        redis_connection, telemetry_rows, implant_registry,
    )
    scored_events = score_all_rows(telemetry_rows, scope_map, model_cache)
    detected = print_detections(scored_events)
    logger.info(
        "Scored %d rows, %d detections printed",
        len(telemetry_rows), detected,
    )
