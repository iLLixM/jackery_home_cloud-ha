"""Helpers for normalizing MQTT payloads from Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


def _search_key(value: Any, target_key: str) -> Any:
    """Search a nested mapping/list structure for the first matching key."""
    if isinstance(value, Mapping):
        if target_key in value:
            return value[target_key]
        for child in value.values():
            found = _search_key(child, target_key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _search_key(child, target_key)
            if found is not None:
                return found
    return None


def parse_mqtt_payload(topic: str, payload: bytes) -> dict[str, Any]:
    """Parse a raw MQTT payload into a normalized diagnostic structure."""
    payload_text = payload.decode("utf-8", errors="replace")
    payload_json: Any = None
    try:
        payload_json = json.loads(payload_text)
    except json.JSONDecodeError:
        payload_json = None

    message: dict[str, Any] = {
        "topic": topic,
        "payload_text": payload_text,
        "payload_json": payload_json,
        "gw_sn": None,
        "dev_sn": None,
        "method": None,
    }

    if payload_json is not None:
        message["gw_sn"] = _search_key(payload_json, "gw_sn")
        message["dev_sn"] = _search_key(payload_json, "dev_sn")
        message["method"] = _search_key(payload_json, "method")

    return message
