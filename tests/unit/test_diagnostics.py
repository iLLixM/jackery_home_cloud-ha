"""Tests for diagnostics.py (discussion #6, item 1: "Expose the selected
MQTT system in diagnostics").

Constructs a `MockConfigEntry` with sensitive `data`/`options` plus a
minimal `SimpleNamespace` standing in for `entry.runtime_data.coordinator`
(only `.data`/`.mqtt_system`/`.mqtt_credentials` are read by
`async_get_config_entry_diagnostics`), matching this project's "construct
minimal fakes directly" convention (see test_init_lifecycle.py's
`_FakeCoordinator`). Calls the diagnostics function directly rather than
through any HTTP/websocket layer.
"""

from __future__ import annotations

from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jackery_home_cloud import diagnostics
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    DOMAIN,
)
from custom_components.jackery_home_cloud.coordinator import JackeryMqttSystem

REDACTED = "**REDACTED**"


def _entry(*, data=None, options=None, coordinator=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data
        or {
            CONF_ACCOUNT: "user@example.com",
            CONF_PASSWORD: "super-secret",
            CONF_PHONE_UID: "ha-1",
        },
        options=options
        or {
            CONF_ENABLE_MQTT: True,
            CONF_CRYPTO_KEY: "crypto-secret",
            CONF_MQTT_SYSTEM_ID: "1",
        },
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator) if coordinator is not None else None
    return entry


def _coordinator(
    *,
    mqtt_system_id="1",
    mqtt_device_serial="SN1",
    mqtt_credentials=None,
    data=None,
    detected_model=None,
    mqtt_write_state=None,
    last_rest_update_success_at=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        mqtt_system=(
            JackeryMqttSystem(system_id=mqtt_system_id, device_serial=mqtt_device_serial)
            if mqtt_system_id is not None
            else None
        ),
        mqtt_credentials=mqtt_credentials or {},
        data=data
        or {
            "mqtt_state": {"connected": True},
            "selected_system_ids": ["1", "2"],
            "available_systems": {"1": {}, "2": {}, "3": {}},
        },
        detected_model=lambda system_id: detected_model,
        mqtt_write_state=mqtt_write_state or {},
        last_rest_update_success_at=last_rest_update_success_at,
    )


