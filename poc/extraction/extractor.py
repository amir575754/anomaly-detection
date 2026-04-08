"""
Feature extraction. Converts a raw configuration dict into a flat
dict[str, float] suitable for statistical and ML-based detection.

Features are designed to capture operational semantics (posture consistency,
dependency coherence, resource allocation ratios) rather than raw counts
or binary flags that produce noisy signals for IQR and Isolation Forest.
"""


def extract_features(configuration: dict, config_type: str) -> dict[str, float]:
    """Return a flat feature vector for the given config type."""
    extractors = {
        "dangerous_program_configuration": _extract_dangerous_program,
        "communication_configuration": _extract_communication,
        "persistence_configuration": _extract_persistence,
        "capability_configuration": _extract_capability,
        "evasion_configuration": _extract_evasion,
    }
    extractor = extractors.get(config_type)
    if extractor is None:
        raise ValueError(f"No feature extractor for config type: {config_type!r}")
    return extractor(configuration)


# ---------------------------------------------------------------------------
# Dangerous program configuration — posture ratios + consistency
# ---------------------------------------------------------------------------

def _action_counts(entries: list[dict]) -> tuple[int, int, int]:
    """Return (audit_count, self_destruct_count, do_nothing_count) for a list of entries."""
    audit = sum(1 for e in entries if e["action"] == "audit")
    self_destruct = sum(1 for e in entries if e["action"] == "self_destruct")
    do_nothing = len(entries) - audit - self_destruct
    return audit, self_destruct, do_nothing


def _action_ratios(entries: list[dict]) -> tuple[float, float, float]:
    """Return (audit_ratio, self_destruct_ratio, do_nothing_ratio) for a list of entries."""
    total = len(entries)
    if total == 0:
        return 0.0, 0.0, 0.0
    audit, self_destruct, do_nothing = _action_counts(entries)
    return audit / total, self_destruct / total, do_nothing / total


def _posture_consistency(prog_ratios: tuple, drv_ratios: tuple) -> float:
    """L1 similarity between program and driver action distributions.

    Returns 1.0 when both groups have identical action ratios, 0.0 when
    completely opposite (e.g., all-audit programs vs all-do_nothing drivers).
    """
    l1_distance = sum(abs(p - d) for p, d in zip(prog_ratios, drv_ratios))
    return 1.0 - l1_distance / 2.0


