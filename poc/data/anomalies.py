"""
Anomaly injectors. Each injector takes a clean snapshot, mutates the
configuration to represent a specific operator mistake or risk, tags it with
ground truth, and returns a new snapshot. The original is never modified.

Multi-feature injectors are designed to violate the feature correlations
built into profiles.py — they produce values within per-feature ranges but
in combinations that never occur in correlated baseline data.
"""

import copy
import random

from data.profiles import (
    _C2_PROTOCOLS,
    _DANGEROUS_DRIVERS,
    _DANGEROUS_PROGRAMS,
    _PERSISTENCE_METHODS,
    _generate_c2_channel,
)


def _prepare_anomaly(snapshot: dict, injector_tag: str) -> dict:
    """Deep-copy a snapshot and tag it as anomalous. Caller mutates the returned configuration."""
    result = copy.deepcopy(snapshot)
    result["ground_truth"]["is_anomaly"] = True
    result["ground_truth"]["injector_tag"] = injector_tag
    return result


# ---------------------------------------------------------------------------
# Single-feature injectors — caught by IQR
# ---------------------------------------------------------------------------


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
    """Set all dangerous program and driver actions to self_destruct
    and add extra entries — operator panic-loaded a blanket kill list."""
    result = _prepare_anomaly(snapshot, "self_destruct_flood")
    for entry in result["configuration"].get("dangerous_programs", []):
        entry["action"] = "self_destruct"
    for entry in result["configuration"].get("dangerous_drivers", []):
        entry["action"] = "self_destruct"
    extra_programs = random.sample(
        [p for p in _DANGEROUS_PROGRAMS if p not in
         {e["name"] for e in result["configuration"].get("dangerous_programs", [])}],
        k=min(3, len(_DANGEROUS_PROGRAMS) - len(result["configuration"].get("dangerous_programs", []))),
    )
    for name in extra_programs:
        result["configuration"]["dangerous_programs"].append(
            {"name": name, "action": "self_destruct"}
        )
    return result


def inject_capability_explosion(snapshot: dict) -> dict:
    """Enable all capabilities simultaneously and inflate concurrency limit."""
    result = _prepare_anomaly(snapshot, "capability_explosion")
    for capability in result["configuration"].get("capabilities", []):
        capability["enabled"] = True
    result["configuration"]["max_concurrent_tasks"] = random.randint(20, 50)
    return result


def inject_full_evasion(snapshot: dict) -> dict:
    """Enable every evasion technique at once — operator loaded a
    max-stealth template without understanding the dependency chain.

    Also injects impossible dependency violations: layer-3 techniques
    enabled while their layer-1 prerequisites are disabled, which
    produces dependency_coherence anomalies that never occur in
    correlated baseline data.
    """
    result = _prepare_anomaly(snapshot, "full_evasion")
    # Enable ONLY the top-layer techniques while disabling all foundations.
    # This produces:
    # - dependency_coherence = 0.0 (no enabled technique has its prereqs met)
    # - evasion_layer_depth = 3 with evasion_enabled_count = 2
    # In baseline data, layer depth 3 always has count >= 3 (prerequisites).
    result["configuration"]["obfuscate_strings"] = False
    result["configuration"]["amsi_bypass_enabled"] = False
    result["configuration"]["etw_patch_enabled"] = False
    result["configuration"]["unhook_ntdll"] = False
    result["configuration"]["sleep_obfuscation"] = random.choice([True, False])
    result["configuration"]["stack_spoof"] = True
    return result


def inject_persistence_spike(snapshot: dict) -> dict:
    """Inflate registry key count and scheduled task count to unusual levels."""
    result = _prepare_anomaly(snapshot, "persistence_spike")
    result["configuration"]["registry_key_count"] = random.randint(50, 200)
    result["configuration"]["scheduled_task_count"] = random.randint(20, 50)
    return result


# ---------------------------------------------------------------------------
# Multi-feature injectors — violate correlations, caught by IF
# ---------------------------------------------------------------------------


def inject_wrong_comm_profile(snapshot: dict) -> dict:
    """Operator pastes a config template from a different campaign.

    Uses a high beacon interval with low jitter AND high retries — in
    baseline data, high beacon → low retries and high jitter. Also
    pushes c2 channel count and key rotation to extreme values.
    """
    result = _prepare_anomaly(snapshot, "wrong_comm_profile")
    protocols = random.sample(_C2_PROTOCOLS, k=random.randint(3, 5))
    result["configuration"] = {
        "beacon_interval_ms": random.randint(33000, 35000),
        "jitter_percentage": round(random.uniform(0.10, 0.13), 2),
        "max_retries": random.randint(6, 7),
        "sleep_on_failure_ms": random.randint(5000, 7000),
        "c2_channels": [_generate_c2_channel(p) for p in protocols],
        "encryption": {"enabled": True, "key_rotation_hours": random.randint(12, 18)},
    }
    return result


def inject_forgotten_operation_teardown(snapshot: dict) -> dict:
    """Operator forgot to disable the hot surveillance posture after an op.

    Violates the surveillance↔resource correlation: ALL capabilities are
    enabled (full hot posture) but resource settings are pushed to extreme
    passive values — very low concurrency and very long timeouts.
    """
    result = _prepare_anomaly(snapshot, "forgotten_operation_teardown")
    configuration = result["configuration"]

    for capability in configuration.get("capabilities", []):
        capability["enabled"] = True

    configuration["max_concurrent_tasks"] = 1
    configuration["task_timeout_ms"] = random.randint(150000, 200000)
    return result


def inject_duplicated_persistence_setup(snapshot: dict) -> dict:
    """Operator's persistence template partially failed — heavy safety
    infrastructure wrapping minimal actual persistence.

    Violates depth-driven correlations: method_count=1 (depth 1) paired with
    high registry/task counts and both safety nets enabled (depth 3 behavior).
    """
    result = _prepare_anomaly(snapshot, "duplicated_persistence_setup")
    configuration = result["configuration"]

    configuration["active_methods"] = random.sample(_PERSISTENCE_METHODS, k=1)
    configuration["registry_key_count"] = random.randint(4, 5)
    configuration["scheduled_task_count"] = random.randint(2, 3)
    configuration["watchdog_enabled"] = True
    configuration["reinstall_on_removal"] = True
    return result


def inject_mismatched_escalation_policy(snapshot: dict) -> dict:
    """Operator loads contradictory response-policy templates for programs
    vs drivers.

    Violates cross-group posture consistency: ALL programs get audit while
    ALL drivers get do_nothing. In baseline, both groups share the same
    dominant action. Also inflates the driver list beyond normal counts.
    """
    result = _prepare_anomaly(snapshot, "mismatched_escalation_policy")
    configuration = result["configuration"]

    for entry in configuration.get("dangerous_programs", []):
        entry["action"] = "audit"
    for entry in configuration.get("dangerous_drivers", []):
        entry["action"] = "do_nothing"

    extra_drivers = random.sample(
        [d for d in _DANGEROUS_DRIVERS if d not in
         {e["name"] for e in configuration.get("dangerous_drivers", [])}],
        k=min(2, len(_DANGEROUS_DRIVERS) - len(configuration.get("dangerous_drivers", []))),
    )
    for name in extra_drivers:
        configuration["dangerous_drivers"].append(
            {"name": name, "action": "do_nothing"}
        )
    return result


# ---------------------------------------------------------------------------
# Injector dispatch
# ---------------------------------------------------------------------------

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
