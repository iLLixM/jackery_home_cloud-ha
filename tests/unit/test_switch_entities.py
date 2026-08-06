"""Tests for switch.py entity instance behavior: is_on mapping,
async_turn_on/async_turn_off write paths, icon/extra_state_attributes,
coordinator-update refresh, and the data_get "request initial state"
flow. Complements tests/unit/test_entity_setup_gating.py (setup gating)
and tests/unit/test_entity_availability.py (availability).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.jackery_home_cloud.const import (
    MQTT_EMS_AC_OUTPUT_METER_ID,
    MQTT_EMS_AUTO_STANDBY_METER_ID,
    MQTT_EMS_AUTO_STANDBY_RAW_OFF,
    MQTT_EMS_AUTO_STANDBY_RAW_ON,
    MQTT_EMS_STANDBY_METER_ID,
    MQTT_EMS_STANDBY_RAW_OFF,
    MQTT_EMS_STANDBY_RAW_ON,
)
from custom_components.jackery_home_cloud.switch import (
    JackeryAcOutputSwitch,
    JackeryAutoStandbySwitch,
    JackeryStandbySwitch,
)


class _FakeCoordinator:
    def __init__(self, bundle: dict | None = None):
        self.data = {"systems": {"sys1": bundle if bundle is not None else {}}}
        self.async_set_meter_value = AsyncMock()
        self.async_request_config_live_meter_values = AsyncMock()


class _FakeMqttClient:
    def __init__(self, *, exception: Exception | None = None):
        self.calls: list[dict] = []
        self._exception = exception

    async def async_publish_json(self, topic, payload, qos=1):
        self.calls.append({"topic": topic, "payload": payload, "qos": qos})
        if self._exception is not None:
            raise self._exception


def _make_entity(cls, *, bundle: dict | None = None, device_sn: str = "SN1", mqtt_client=None):
    coordinator = _FakeCoordinator(bundle)
    mqtt_client = mqtt_client if mqtt_client is not None else _FakeMqttClient()
    entity = cls(
        coordinator=coordinator,
        system_id="sys1",
        bundle=bundle or {},
        mqtt_client=mqtt_client,
        device_sn=device_sn,
    )
    return entity, coordinator, mqtt_client


class TestAcOutputSwitch:
    def test_is_on_true(self):
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": True})
        assert entity.is_on is True

    def test_is_on_false(self):
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": False})
        assert entity.is_on is False

    def test_is_on_none_when_missing_or_non_bool(self):
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={})
        assert entity.is_on is None
        entity2, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": "1"})
        assert entity2.is_on is None

    def test_icon_reflects_state(self):
        on_entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": True})
        off_entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": False})
        unknown_entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={})
        assert on_entity.icon == "mdi:power-plug"
        assert off_entity.icon == "mdi:power-plug-off-outline"
        assert unknown_entity.icon == "mdi:power-plug"

    def test_extra_state_attributes_from_mqtt_live_metadata(self):
        bundle = {
            "ac_output_state": True,
            "mqtt_live": {"ac_output_state": {"source": "mqtt_data_set", "age_seconds": 3}},
        }
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle=bundle)
        assert entity.extra_state_attributes == {"state_source": "mqtt_data_set", "state_age_seconds": 3}

    def test_extra_state_attributes_empty_when_no_metadata(self):
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": True})
        assert entity.extra_state_attributes == {}

    async def test_turn_on_calls_coordinator_with_expected_kwargs(self):
        entity, coordinator, _ = _make_entity(JackeryAcOutputSwitch)

        await entity.async_turn_on()

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["meter_id"] == MQTT_EMS_AC_OUTPUT_METER_ID
        assert kwargs["raw_value"] == "1"
        assert kwargs["bundle_key"] == "ac_output_state"
        assert kwargs["expected_bundle_value"] is True

    async def test_turn_off_sends_raw_zero(self):
        entity, coordinator, _ = _make_entity(JackeryAcOutputSwitch)

        await entity.async_turn_off()

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["raw_value"] == "0"
        assert kwargs["expected_bundle_value"] is False

    async def test_turn_on_without_device_serial_raises(self):
        entity, coordinator, _ = _make_entity(JackeryAcOutputSwitch, device_sn="")

        with pytest.raises(HomeAssistantError, match="device serial"):
            await entity.async_turn_on()

        coordinator.async_set_meter_value.assert_not_awaited()


class TestStandbySwitch:
    def test_is_on_true_for_standby_raw_on(self):
        entity, _, _ = _make_entity(JackeryStandbySwitch, bundle={"standby_raw": MQTT_EMS_STANDBY_RAW_ON})
        assert entity.is_on is True

    def test_is_on_false_for_standby_raw_off(self):
        entity, _, _ = _make_entity(JackeryStandbySwitch, bundle={"standby_raw": MQTT_EMS_STANDBY_RAW_OFF})
        assert entity.is_on is False

    def test_is_on_none_for_unrecognized_raw(self):
        entity, _, _ = _make_entity(JackeryStandbySwitch, bundle={"standby_raw": "99"})
        assert entity.is_on is None

    async def test_turn_on_publishes_standby_on_raw(self):
        entity, coordinator, _ = _make_entity(JackeryStandbySwitch)

        await entity.async_turn_on()

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["meter_id"] == MQTT_EMS_STANDBY_METER_ID
        assert kwargs["raw_value"] == MQTT_EMS_STANDBY_RAW_ON
        assert kwargs["bundle_key"] == "standby_raw"
        assert kwargs["refresh_group"] == coordinator.async_request_config_live_meter_values

    async def test_turn_off_publishes_standby_off_raw(self):
        entity, coordinator, _ = _make_entity(JackeryStandbySwitch)

        await entity.async_turn_off()

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["raw_value"] == MQTT_EMS_STANDBY_RAW_OFF


class TestAutoStandbySwitch:
    def test_is_on_true_for_auto_standby_raw_on(self):
        entity, _, _ = _make_entity(
            JackeryAutoStandbySwitch, bundle={"auto_standby_raw": MQTT_EMS_AUTO_STANDBY_RAW_ON}
        )
        assert entity.is_on is True

    def test_is_on_false_for_auto_standby_raw_off(self):
        entity, _, _ = _make_entity(
            JackeryAutoStandbySwitch, bundle={"auto_standby_raw": MQTT_EMS_AUTO_STANDBY_RAW_OFF}
        )
        assert entity.is_on is False

    async def test_turn_on_calls_coordinator_with_expected_kwargs(self):
        entity, coordinator, _ = _make_entity(JackeryAutoStandbySwitch)

        await entity.async_turn_on()

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["meter_id"] == MQTT_EMS_AUTO_STANDBY_METER_ID
        assert kwargs["raw_value"] == MQTT_EMS_AUTO_STANDBY_RAW_ON

    async def test_turn_off_without_device_serial_raises(self):
        entity, coordinator, _ = _make_entity(JackeryAutoStandbySwitch, device_sn="")

        with pytest.raises(HomeAssistantError, match="device serial"):
            await entity.async_turn_off()


class TestHandleCoordinatorUpdate:
    def test_refreshes_bundle_from_coordinator_data(self):
        entity, coordinator, _ = _make_entity(JackeryAcOutputSwitch, bundle={"ac_output_state": True})
        assert entity.is_on is True
        entity.async_write_ha_state = lambda: None

        coordinator.data["systems"]["sys1"] = {"ac_output_state": False}
        entity._handle_coordinator_update()

        assert entity.is_on is False


class TestRequestState:
    async def test_no_device_serial_never_publishes(self):
        entity, _, mqtt_client = _make_entity(JackeryStandbySwitch, device_sn="")
        await entity._async_request_state()
        assert mqtt_client.calls == []

    async def test_publishes_data_get_for_the_right_meter(self):
        entity, _, mqtt_client = _make_entity(JackeryStandbySwitch, device_sn="SN1")
        await entity._async_request_state()

        assert len(mqtt_client.calls) == 1
        payload = mqtt_client.calls[0]["payload"]
        assert payload["cmd"] == "data_get"
        assert payload["info"]["dev_list"][0]["meter_list"] == [MQTT_EMS_STANDBY_METER_ID]

    async def test_not_connected_error_is_swallowed(self):
        mqtt_client = _FakeMqttClient(exception=RuntimeError("MQTT client is Not Connected"))
        entity, _, _ = _make_entity(JackeryAutoStandbySwitch, mqtt_client=mqtt_client)
        await entity._async_request_state()  # must not raise

    async def test_other_error_is_wrapped_in_home_assistant_error(self):
        mqtt_client = _FakeMqttClient(exception=RuntimeError("broker exploded"))
        entity, _, _ = _make_entity(JackeryAcOutputSwitch, mqtt_client=mqtt_client)
        with pytest.raises(HomeAssistantError, match="broker exploded"):
            await entity._async_request_state()