def _extract_dangerous_program(configuration: dict) -> dict[str, float]:
    programs = configuration.get("dangerous_programs", [])
    drivers = configuration.get("dangerous_drivers", [])

    prog_audit, prog_self_destruct, prog_do_nothing = _action_counts(programs)
    drv_audit, drv_self_destruct, drv_do_nothing = _action_counts(drivers)
    prog_ratios = _action_ratios(programs)
    drv_ratios = _action_ratios(drivers)

    total = len(programs) + len(drivers)
    overall_audit = prog_audit + drv_audit
    overall_self_destruct = prog_self_destruct + drv_self_destruct
    overall_do_nothing = prog_do_nothing + drv_do_nothing

    all_entries = programs + drivers
    distinct_actions = len({e["action"] for e in all_entries}) if all_entries else 0
    dominant_count = max(overall_audit, overall_self_destruct, overall_do_nothing) if total else 0

    return {
        "program_count": float(len(programs)),
        "driver_count": float(len(drivers)),
        "total_entries": float(total),
        "prog_audit_count": float(prog_audit),
        "prog_self_destruct_count": float(prog_self_destruct),
        "prog_do_nothing_count": float(prog_do_nothing),
        "drv_audit_count": float(drv_audit),
        "drv_self_destruct_count": float(drv_self_destruct),
        "drv_do_nothing_count": float(drv_do_nothing),
        "overall_audit_ratio": overall_audit / total if total else 0.0,
        "overall_self_destruct_ratio": overall_self_destruct / total if total else 0.0,
        "overall_do_nothing_ratio": overall_do_nothing / total if total else 0.0,
        "posture_consistency": _posture_consistency(prog_ratios, drv_ratios),
        "action_diversity": float(distinct_actions),
        "dominant_action_ratio": dominant_count / total if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Communication configuration — beacon exposure score
# ---------------------------------------------------------------------------

def _extract_c2_channel_features(channels: list[dict]) -> dict[str, float]:
    enabled_channels = [ch for ch in channels if ch.get("enabled")]
    unique_protocols = {ch["protocol"] for ch in channels}
    non_standard_ports = sum(
        1 for ch in channels if ch.get("port") not in (80, 443, 53)
    )
    return {
        "c2_channel_count": float(len(channels)),
        "c2_enabled_count": float(len(enabled_channels)),
        "c2_unique_protocols": float(len(unique_protocols)),
        "c2_non_standard_ports": float(non_standard_ports),
    }


def _extract_communication(configuration: dict) -> dict[str, float]:
    encryption = configuration.get("encryption", {})

    beacon = float(configuration.get("beacon_interval_ms", 0))
    jitter = float(configuration.get("jitter_percentage", 0))
    retries = float(configuration.get("max_retries", 0))
    sleep_ms = float(configuration.get("sleep_on_failure_ms", 0))
    rotation = float(encryption.get("key_rotation_hours", 0))

    features = {
        "beacon_interval_ms": beacon,
        "jitter_percentage": jitter,
        "max_retries": retries,
        "sleep_on_failure_ms": sleep_ms,
        "key_rotation_hours": rotation,
        "beacon_to_sleep_ratio": beacon / max(1.0, sleep_ms),
        "beacon_to_rotation_ratio": beacon / max(1.0, rotation),
        "jitter_retries_product": jitter * retries,
    }
    features.update(_extract_c2_channel_features(configuration.get("c2_channels", [])))
    return features


# ---------------------------------------------------------------------------
# Persistence configuration — safety-to-depth ratio
# ---------------------------------------------------------------------------

def _extract_persistence(configuration: dict) -> dict[str, float]:
    methods = configuration.get("active_methods", [])
    method_count = len(methods)
    registry_key_count = configuration.get("registry_key_count", 0)
    scheduled_task_count = configuration.get("scheduled_task_count", 0)
    watchdog = 1.0 if configuration.get("watchdog_enabled") else 0.0
    reinstall = 1.0 if configuration.get("reinstall_on_removal") else 0.0

    return {
        "active_method_count": float(method_count),
        "registry_key_count": float(registry_key_count),
        "scheduled_task_count": float(scheduled_task_count),
        "watchdog_enabled": watchdog,
        "reinstall_on_removal": reinstall,
        "total_persistence_footprint": float(method_count + registry_key_count + scheduled_task_count),
        "safety_to_depth_ratio": (watchdog + reinstall) / max(1, method_count),
    }


# ---------------------------------------------------------------------------
# Capability configuration — resource-per-capability ratio
# ---------------------------------------------------------------------------

_SURVEILLANCE_CAPABILITIES = {"keylogging", "screenshot"}


def _extract_capability(configuration: dict) -> dict[str, float]:
    capabilities = configuration.get("capabilities", [])
    enabled = [c for c in capabilities if c.get("enabled")]
    enabled_count = len(enabled)
    max_tasks = float(configuration.get("max_concurrent_tasks", 0))
    timeout = float(configuration.get("task_timeout_ms", 0))
    surveillance_count = sum(
        1 for c in enabled if c.get("name") in _SURVEILLANCE_CAPABILITIES
    )

    non_surveillance_enabled = enabled_count - surveillance_count

    return {
        "enabled_count": float(enabled_count),
        "enabled_ratio": enabled_count / len(capabilities) if capabilities else 0.0,
        "max_concurrent_tasks": max_tasks,
        "task_timeout_ms": timeout,
        "active_surveillance_count": float(surveillance_count),
        "non_surveillance_enabled_count": float(non_surveillance_enabled),
        "resource_per_capability": max_tasks / max(1, enabled_count),
        "surveillance_ratio": surveillance_count / max(1, enabled_count),
        "concurrency_timeout_product": max_tasks * timeout / 1000.0,
    }


# ---------------------------------------------------------------------------
# Evasion configuration — layer depth + dependency coherence
# ---------------------------------------------------------------------------

_EVASION_DEPENDENCIES = {
    "amsi_bypass_enabled": ["obfuscate_strings"],
    "unhook_ntdll": ["etw_patch_enabled"],
    "sleep_obfuscation": ["amsi_bypass_enabled", "etw_patch_enabled", "unhook_ntdll"],
    "stack_spoof": ["sleep_obfuscation"],
}

_EVASION_LAYERS = {
    "obfuscate_strings": 1,
    "amsi_bypass_enabled": 2,
    "etw_patch_enabled": 2,
    "unhook_ntdll": 2,
    "sleep_obfuscation": 3,
    "stack_spoof": 3,
}

_EVASION_FLAGS = [
    "obfuscate_strings", "amsi_bypass_enabled", "etw_patch_enabled",
    "unhook_ntdll", "sleep_obfuscation", "stack_spoof",
]


def _evasion_layer_depth(configuration: dict) -> int:
    """Return the highest dependency layer with any enabled technique (0-3)."""
    max_layer = 0
    for flag, layer in _EVASION_LAYERS.items():
        if configuration.get(flag):
            max_layer = max(max_layer, layer)
    return max_layer


def _dependency_coherence(configuration: dict) -> float:
    """Fraction of enabled techniques whose prerequisites are also enabled.

    Returns 1.0 when all dependencies are satisfied (normal), lower values
    when techniques are enabled without their prerequisites (misconfiguration).
    Techniques with no dependencies always count as coherent.
    """
    enabled_with_deps = 0
    satisfied = 0
    for flag in _EVASION_FLAGS:
        if not configuration.get(flag):
            continue
        prerequisites = _EVASION_DEPENDENCIES.get(flag)
        if prerequisites is None:
            continue
        enabled_with_deps += 1
        if any(configuration.get(prereq) for prereq in prerequisites):
            satisfied += 1
    if enabled_with_deps == 0:
        return 1.0
    return satisfied / enabled_with_deps


def _extract_evasion(configuration: dict) -> dict[str, float]:
    enabled_count = sum(1 for flag in _EVASION_FLAGS if configuration.get(flag))
    layer_depth = _evasion_layer_depth(configuration)
    features = {
        "evasion_enabled_count": float(enabled_count),
        "evasion_enabled_ratio": enabled_count / len(_EVASION_FLAGS),
        "evasion_layer_depth": float(layer_depth),
        "dependency_coherence": _dependency_coherence(configuration),
        "depth_per_enabled": float(layer_depth) / max(1, enabled_count),
    }
    for flag in _EVASION_FLAGS:
        features[flag] = 1.0 if configuration.get(flag) else 0.0
    return features
