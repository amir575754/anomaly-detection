"""
Anomaly injectors. Each injector takes a clean snapshot, mutates the
configuration to represent a specific operator mistake or risk, tags it with
ground truth, and returns a new snapshot. The original is never modified.
"""

import copy
import random


def _prepare_anomaly(snapshot: dict, injector_tag: str) -> dict:
    """Deep-copy a snapshot and tag it as anomalous. Caller mutates the returned configuration."""
    result = copy.deepcopy(snapshot)
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = injector_tag
    return result


def inject_beacon_storm(snapshot: dict) -> dict:
    """Dramatically reduce beacon interval to simulate aggressive beaconing."""
    original_interval = snapshot["configuration"].get("beacon_interval_ms", 30000)
    new_interval = random.randint(500, 2000)
    result = _prepare_anomaly(snapshot, f"beacon_storm:{original_interval}->{new_interval}")
    result["configuration"]["beacon_interval_ms"] = new_interval
    return result


def inject_zero_jitter(snapshot: dict) -> dict:
    """Set jitter to zero — predictable beacon timing is detectable by defenders."""
    result = _prepare_anomaly(snapshot, "zero_jitter")
    result["configuration"]["jitter_percentage"] = 0.0
    return result


def inject_self_destruct_flood(snapshot: dict) -> dict:
    """Set all dangerous program and driver actions to self_destruct."""
    result = _prepare_anomaly(snapshot, "self_destruct_flood")
    for entry in result["configuration"].get("dangerous_programs", []):
        entry["action"] = "self_destruct"
    for entry in result["configuration"].get("dangerous_drivers", []):
        entry["action"] = "self_destruct"
    return result


def inject_capability_explosion(snapshot: dict) -> dict:
    """Enable all capabilities simultaneously and inflate concurrency limit."""
    result = _prepare_anomaly(snapshot, "capability_explosion")
    for capability in result["configuration"].get("capabilities", []):
        capability["enabled"] = True
    result["configuration"]["max_concurrent_tasks"] = random.randint(20, 50)
    return result


def inject_full_evasion(snapshot: dict) -> dict:
    """Enable every evasion technique at once."""
    result = _prepare_anomaly(snapshot, "full_evasion")
    for key, value in result["configuration"].items():
        if isinstance(value, bool):
            result["configuration"][key] = True
    return result


def inject_persistence_spike(snapshot: dict) -> dict:
    """Inflate registry key count and scheduled task count to unusual levels."""
    result = _prepare_anomaly(snapshot, "persistence_spike")
    result["configuration"]["registry_key_count"] = random.randint(50, 200)
    result["configuration"]["scheduled_task_count"] = random.randint(20, 50)
    return result


# Maps each config type to the injectors that are applicable for that type
_INJECTORS_BY_CONFIG_TYPE = {
    "communication_configuration": [inject_beacon_storm, inject_zero_jitter],
    "dangerous_program_configuration": [inject_self_destruct_flood],
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
