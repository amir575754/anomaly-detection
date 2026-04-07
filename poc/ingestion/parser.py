"""
Message parsing and batch preparation. Transforms raw Kafka JSON payloads
into rows ready for PostgreSQL and Redis window commands.
"""

import json
import logging
from datetime import datetime, timezone
from typing import NamedTuple

from extraction.extractor import extract_features

logger = logging.getLogger(__name__)


class ImplantRow(NamedTuple):
    """Maps to the implants table: (implant_id, group_id, first_seen, last_seen)."""
    implant_id: str
    group_id: str
    first_seen: datetime
    last_seen: datetime


class TelemetryRow(NamedTuple):
    """Maps to the telemetry table insert columns."""
    implant_id: str
    config_type: str
    received_at: datetime
    raw: str
    features: str
    is_anomaly: bool
    injector_tag: str | None


class WindowCommand(NamedTuple):
    """A Redis rpush command: (key, features_json)."""
    key: str
    features_json: str


def parse_received_at(timestamp_string: str) -> datetime:
    """Parse the DD-MM-YYYY HH:MM:SS[.ff] format used in telemetry metadata."""
    for format_string in ("%d-%m-%Y %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(timestamp_string, format_string).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp format: {timestamp_string!r}")


def build_window_commands(
    implant_id: str, group_id: str, config_type: str, features_json: str,
) -> list[WindowCommand]:
    """Build Redis window push commands for both implant and group scopes."""
    return [
        WindowCommand(f"window:implant:{implant_id}:{config_type}", features_json),
        WindowCommand(f"window:group:{group_id}:{config_type}", features_json),
    ]


def parse_message(
    message: dict,
) -> tuple[ImplantRow, TelemetryRow, list[WindowCommand]]:
    """Extract implant row, telemetry row, and window commands from one message."""
    metadata = message["metadata"]
    implant_id = str(metadata["implant_id"])
    group_id = str(metadata["group_id"])
    config_type = metadata["type"]
    received_at = parse_received_at(metadata["received_at"])
    ground_truth = message.get("ground_truth", {})

    features_json = json.dumps(extract_features(message["configuration"], config_type))
    is_anomaly = bool(ground_truth.get("is_anomaly", False))

    implant_row = ImplantRow(implant_id, group_id, first_seen=received_at, last_seen=received_at)
    telemetry_row = TelemetryRow(
        implant_id, config_type, received_at,
        json.dumps(message), features_json, is_anomaly,
        ground_truth.get("injector_tag"),
    )
    window_commands = build_window_commands(implant_id, group_id, config_type, features_json)
    return implant_row, telemetry_row, window_commands


def deduplicate_implants(implant_map: dict[str, ImplantRow], implant_row: ImplantRow) -> None:
    """Merge an implant row into the map, keeping the earliest first_seen and latest last_seen."""
    existing = implant_map.get(implant_row.implant_id)
    if existing is None:
        implant_map[implant_row.implant_id] = implant_row
    else:
        implant_map[implant_row.implant_id] = ImplantRow(
            implant_id=implant_row.implant_id,
            group_id=implant_row.group_id,
            first_seen=min(existing.first_seen, implant_row.first_seen),
            last_seen=max(existing.last_seen, implant_row.last_seen),
        )


def prepare_batch_rows(
    batch: list[dict],
) -> tuple[list[ImplantRow], list[TelemetryRow], list[WindowCommand]]:
    """
    Parse a batch of raw Kafka messages into rows for PostgreSQL and Redis.
    Returns (implant_rows, telemetry_rows, window_commands).
    """
    implant_map: dict[str, ImplantRow] = {}
    telemetry_rows: list[TelemetryRow] = []
    window_commands: list[WindowCommand] = []

    for message in batch:
        implant_row, telemetry_row, commands = parse_message(message)
        deduplicate_implants(implant_map, implant_row)
        telemetry_rows.append(telemetry_row)
        window_commands.extend(commands)

    implant_rows = list(implant_map.values())
    logger.debug(
        "Prepared batch: %d unique implants, %d telemetry rows, %d window commands",
        len(implant_rows), len(telemetry_rows), len(window_commands),
    )
    return implant_rows, telemetry_rows, window_commands
