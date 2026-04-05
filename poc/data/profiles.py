"""
Synthetic config snapshot generators. Produces clean (non-anomalous) telemetry
representative of normal implant behaviour. Used by the generator for the
baseline phase and as the base for live-phase events.
"""

import random
from datetime import datetime, timezone

_TYPE_WEIGHTS = {
    "dangerous_program_configuration": 0.15,
    "communication_configuration": 0.25,
    "persistence_configuration": 0.20,
    "capability_configuration": 0.15,
    "evasion_configuration": 0.15,
}

_DANGEROUS_PROGRAMS = [
    "wireshark.exe", "dumpcap.exe", "procmon.exe", "procexp.exe",
    "fiddler.exe", "burpsuite.exe", "x64dbg.exe", "ollydbg.exe",
    "ida64.exe", "ghidra.exe",
]

_DANGEROUS_DRIVERS = [
    "WdFilter.sys", "csagent.sys", "csfalcondrv.sys", "MpFilter.sys",
    "klif.sys", "bdfilter.sys", "aswSP.sys",
]

_ACTIONS = ["audit", "self_destruct", "do_nothing"]

_C2_PROTOCOLS = ["https", "dns", "smb", "http", "tcp"]

_CAPABILITY_NAMES = [
    "file_exfiltration", "keylogging", "screenshot", "process_injection",
    "lateral_movement", "credential_harvesting", "persistence", "network_scan",
]

_PERSISTENCE_METHODS = [
    "registry_run", "scheduled_task", "service_install",
    "startup_folder", "wmi_subscription",
]

_EVASION_FLAGS = [
    "obfuscate_strings", "amsi_bypass_enabled", "etw_patch_enabled",
    "unhook_ntdll", "sleep_obfuscation", "stack_spoof",
]


def _generate_dangerous_program_configuration() -> dict:
    programs = random.sample(_DANGEROUS_PROGRAMS, k=random.randint(2, 5))
    drivers = random.sample(_DANGEROUS_DRIVERS, k=random.randint(1, 4))
    return {
        "dangerous_programs": [
            {"name": program, "action": random.choice(_ACTIONS)}
            for program in programs
        ],
        "dangerous_drivers": [
            {"name": driver, "action": random.choice(_ACTIONS)}
            for driver in drivers
        ],
    }


def _default_port_for_protocol(protocol: str) -> int:
    """Return the default or random port for a given C2 protocol."""
    if protocol == "https":
        return 443
    if protocol == "dns":
        return 53
    return random.choice([80, 443, 8080, 8443])


def _generate_c2_channel(protocol: str) -> dict:
    return {
        "protocol": protocol,
        "enabled": random.random() < 0.7,
        "port": _default_port_for_protocol(protocol),
    }


def _generate_communication_configuration() -> dict:
    protocols = random.sample(_C2_PROTOCOLS, k=random.randint(1, 3))
    return {
        "beacon_interval_ms": random.randint(25000, 35000),
        "jitter_percentage": round(random.uniform(0.10, 0.25), 2),
        "max_retries": random.randint(3, 7),
        "sleep_on_failure_ms": random.randint(5000, 15000),
        "c2_channels": [_generate_c2_channel(protocol) for protocol in protocols],
        "encryption": {"enabled": True, "key_rotation_hours": random.randint(12, 48)},
    }


def _generate_persistence_configuration() -> dict:
    methods = random.sample(_PERSISTENCE_METHODS, k=random.randint(1, 3))
    return {
        "active_methods": methods,
        "registry_key_count": random.randint(1, 5),
        "scheduled_task_count": random.randint(0, 3),
        "watchdog_enabled": random.random() < 0.4,
        "reinstall_on_removal": random.random() < 0.3,
    }


def _generate_capability_configuration() -> dict:
    capabilities = list(_CAPABILITY_NAMES)
    random.shuffle(capabilities)
    return {
        "capabilities": [
            {"name": capability, "enabled": random.random() < 0.4}
            for capability in capabilities
        ],
        "max_concurrent_tasks": random.randint(2, 6),
        "task_timeout_ms": random.randint(30000, 120000),
    }


def _generate_evasion_configuration() -> dict:
    return {flag: random.random() < 0.4 for flag in _EVASION_FLAGS}


_GENERATORS = {
    "dangerous_program_configuration": _generate_dangerous_program_configuration,
    "communication_configuration": _generate_communication_configuration,
    "persistence_configuration": _generate_persistence_configuration,
    "capability_configuration": _generate_capability_configuration,
    "evasion_configuration": _generate_evasion_configuration,
}


def sample_config_type() -> str:
    """Draw a config type proportionally to its type weight."""
    config_types = list(_TYPE_WEIGHTS.keys())
    weights = [_TYPE_WEIGHTS[t] for t in config_types]
    return random.choices(config_types, weights=weights, k=1)[0]


def generate_snapshot(implant_id: str, group_id: str, config_type: str | None = None) -> dict:
    """Generate a single clean config snapshot for an implant."""
    if config_type is None:
        config_type = sample_config_type()
    return {
        "metadata": _build_metadata(implant_id, group_id, config_type),
        "configuration": _GENERATORS[config_type](),
        "ground_truth": {"is_anomaly": False, "injector_tag": None},
    }


def _build_metadata(implant_id: str, group_id: str, config_type: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "implant_id": implant_id,
        "group_id": group_id,
        "received_at": now.strftime("%d-%m-%Y %H:%M:%S"),
        "type": config_type,
    }
