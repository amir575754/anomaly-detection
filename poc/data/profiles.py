"""
Synthetic config snapshot generators. Produces clean (non-anomalous) telemetry
representative of normal implant behaviour. Used by the generator for the
baseline phase and as the base for live-phase events.

Features within each config type are correlated — not independently drawn.
This gives Isolation Forest learnable multivariate structure: normal data
occupies a restricted submanifold, and anomalies that violate the correlations
land off-manifold.
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

_SURVEILLANCE_CAPABILITIES = {"keylogging", "screenshot"}

_PERSISTENCE_METHODS = [
    "registry_run", "scheduled_task", "service_install",
    "startup_folder", "wmi_subscription",
]

_EVASION_FLAGS = [
    "obfuscate_strings", "amsi_bypass_enabled", "etw_patch_enabled",
    "unhook_ntdll", "sleep_obfuscation", "stack_spoof",
]

_POSTURE_ACTIONS = {
    "stealth": "do_nothing",
    "defensive": "self_destruct",
    "monitoring": "audit",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clamp_int(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


# ---------------------------------------------------------------------------
# Dangerous program configuration — shared posture template
# ---------------------------------------------------------------------------

def _pick_posture_action(dominant_action: str) -> str:
    """70% chance of the dominant action, 30% split among the other two."""
    if random.random() < 0.70:
        return dominant_action
    return random.choice([a for a in _ACTIONS if a != dominant_action])


def _generate_dangerous_program_configuration() -> dict:
    posture = random.choices(
        ["stealth", "defensive", "monitoring"],
        weights=[0.35, 0.30, 0.35],
        k=1,
    )[0]
    dominant_action = _POSTURE_ACTIONS[posture]

    programs = random.sample(_DANGEROUS_PROGRAMS, k=random.randint(2, 5))
    drivers = random.sample(_DANGEROUS_DRIVERS, k=random.randint(1, 4))
    return {
        "dangerous_programs": [
            {"name": p, "action": _pick_posture_action(dominant_action)}
            for p in programs
        ],
        "dangerous_drivers": [
            {"name": d, "action": _pick_posture_action(dominant_action)}
            for d in drivers
        ],
    }


# ---------------------------------------------------------------------------
# Communication configuration — beacon-driven manifold
# ---------------------------------------------------------------------------

def _default_port_for_protocol(protocol: str) -> int:
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
    beacon_interval_ms = random.randint(25000, 35000)
    normalized = (beacon_interval_ms - 25000) / 10000.0

    jitter_base = 0.25 - normalized * 0.15
    jitter_percentage = round(_clamp(jitter_base + random.gauss(0, 0.02), 0.10, 0.25), 2)

    max_retries = _clamp_int(3 + normalized * 4 + random.gauss(0, 0.5), 3, 7)
    sleep_on_failure_ms = _clamp_int(
        5000 + normalized * 10000 + random.gauss(0, 1000), 5000, 15000,
    )

    key_rotation_hours = _clamp_int(
        12 + normalized * 36 + random.gauss(0, 3), 12, 48,
    )

    protocols = random.sample(_C2_PROTOCOLS, k=random.randint(1, 3))
    return {
        "beacon_interval_ms": beacon_interval_ms,
        "jitter_percentage": jitter_percentage,
        "max_retries": max_retries,
        "sleep_on_failure_ms": sleep_on_failure_ms,
        "c2_channels": [_generate_c2_channel(protocol) for protocol in protocols],
        "encryption": {"enabled": True, "key_rotation_hours": key_rotation_hours},
    }


# ---------------------------------------------------------------------------
# Persistence configuration — depth-driven correlations
# ---------------------------------------------------------------------------

_DEPTH_REGISTRY_RANGE = {1: (1, 2), 2: (2, 3), 3: (3, 5)}
_DEPTH_TASK_RANGE = {1: (0, 0), 2: (0, 1), 3: (1, 3)}
_DEPTH_WATCHDOG_PROB = {1: 0.10, 2: 0.30, 3: 0.80}
_DEPTH_REINSTALL_PROB = {1: 0.05, 2: 0.20, 3: 0.70}


def _generate_persistence_configuration() -> dict:
    method_count = random.randint(1, 3)
    methods = random.sample(_PERSISTENCE_METHODS, k=method_count)

    reg_low, reg_high = _DEPTH_REGISTRY_RANGE[method_count]
    task_low, task_high = _DEPTH_TASK_RANGE[method_count]

    return {
        "active_methods": methods,
        "registry_key_count": random.randint(reg_low, reg_high),
        "scheduled_task_count": random.randint(task_low, task_high),
        "watchdog_enabled": random.random() < _DEPTH_WATCHDOG_PROB[method_count],
        "reinstall_on_removal": random.random() < _DEPTH_REINSTALL_PROB[method_count],
    }


# ---------------------------------------------------------------------------
# Capability configuration — two-cluster posture (surveillance vs passive)
# ---------------------------------------------------------------------------

def _generate_capability_configuration() -> dict:
    is_surveillance = random.random() < 0.35
    capabilities = list(_CAPABILITY_NAMES)
    random.shuffle(capabilities)

    if is_surveillance:
        enabled_set = _pick_surveillance_capabilities(capabilities)
        concurrency_low = max(2, min(6, len(enabled_set) - 1))
        concurrency_high = max(concurrency_low, min(6, len(enabled_set) + 1))
        max_concurrent_tasks = random.randint(concurrency_low, concurrency_high)
        task_timeout_ms = random.randint(30000, 60000)
    else:
        enabled_set = _pick_passive_capabilities(capabilities)
        max_concurrent_tasks = random.randint(2, max(2, min(4, len(enabled_set) + 1)))
        task_timeout_ms = random.randint(70000, 120000)

    return {
        "capabilities": [
            {"name": cap, "enabled": cap in enabled_set}
            for cap in capabilities
        ],
        "max_concurrent_tasks": max_concurrent_tasks,
        "task_timeout_ms": task_timeout_ms,
    }


def _pick_surveillance_capabilities(capabilities: list[str]) -> set[str]:
    enabled = set()
    for cap in capabilities:
        prob = 0.85 if cap in _SURVEILLANCE_CAPABILITIES else 0.30
        if random.random() < prob:
            enabled.add(cap)
    if len(enabled) < 2:
        enabled.update(random.sample(sorted(_SURVEILLANCE_CAPABILITIES), k=2 - len(enabled)))
    return enabled


def _pick_passive_capabilities(capabilities: list[str]) -> set[str]:
    enabled = set()
    for cap in capabilities:
        prob = 0.10 if cap in _SURVEILLANCE_CAPABILITIES else 0.35
        if random.random() < prob:
            enabled.add(cap)
    return enabled


# ---------------------------------------------------------------------------
# Evasion configuration — layered dependency chain
# ---------------------------------------------------------------------------

def _generate_evasion_configuration() -> dict:
    obfuscate = random.random() < 0.55
    amsi = (obfuscate and random.random() < 0.65) or (not obfuscate and random.random() < 0.08)
    etw = random.random() < 0.50
    unhook = (etw and random.random() < 0.65) or (not etw and random.random() < 0.12)

    layer2_count = sum([amsi, etw, unhook])
    sleep_obf = (layer2_count >= 2 and random.random() < 0.65) or (layer2_count < 2 and random.random() < 0.08)
    stack = (sleep_obf and random.random() < 0.55) or (not sleep_obf and random.random() < 0.05)

    return {
        "obfuscate_strings": obfuscate,
        "amsi_bypass_enabled": amsi,
        "etw_patch_enabled": etw,
        "unhook_ntdll": unhook,
        "sleep_obfuscation": sleep_obf,
        "stack_spoof": stack,
    }


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------

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
