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


def extract_ems_meter_value(
    payload: Mapping[str, Any],
    *,
    device_serial: str,
    meter_id: str,
) -> float | None:
    """Extract a single EMS meter value from a MQTT data_report payload.

    The helper only evaluates MQTT payloads with cmd=data_report and the EMS
    device node matching ``ems_<device_serial>``.
    """
    if not isinstance(payload, Mapping):
        return None

    if str(payload.get("cmd") or "") != "data_report":
        return None

    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None

    dev_list = info.get("dev_list")
    if not isinstance(dev_list, list):
        return None

    expected_dev_sn = f"ems_{str(device_serial).strip()}"
    normalized_meter_id = str(meter_id)

    for dev in dev_list:
        if not isinstance(dev, Mapping):
            continue
        if str(dev.get("dev_sn") or "").strip() != expected_dev_sn:
            continue

        meter_list = dev.get("meter_list")
        if not isinstance(meter_list, list):
            return None

        for item in meter_list:
            if (
                isinstance(item, list)
                and len(item) >= 2
                and str(item[0]) == normalized_meter_id
            ):
                try:
                    return float(item[1])
                except (TypeError, ValueError):
                    return None
        return None

    return None
