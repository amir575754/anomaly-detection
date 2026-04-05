"""
Anomaly injectors. Each injector takes a clean snapshot, mutates the
configuration to represent a specific operator mistake or risk, tags it with
ground truth, and returns a new snapshot. The original is never modified.
"""

import copy
import random


def inject_beacon_storm(snapshot: dict) -> dict:
    """Dramatically reduce beacon interval to simulate aggressive beaconing."""
    result = copy.deepcopy(snapshot)
    original_interval = result["configuration"].get("beacon_interval_ms", 30000)
    result["configuration"]["beacon_interval_ms"] = random.randint(500, 2000)
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = (
        f"beacon_storm:{original_interval}->{result['configuration']['beacon_interval_ms']}"
    )
    return result


def inject_zero_jitter(snapshot: dict) -> dict:
    """Set jitter to zero — predictable beacon timing is detectable by defenders."""
    result = copy.deepcopy(snapshot)
    result["configuration"]["jitter_percentage"] = 0.0
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "zero_jitter"
    return result


def inject_self_destruct_flood(snapshot: dict) -> dict:
    """Set all dangerous program and driver actions to self_destruct."""
    result = copy.deepcopy(snapshot)
    for entry in result["configuration"].get("dangerous_programs", []):
        entry["action"] = "self_destruct"
    for entry in result["configuration"].get("dangerous_drivers", []):
        entry["action"] = "self_destruct"
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "self_destruct_flood"
    return result


def inject_air_gap_flip(snapshot: dict) -> dict:
    """Enable air-gap mode — unusual for an active implant and worth investigating."""
    result = copy.deepcopy(snapshot)
    result["configuration"]["is_air_gapped"] = True
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "air_gap_flip"
    return result


def inject_capability_explosion(snapshot: dict) -> dict:
    """Enable all capabilities simultaneously and inflate concurrency limit."""
    result = copy.deepcopy(snapshot)
    for capability in result["configuration"].get("capabilities", []):
        capability["enabled"] = True
    result["configuration"]["max_concurrent_tasks"] = random.randint(20, 50)
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "capability_explosion"
    return result


def inject_full_evasion(snapshot: dict) -> dict:
    """Enable every evasion technique at once."""
    result = copy.deepcopy(snapshot)
    for key, value in result["configuration"].items():
        if isinstance(value, bool):
            result["configuration"][key] = True
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "full_evasion"
    return result


def inject_persistence_spike(snapshot: dict) -> dict:
    """Inflate registry key count and scheduled task count to unusual levels."""
    result = copy.deepcopy(snapshot)
    result["configuration"]["registry_key_count"] = random.randint(50, 200)
    result["configuration"]["scheduled_task_count"] = random.randint(20, 50)
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = "persistence_spike"
    return result


# Maps each config type to the injectors that are applicable for that type
_INJECTORS_BY_CONFIG_TYPE = {
    "communication_configuration": [inject_beacon_storm, inject_zero_jitter],
    "dangerous_program_configuration": [inject_self_destruct_flood],
    "internet_configuration": [inject_air_gap_flip],
    "capability_configuration": [inject_capability_explosion],
    "evasion_configuration": [inject_full_evasion],
    "persistence_configuration": [inject_persistence_spike],
}


def inject_random_anomaly(snapshot: dict) -> dict:
    """Apply a random applicable anomaly injector. Returns snapshot unchanged if none apply."""
    config_type = snapshot["metadata"]["type"]
    applicable_injectors = _INJECTORS_BY_CONFIG_TYPE.get(config_type)
    if not applicable_injectors:
        return snapshot
    injector = random.choice(applicable_injectors)
    return injector(snapshot)
