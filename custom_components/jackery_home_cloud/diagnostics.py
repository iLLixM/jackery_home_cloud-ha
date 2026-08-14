"""Diagnostics support for Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Mapping
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
    MODEL_CAPABILITIES,
)
from .coordinator import _safe_get
from .mqtt_registry import build_default_subscriptions

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

# The 6 meters covered by discussion #6 items 10/11 ("Validate MQTT values
# against REST" / "Validate AC main power sign handling"). mqtt_key and
# rest_path match exactly what sensor.py's _mqtt_or_rest() reads for the
# same sensor, so this block can never disagree with what's shown on the
# dashboard.
_PROTOCOL_VALIDATION_METERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("grid_power", "grid_power_mqtt", ("monitor", "energyFlowChartVO", "gridVO", "gridPower")),
    ("ac_main_power", "ac_main_power_mqtt", ("monitor", "energyFlowChartVO", "acMainVO", "acMainPower")),
    ("battery_soc", "battery_soc_mqtt", ("monitor", "energyFlowChartVO", "emsGwVO", "soc")),
    ("pv_power", "pv_power_mqtt", ("monitor", "energyFlowChartVO", "pvInfo", "pvPower")),
    ("eps_load_power", "eps_load_power_mqtt", ("monitor", "energyFlowChartVO", "acInfo", "epsLoadPower")),
    ("other_load_power", "other_load_power_mqtt", ("monitor", "energyFlowChartVO", "otherLoadVO", "otherLoadPower")),
)


def _protocol_validation(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Side-by-side MQTT vs REST values for the 6 meters covered by
    discussion #6 items 10/11 - observational only, no pass/fail verdict.
    A tolerance threshold would itself be an unconfirmed guess (see
    CONTRIBUTING.md #1/#9); compare the two values by eye instead.
    """
    return {
        key: {"mqtt": bundle.get(mqtt_key), "rest": _safe_get(bundle, *rest_path)}
        for key, mqtt_key, rest_path in _PROTOCOL_VALIDATION_METERS
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    mqtt_client = getattr(runtime, "mqtt_client", None)
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
    mqtt_system = getattr(coordinator, "mqtt_system", None)
    resolved_system_id = mqtt_system.system_id if mqtt_system is not None else None
    resolved_device_serial = mqtt_system.device_serial if mqtt_system is not None else ""

    detected_model = None
    capability_source = None
    if coordinator is not None and resolved_system_id is not None and hasattr(coordinator, "detected_model"):
        detected_model = coordinator.detected_model(resolved_system_id)
        if detected_model is not None:
            capability_source = "confirmed" if detected_model in MODEL_CAPABILITIES else "unconfirmed_fallback"

    resolved_bundle = data.get("systems", {}).get(resolved_system_id) if resolved_system_id else None
    if not isinstance(resolved_bundle, Mapping):
        resolved_bundle = {}

    subscription_topics = (
        [{"topic": topic, "qos": qos} for topic, qos in build_default_subscriptions(resolved_device_serial)]
        if resolved_device_serial
        else []
    )

    write_state = dict(getattr(coordinator, "mqtt_write_state", {}) or {}) if coordinator is not None else {}
    last_rest_update_success_at = getattr(coordinator, "last_rest_update_success_at", None)

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "mqtt": {
            "enabled": bool(entry.options.get(CONF_ENABLE_MQTT, False)),
            "configured_system_id": entry.options.get(CONF_MQTT_SYSTEM_ID),
            "resolved_system_id": resolved_system_id,
            "resolved_device_serial": resolved_device_serial,
            "detected_model": detected_model,
            "capability_source": capability_source,
            "connection_state": {
                **dict(data.get("mqtt_state", {})),
                # Counted centrally in JackeryMqttClient.async_publish_json()
                # so it covers every publish caller (coordinator and every
                # entity platform), not just the coordinator's own two paths.
                "publish_count": getattr(mqtt_client, "publish_count", 0),
            },
            "broker_config": async_redact_data(mqtt_credentials, TO_REDACT),
            "write_state": write_state,
            "subscription_topics": subscription_topics,
            "gateway": {
                "connection_state": resolved_bundle.get("device_connection"),
                "age_seconds": _safe_get(resolved_bundle, "mqtt_live", "device_connection", "age_seconds"),
            },
            "protocol_validation": _protocol_validation(resolved_bundle),
        },
        "systems": {
            "selected_system_ids": list(data.get("selected_system_ids", [])),
            "available_system_ids": list(data.get("available_systems", {}).keys()),
        },
        "coordinator": {
            "last_rest_update_success_at": last_rest_update_success_at,
        },
    }
