"""
Offline validation of detection accuracy. Generates synthetic baseline data,
trains models, injects anomalies, and measures detection rate and precision
without requiring Kafka/Redis/PostgreSQL.
"""

import sys
import random

sys.path.insert(0, ".")

import config
from collections import Counter
from data.anomalies import inject_random_anomaly
from data.profiles import generate_snapshot, sample_config_type, _TYPE_WEIGHTS
from detection.fences import compute_iqr_fences
from detection.scoring import determine_severity, score_with_iqr
from detection.training import train_isolation_forest
from extraction.extractor import extract_features

random.seed(42)

IMPLANTS = {
    "ALPHA": [f"implant_ALPHA_{i:03d}" for i in range(5)],
    "BRAVO": [f"implant_BRAVO_{i:03d}" for i in range(5)],
    "CHARLIE": [f"implant_CHARLIE_{i:03d}" for i in range(5)],
}

CONFIG_TYPES = list(_TYPE_WEIGHTS.keys())


def generate_training_data(samples_per_type: int = 1000) -> dict[str, list[dict]]:
    """Generate clean training feature vectors per config type."""
    training: dict[str, list[dict]] = {ct: [] for ct in CONFIG_TYPES}
    for group_id, implant_ids in IMPLANTS.items():
        for implant_id in implant_ids:
            for _ in range(samples_per_type // len(IMPLANTS) // 5):
                for config_type in CONFIG_TYPES:
                    snapshot = generate_snapshot(implant_id, group_id, config_type)
                    features = extract_features(snapshot["configuration"], config_type)
                    training[config_type].append(features)
    return training


def train_models(training: dict[str, list[dict]]) -> dict[str, tuple]:
    """Train IQR fences and IF+LOF models per config type."""
    models = {}
    for config_type, vectors in training.items():
        if len(vectors) < config.MINIMUM_TRAINING_SAMPLES:
            print(f"  Skipping {config_type}: only {len(vectors)} samples")
            continue
        fences = compute_iqr_fences(vectors, config_type)
        trained = train_isolation_forest(vectors)
        models[config_type] = (fences, trained)
        print(f"  {config_type}: {len(vectors)} samples, {len(trained.active_features)} features")
    return models


def generate_test_events(count: int = 2000) -> list[dict]:
    """Generate test events: ~96% clean, ~4% anomalous."""
    events = []
    for _ in range(count):
        group_id = random.choice(list(IMPLANTS.keys()))
        implant_id = random.choice(IMPLANTS[group_id])
        config_type = sample_config_type()
        snapshot = generate_snapshot(implant_id, group_id, config_type)

        if random.random() < config.ANOMALY_RATE:
            anomalous = inject_random_anomaly(snapshot)
            if anomalous is not None:
                snapshot = anomalous

        features = extract_features(snapshot["configuration"], snapshot["metadata"]["type"])
        events.append({
            "config_type": snapshot["metadata"]["type"],
            "features": features,
            "is_anomaly": snapshot["ground_truth"]["is_anomaly"],
            "injector_tag": snapshot["ground_truth"]["injector_tag"],
        })
    return events


def score_events(events: list[dict], models: dict[str, tuple]) -> dict:
    """Score all events and collect stats."""
    stats = {
        "total": 0, "injected": 0, "detected": 0,
        "true_positives": 0, "false_positives": 0,
        "missed": 0,
        "injector_injected": Counter(),
        "injector_detected": Counter(),
    }

    for event in events:
        ct = event["config_type"]
        if ct not in models:
            continue
        fences, trained = models[ct]
        features = event["features"]
        stats["total"] += 1

        is_anomaly = event["is_anomaly"]
        tag = event["injector_tag"]
        if is_anomaly and tag:
            norm_tag = tag.split(":")[0] if ":" in tag else tag
            stats["injected"] += 1
            stats["injector_injected"][norm_tag] += 1

        deviations, _soft_count = score_with_iqr(features, fences)
        vec = trained.build_scaled_vector(features)
        if_pred = trained.model.predict(vec)[0] == -1
        lof_pred = trained.lof_model.predict(vec)[0] == -1
        mahal_p = trained.mahalanobis_p_value(features)
        severity = determine_severity(deviations, if_pred, lof_pred, mahal_p)

        if severity is not None:
            if is_anomaly and tag:
                stats["true_positives"] += 1
                stats["detected"] += 1
                norm_tag = tag.split(":")[0] if ":" in tag else tag
                stats["injector_detected"][norm_tag] += 1
            else:
                stats["false_positives"] += 1
        else:
            if is_anomaly and tag:
                stats["missed"] += 1

    return stats


def print_results(stats: dict) -> None:
    total_alerts = stats["true_positives"] + stats["false_positives"]
    detection_rate = stats["detected"] / stats["injected"] if stats["injected"] > 0 else 0
    precision = stats["true_positives"] / total_alerts if total_alerts > 0 else 0
    fp_rate = stats["false_positives"] / total_alerts if total_alerts > 0 else 0

    print(f"\n{'='*60}")
    print(f"  DETECTION VALIDATION RESULTS")
    print(f"{'='*60}")
    print(f"  Events scored:    {stats['total']}")
    print(f"  Anomalies injected: {stats['injected']}")
    print(f"  Alerts fired:     {total_alerts}")
    print(f"  True positives:   {stats['true_positives']}")
    print(f"  False positives:  {stats['false_positives']}")
    print(f"  Missed anomalies: {stats['missed']}")
    print(f"{'─'*60}")
    print(f"  Detection rate:   {detection_rate:.1%} ({stats['detected']}/{stats['injected']})")
    print(f"  Precision:        {precision:.1%}")
    print(f"  FP rate:          {fp_rate:.1%}")
    print(f"{'─'*60}")

    print(f"\n  Per-injector breakdown:")
    all_tags = sorted(
        set(stats["injector_injected"]) | set(stats["injector_detected"]),
        key=lambda t: -stats["injector_injected"].get(t, 0),
    )
    for tag in all_tags:
        injected = stats["injector_injected"].get(tag, 0)
        detected = stats["injector_detected"].get(tag, 0)
        rate = detected / injected * 100 if injected > 0 else 0
        marker = "OK" if rate >= 70 else "WEAK" if rate >= 40 else "MISS"
        print(f"    {tag:40s} {detected:3d}/{injected:3d} = {rate:5.1f}% [{marker}]")
    print(f"{'='*60}")

    goal_det = detection_rate >= 0.70
    goal_fp = fp_rate < 0.40
    print(f"\n  Goal: Detection >= 70%: {'PASS' if goal_det else 'FAIL'}")
    print(f"  Goal: FP rate < 40%:    {'PASS' if goal_fp else 'FAIL'}")


if __name__ == "__main__":
    print("Generating training data...")
    training = generate_training_data(500)
    print("Training models...")
    models = train_models(training)
    print("Generating test events...")
    events = generate_test_events(5000)
    print(f"Test events: {len(events)} total, {sum(1 for e in events if e['is_anomaly'])} anomalous")
    print("Scoring...")
    stats = score_events(events, models)
    print_results(stats)
