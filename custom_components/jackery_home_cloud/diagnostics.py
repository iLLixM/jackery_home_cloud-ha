"""Diagnostics support for Jackery Home Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
)

TO_REDACT = {
    CONF_ACCOUNT,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_CRYPTO_KEY,
    "account",  # coordinator.data's top-level mirror of CONF_ACCOUNT
    "email",  # app_user payload
    "mqttUserName",
    "mqttPassword",  # raw MQTT broker credential fields
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    data = (
        dict(coordinator.data)
        if coordinator is not None and isinstance(getattr(coordinator, "data", None), dict)
        else {}
    )
    mqtt_credentials = (
        dict(getattr(coordinator, "mqtt_credentials", {}) or {})
        if coordinator is not None
        else {}
    )

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "mqtt": {
            "enabled": bool(entry.options.get(CONF_ENABLE_MQTT, False)),
            "configured_system_id": entry.options.get(CONF_MQTT_SYSTEM_ID),
            "resolved_system_id": getattr(coordinator, "mqtt_system_id", None),
            "resolved_device_serial": getattr(coordinator, "mqtt_device_serial", ""),
            "connection_state": dict(data.get("mqtt_state", {})),
            "broker_config": async_redact_data(mqtt_credentials, TO_REDACT),
        },
        "systems": {
            "selected_system_ids": list(data.get("selected_system_ids", [])),
            "available_system_ids": list(data.get("available_systems", {}).keys()),
        },
    }
