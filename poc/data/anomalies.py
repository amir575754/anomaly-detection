"""
Anomaly injectors. Each injector takes a clean snapshot, mutates the
configuration to represent a specific operator mistake or risk, tags it with
ground truth, and returns a new snapshot. The original is never modified.
"""

import copy
import random

from data.profiles import (
    _PERSISTENCE_METHODS,
    _generate_communication_configuration,
)


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


# ---------------------------------------------------------------------------
# Multi-feature anomalies — subtle combinations that stay within per-feature
# IQR bounds but occupy rare regions of the joint feature space.  Designed to
# exercise the Isolation Forest detector.
# ---------------------------------------------------------------------------


def inject_wrong_comm_profile(snapshot: dict) -> dict:
    """Replace the comm config wholesale with a fresh random draw.

    Simulates an operator pasting a config template from a different group.
    Every individual feature lands inside the normal generation range, but the
    specific combination won't match this implant's or group's learned baseline.
    """
    result = _prepare_anomaly(snapshot, "wrong_comm_profile")
    result["configuration"] = _generate_communication_configuration()
    return result


def inject_forgotten_operation_teardown(snapshot: dict) -> dict:
    """Leave an active-operation capability set enabled after the op ended.

    The operator was supposed to disable surveillance capabilities and reduce
    concurrency after an operation.  They forgot, so the implant still reports
    with a hot operational posture.
    """
    result = _prepare_anomaly(snapshot, "forgotten_operation_teardown")
    configuration = result["configuration"]

    operation_capabilities = {
        "file_exfiltration", "keylogging", "screenshot", "lateral_movement",
    }
    for capability in configuration.get("capabilities", []):
        capability["enabled"] = capability["name"] in operation_capabilities

    configuration["max_concurrent_tasks"] = random.randint(5, 6)
    configuration["task_timeout_ms"] = random.randint(30000, 45000)
    return result


def inject_duplicated_persistence_setup(snapshot: dict) -> dict:
    """Push every persistence counter to its ceiling simultaneously.

    Simulates the operator running a persistence provisioning script twice,
    or applying two overlapping persistence templates.
    """
    result = _prepare_anomaly(snapshot, "duplicated_persistence_setup")
    configuration = result["configuration"]

    configuration["active_methods"] = list(_PERSISTENCE_METHODS)
    configuration["registry_key_count"] = random.randint(4, 5)
    configuration["scheduled_task_count"] = random.randint(2, 3)
    configuration["watchdog_enabled"] = True
    configuration["reinstall_on_removal"] = True
    return result


def inject_mismatched_escalation_policy(snapshot: dict) -> dict:
    """Apply contradictory response policies to programs vs. drivers.

    The operator loads the wrong response-policy template: programs all get
    set to 'audit' (passive watch) while drivers all get set to 'do_nothing'
    (ignored entirely).  Counts stay within normal range.
    """
    result = _prepare_anomaly(snapshot, "mismatched_escalation_policy")
    configuration = result["configuration"]

    for entry in configuration.get("dangerous_programs", []):
        entry["action"] = "audit"
    for entry in configuration.get("dangerous_drivers", []):
        entry["action"] = "do_nothing"
    return result


# Maps each config type to the injectors that are applicable for that type
_INJECTORS_BY_CONFIG_TYPE = {
    "communication_configuration": [
        inject_beacon_storm, inject_zero_jitter, inject_wrong_comm_profile,
    ],
    "dangerous_program_configuration": [
        inject_self_destruct_flood, inject_mismatched_escalation_policy,
    ],
    "capability_configuration": [
        inject_capability_explosion, inject_forgotten_operation_teardown,
    ],
    "evasion_configuration": [inject_full_evasion],
    "persistence_configuration": [
        inject_persistence_spike, inject_duplicated_persistence_setup,
    ],
}


def inject_random_anomaly(snapshot: dict) -> dict | None:
    """Apply a random applicable anomaly injector. Returns None if no injector applies."""
    config_type = snapshot["metadata"]["type"]
    applicable_injectors = _INJECTORS_BY_CONFIG_TYPE.get(config_type)
    if not applicable_injectors:
        return None
    injector = random.choice(applicable_injectors)
    return injector(snapshot)
