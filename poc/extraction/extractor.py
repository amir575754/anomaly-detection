"""
Feature extraction. Converts a raw configuration dict into a flat
dict[str, float] suitable for statistical and ML-based detection.
Each feature name matches exactly the catalogue in CLAUDE.md.
"""


def extract_features(configuration: dict, config_type: str) -> dict[str, float]:
    """Return a flat feature vector for the given config type."""
    extractors = {
        "dangerous_program_configuration": _extract_dangerous_program,
        "internet_configuration": _extract_internet,
        "communication_configuration": _extract_communication,
        "persistence_configuration": _extract_persistence,
        "capability_configuration": _extract_capability,
        "evasion_configuration": _extract_evasion,
    }
    extractor = extractors.get(config_type)
    if extractor is None:
        raise ValueError(f"No feature extractor for config type: {config_type!r}")
    return extractor(configuration)


def _count_actions(entries: list[dict], action: str) -> int:
    """Count entries with the given action value."""
    return sum(1 for entry in entries if entry["action"] == action)


def _extract_program_features(programs: list[dict], prefix: str) -> dict[str, float]:
    """Extract count and ratio features for a list of program/driver entries."""
    audit_count = _count_actions(programs, "audit")
    self_destruct_count = _count_actions(programs, "self_destruct")
    do_nothing_count = _count_actions(programs, "do_nothing")
    total = len(programs)

    ratio_key = f"{prefix}_self_destruct_ratio" if prefix == "prog" else f"{prefix}_do_nothing_ratio"
    ratio_value = (
        self_destruct_count / total if prefix == "prog" and total > 0
        else do_nothing_count / total if total > 0
        else 0.0
    )

    return {
        f"{prefix}_audit_count": float(audit_count),
        f"{prefix}_self_destruct_count": float(self_destruct_count),
        f"{prefix}_do_nothing_count": float(do_nothing_count),
        ratio_key: ratio_value,
    }


def _extract_dangerous_program(configuration: dict) -> dict[str, float]:
    programs = configuration.get("dangerous_programs", [])
    drivers = configuration.get("dangerous_drivers", [])

    features = {
        "program_count": float(len(programs)),
        "driver_count": float(len(drivers)),
        "total_entries": float(len(programs) + len(drivers)),
    }
    features.update(_extract_program_features(programs, "prog"))
    features.update(_extract_program_features(drivers, "drv"))
    return features


def _extract_internet(configuration: dict) -> dict[str, float]:
    return {
        "is_air_gapped": 1.0 if configuration.get("is_air_gapped") else 0.0,
    }


def _extract_c2_channel_features(channels: list[dict]) -> dict[str, float]:
    """Extract C2 channel features from a list of channel configurations."""
    enabled_channels = [channel for channel in channels if channel.get("enabled")]
    unique_protocols = {channel["protocol"] for channel in channels}
    non_standard_ports = sum(
        1 for channel in channels if channel.get("port") not in (80, 443, 53)
    )
    return {
        "c2_channel_count": float(len(channels)),
        "c2_enabled_count": float(len(enabled_channels)),
        "c2_unique_protocols": float(len(unique_protocols)),
        "c2_non_standard_ports": float(non_standard_ports),
    }


def _extract_communication(configuration: dict) -> dict[str, float]:
    encryption = configuration.get("encryption", {})
    features = {
        "beacon_interval_ms": float(configuration.get("beacon_interval_ms", 0)),
        "jitter_percentage": float(configuration.get("jitter_percentage", 0)),
        "max_retries": float(configuration.get("max_retries", 0)),
        "sleep_on_failure_ms": float(configuration.get("sleep_on_failure_ms", 0)),
        "encryption_enabled": 1.0 if encryption.get("enabled") else 0.0,
        "key_rotation_hours": float(encryption.get("key_rotation_hours", 0)),
    }
    features.update(_extract_c2_channel_features(configuration.get("c2_channels", [])))
    return features


def _extract_persistence(configuration: dict) -> dict[str, float]:
    methods = configuration.get("active_methods", [])
    registry_key_count = configuration.get("registry_key_count", 0)
    scheduled_task_count = configuration.get("scheduled_task_count", 0)

    return {
        "active_method_count": float(len(methods)),
        "registry_key_count": float(registry_key_count),
        "scheduled_task_count": float(scheduled_task_count),
        "watchdog_enabled": 1.0 if configuration.get("watchdog_enabled") else 0.0,
        "reinstall_on_removal": 1.0 if configuration.get("reinstall_on_removal") else 0.0,
        "total_persistence_footprint": float(len(methods) + registry_key_count + scheduled_task_count),
    }


def _count_surveillance_capabilities(enabled_capabilities: list[dict]) -> int:
    """Count enabled capabilities that are surveillance-related."""
    surveillance_names = {"keylogging", "screenshot"}
    return sum(
        1 for capability in enabled_capabilities
        if capability.get("name") in surveillance_names
    )


def _extract_capability(configuration: dict) -> dict[str, float]:
    capabilities = configuration.get("capabilities", [])
    enabled_capabilities = [capability for capability in capabilities if capability.get("enabled")]

    return {
        "capability_count": float(len(capabilities)),
        "enabled_count": float(len(enabled_capabilities)),
        "enabled_ratio": len(enabled_capabilities) / len(capabilities) if capabilities else 0.0,
        "max_concurrent_tasks": float(configuration.get("max_concurrent_tasks", 0)),
        "task_timeout_ms": float(configuration.get("task_timeout_ms", 0)),
        "active_surveillance_count": float(_count_surveillance_capabilities(enabled_capabilities)),
    }


def _extract_evasion(configuration: dict) -> dict[str, float]:
    evasion_flags = [
        "obfuscate_strings", "amsi_bypass_enabled", "etw_patch_enabled",
        "unhook_ntdll", "sleep_obfuscation", "stack_spoof",
    ]
    enabled_count = sum(1 for flag in evasion_flags if configuration.get(flag))

    features = {flag: 1.0 if configuration.get(flag) else 0.0 for flag in evasion_flags}
    features["evasion_enabled_count"] = float(enabled_count)
    features["evasion_enabled_ratio"] = enabled_count / len(evasion_flags)
    return features