class TestRedaction:
    async def test_redacts_entry_data(self, hass):
        entry = _entry(coordinator=_coordinator())

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["entry"]["data"][CONF_ACCOUNT] == REDACTED
        assert result["entry"]["data"][CONF_PASSWORD] == REDACTED
        assert result["entry"]["data"][CONF_PHONE_UID] == REDACTED

    async def test_redacts_entry_options_crypto_key(self, hass):
        entry = _entry(coordinator=_coordinator())

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["entry"]["options"][CONF_CRYPTO_KEY] == REDACTED
        # Not a secret - must remain visible so diagnostics are useful.
        assert result["entry"]["options"][CONF_MQTT_SYSTEM_ID] == "1"

    async def test_redacts_broker_credentials_but_keeps_host_and_port(self, hass):
        coordinator = _coordinator(
            mqtt_credentials={
                "mqttServer": "broker.example.com",
                "mqttPort": 8883,
                "mqttUserName": "device-user",
                "mqttPassword": "device-secret",
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        broker = result["mqtt"]["broker_config"]
        assert broker["mqttUserName"] == REDACTED
        assert broker["mqttPassword"] == REDACTED
        assert broker["mqttServer"] == "broker.example.com"
        assert broker["mqttPort"] == 8883


class TestContent:
    async def test_exposes_configured_and_resolved_mqtt_system(self, hass):
        entry = _entry(
            options={CONF_ENABLE_MQTT: True, CONF_MQTT_SYSTEM_ID: "2"},
            coordinator=_coordinator(mqtt_system_id="2", mqtt_device_serial="SN2"),
        )

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["enabled"] is True
        assert result["mqtt"]["configured_system_id"] == "2"
        assert result["mqtt"]["resolved_system_id"] == "2"
        assert result["mqtt"]["resolved_device_serial"] == "SN2"

    async def test_exposes_confirmed_model_capability_source(self, hass):
        entry = _entry(
            coordinator=_coordinator(detected_model="JAKS-IN1K5-BA2K-EUA1"),
        )

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["detected_model"] == "JAKS-IN1K5-BA2K-EUA1"
        assert result["mqtt"]["capability_source"] == "confirmed"

    async def test_exposes_unconfirmed_model_capability_source(self, hass):
        entry = _entry(
            coordinator=_coordinator(detected_model="SOME-FUTURE-MODEL"),
        )

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["detected_model"] == "SOME-FUTURE-MODEL"
        assert result["mqtt"]["capability_source"] == "unconfirmed_fallback"

    async def test_no_detected_model_leaves_capability_fields_none(self, hass):
        entry = _entry(coordinator=_coordinator(detected_model=None))

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["detected_model"] is None
        assert result["mqtt"]["capability_source"] is None

    async def test_exposes_mqtt_connection_state(self, hass):
        coordinator = _coordinator(data={"mqtt_state": {"connected": False, "error": "timeout"}})
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["connection_state"] == {"connected": False, "error": "timeout"}

    async def test_exposes_selected_and_available_system_ids(self, hass):
        entry = _entry(coordinator=_coordinator())

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["systems"]["selected_system_ids"] == ["1", "2"]
        assert sorted(result["systems"]["available_system_ids"]) == ["1", "2", "3"]

    async def test_handles_missing_runtime_data_gracefully(self, hass):
        """A config entry that hasn't finished (or failed) setup has no
        runtime_data/coordinator yet - diagnostics must not crash."""
        entry = _entry(coordinator=None)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["resolved_system_id"] is None
        assert result["mqtt"]["resolved_device_serial"] == ""
        assert result["systems"]["selected_system_ids"] == []
        assert result["systems"]["available_system_ids"] == []
        assert result["mqtt"]["write_state"] == {}
        assert result["mqtt"]["subscription_topics"] == []
        assert result["mqtt"]["gateway"] == {"connection_state": None, "age_seconds": None}
        assert result["mqtt"]["protocol_validation"] == {
            key: {"mqtt": None, "rest": None}
            for key in (
                "grid_power",
                "ac_main_power",
                "battery_soc",
                "pv_power",
                "eps_load_power",
                "other_load_power",
            )
        }
        assert result["coordinator"]["last_rest_update_success_at"] is None


class TestPhase3Diagnostics:
    """New fields added for discussion #6 Phase 3 (items 10/11/13:
    MQTT-vs-REST validation tooling, AC main power sign tooling,
    improved diagnostics)."""

    async def test_exposes_publish_count(self, hass):
        coordinator = _coordinator(data={"mqtt_state": {"connected": True, "publish_count": 7}})
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["connection_state"]["publish_count"] == 7

    async def test_exposes_last_write_confirmed(self, hass):
        coordinator = _coordinator(
            mqtt_write_state={
                "last_confirmed_meter_id": "23132161",
                "last_confirmed_bundle_key": "work_mode_raw",
                "last_confirmed_value": "5",
                "last_confirmed_at": "2026-08-09T10:00:00+00:00",
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["write_state"]["last_confirmed_meter_id"] == "23132161"
        assert result["mqtt"]["write_state"]["last_confirmed_bundle_key"] == "work_mode_raw"

    async def test_exposes_last_write_error(self, hass):
        coordinator = _coordinator(
            mqtt_write_state={
                "last_error_meter_id": "23132161",
                "last_error_bundle_key": "work_mode_raw",
                "last_error_message": "Jackery did not confirm meter 23132161 = '5' after 3 attempts",
                "last_error_at": "2026-08-09T10:05:00+00:00",
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["write_state"]["last_error_meter_id"] == "23132161"
        assert "did not confirm" in result["mqtt"]["write_state"]["last_error_message"]

    async def test_exposes_subscription_topics_for_resolved_device_serial(self, hass):
        from custom_components.jackery_home_cloud.mqtt_registry import build_default_subscriptions

        entry = _entry(coordinator=_coordinator(mqtt_device_serial="SN1"))

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        expected = [{"topic": topic, "qos": qos} for topic, qos in build_default_subscriptions("SN1")]
        assert result["mqtt"]["subscription_topics"] == expected
        assert result["mqtt"]["subscription_topics"] != []

    async def test_subscription_topics_empty_when_no_device_serial(self, hass):
        entry = _entry(coordinator=_coordinator(mqtt_system_id=None, mqtt_device_serial=""))

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["subscription_topics"] == []

    async def test_exposes_gateway_connection_state(self, hass):
        coordinator = _coordinator(
            data={
                "mqtt_state": {"connected": True},
                "selected_system_ids": ["1"],
                "available_systems": {"1": {}},
                "systems": {
                    "1": {
                        "device_connection": "online",
                        "mqtt_live": {"device_connection": {"age_seconds": 12.5}},
                    }
                },
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["gateway"] == {"connection_state": "online", "age_seconds": 12.5}

    async def test_exposes_last_rest_update_success_at(self, hass):
        entry = _entry(coordinator=_coordinator(last_rest_update_success_at="2026-08-09T09:00:00+00:00"))

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"]["last_rest_update_success_at"] == "2026-08-09T09:00:00+00:00"

    async def test_missing_last_rest_update_attribute_defaults_to_none(self, hass):
        bare_coordinator = SimpleNamespace(
            mqtt_system=JackeryMqttSystem(system_id="1", device_serial="SN1"),
            mqtt_credentials={},
            data={"mqtt_state": {}, "selected_system_ids": [], "available_systems": {}},
        )
        entry = _entry(coordinator=bare_coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"]["last_rest_update_success_at"] is None
        assert result["mqtt"]["write_state"] == {}

    async def test_exposes_protocol_validation_for_all_six_meters(self, hass):
        coordinator = _coordinator(
            data={
                "mqtt_state": {"connected": True},
                "selected_system_ids": ["1"],
                "available_systems": {"1": {}},
                "systems": {
                    "1": {
                        "grid_power_mqtt": 100.0,
                        "ac_main_power_mqtt": -50.0,
                        "battery_soc_mqtt": 80.0,
                        "pv_power_mqtt": 200.0,
                        "eps_load_power_mqtt": 30.0,
                        "other_load_power_mqtt": 40.0,
                        "monitor": {
                            "energyFlowChartVO": {
                                "gridVO": {"gridPower": 101.0},
                                "acMainVO": {"acMainPower": -49.0},
                                "emsGwVO": {"soc": 79.5},
                                "pvInfo": {"pvPower": 199.0},
                                "acInfo": {"epsLoadPower": 29.0},
                                "otherLoadVO": {"otherLoadPower": 39.0},
                            }
                        },
                    }
                },
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        validation = result["mqtt"]["protocol_validation"]
        assert validation["grid_power"] == {"mqtt": 100.0, "rest": 101.0}
        assert validation["ac_main_power"] == {"mqtt": -50.0, "rest": -49.0}
        assert validation["battery_soc"] == {"mqtt": 80.0, "rest": 79.5}
        assert validation["pv_power"] == {"mqtt": 200.0, "rest": 199.0}
        assert validation["eps_load_power"] == {"mqtt": 30.0, "rest": 29.0}
        assert validation["other_load_power"] == {"mqtt": 40.0, "rest": 39.0}

    async def test_protocol_validation_handles_mqtt_only_stale_or_missing(self, hass):
        coordinator = _coordinator(
            data={
                "mqtt_state": {"connected": False},
                "selected_system_ids": ["1"],
                "available_systems": {"1": {}},
                "systems": {
                    "1": {
                        "monitor": {
                            "energyFlowChartVO": {"gridVO": {"gridPower": 55.0}},
                        },
                    }
                },
            }
        )
        entry = _entry(coordinator=coordinator)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        assert result["mqtt"]["protocol_validation"]["grid_power"] == {"mqtt": None, "rest": 55.0}
        assert result["mqtt"]["protocol_validation"]["ac_main_power"] == {"mqtt": None, "rest": None}
