"""
Redis key format utilities. Centralises the key structure so that all modules
parsing or constructing Redis keys for windows and models use one definition.
"""


def parse_window_key(raw_key: bytes | str) -> tuple[str, str, str] | None:
    """Parse a Redis window key into (scope, scope_id, config_type) or None.

    Expected format: ``window:{scope}:{scope_id}:{config_type}``
    """
    decoded = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
    parts = decoded.split(":", 3)
    if len(parts) != 4 or parts[0] != "window":
        return None
    return parts[1], parts[2], parts[3]
