"""Tests for select.py entity instance behavior: current_option mapping,
async_select_option write path, coordinator-update refresh, and the
data_get "request initial state" flow. Complements
tests/unit/test_entity_setup_gating.py (setup gating) and
tests/unit/test_entity_availability.py (availability) - this covers the
remaining behavior those two intentionally left out.

Constructor only needs coordinator/system_id/bundle/mqtt_client/device_sn
- no `hass` (same as number.py, already exercised in
test_number_normalization.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.jackery_home_cloud.const import (
    MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
    MQTT_EMS_WORK_MODE_METER_ID,
)
from custom_components.jackery_home_cloud.select import (
    MODE_OPTIONS,
    OUTPUT_POWER_LIMIT_OPTIONS,
    JackeryOutputPowerLimitSelect,
    JackeryWorkModeSelect,
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


class TestWorkModeCurrentOption:
    def test_known_raw_value_maps_to_option_label(self):
        entity, _, _ = _make_entity(JackeryWorkModeSelect, bundle={"work_mode_raw": "3"})
        assert entity.current_option == "Battery priority"

    def test_unknown_raw_value_returns_none(self):
        entity, _, _ = _make_entity(JackeryWorkModeSelect, bundle={"work_mode_raw": "99"})
        assert entity.current_option is None

    def test_non_string_raw_value_returns_none(self):
        entity, _, _ = _make_entity(JackeryWorkModeSelect, bundle={"work_mode_raw": 3})
        assert entity.current_option is None

    def test_missing_raw_value_returns_none(self):
        entity, _, _ = _make_entity(JackeryWorkModeSelect, bundle={})
        assert entity.current_option is None


class TestWorkModeSelectOption:
    async def test_valid_option_calls_coordinator_with_expected_kwargs(self):
        entity, coordinator, _ = _make_entity(JackeryWorkModeSelect)

        await entity.async_select_option("Time of use")

        coordinator.async_set_meter_value.assert_awaited_once()
        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["meter_id"] == MQTT_EMS_WORK_MODE_METER_ID
        assert kwargs["raw_value"] == MODE_OPTIONS["Time of use"]
        assert kwargs["bundle_key"] == "work_mode_raw"
        assert kwargs["timestamp_key"] == "work_mode_raw_at"
        assert kwargs["expected_bundle_value"] == MODE_OPTIONS["Time of use"]
        assert kwargs["refresh_group"] == coordinator.async_request_config_live_meter_values

    async def test_unknown_option_raises_without_calling_coordinator(self):
        entity, coordinator, _ = _make_entity(JackeryWorkModeSelect)

        with pytest.raises(HomeAssistantError, match="Unknown"):
            await entity.async_select_option("Not a real mode")

        coordinator.async_set_meter_value.assert_not_awaited()

    async def test_no_device_serial_raises(self):
        entity, coordinator, _ = _make_entity(JackeryWorkModeSelect, device_sn="")

        with pytest.raises(HomeAssistantError, match="device serial"):
            await entity.async_select_option("Self-consumption")

        coordinator.async_set_meter_value.assert_not_awaited()


class TestOutputPowerLimitSelectOption:
    def test_current_option_maps_raw_value(self):
        entity, _, _ = _make_entity(JackeryOutputPowerLimitSelect, bundle={"output_power_limit_raw": "1"})
        assert entity.current_option == "1500 W"

    async def test_valid_option_calls_coordinator_with_expected_kwargs(self):
        entity, coordinator, _ = _make_entity(JackeryOutputPowerLimitSelect)

        await entity.async_select_option("800 W")

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["meter_id"] == MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID
        assert kwargs["raw_value"] == OUTPUT_POWER_LIMIT_OPTIONS["800 W"]

    async def test_unknown_option_raises(self):
        entity, coordinator, _ = _make_entity(JackeryOutputPowerLimitSelect)
        with pytest.raises(HomeAssistantError, match="Unknown"):
            await entity.async_select_option("3000 W")


class TestHandleCoordinatorUpdate:
    def test_refreshes_bundle_from_coordinator_data(self):
        entity, coordinator, _ = _make_entity(JackeryWorkModeSelect, bundle={"work_mode_raw": "2"})
        assert entity.current_option == "Self-consumption"
        # super()._handle_coordinator_update() calls async_write_ha_state(),
        # which needs a real self.hass - stub it since this entity is never
        # actually added to hass in this unit test.
        entity.async_write_ha_state = lambda: None

        coordinator.data["systems"]["sys1"] = {"work_mode_raw": "7"}
        entity._handle_coordinator_update()

        assert entity.current_option == "Intelligent mode"


class TestRequestState:
    async def test_no_device_serial_never_publishes(self):
        entity, _, mqtt_client = _make_entity(JackeryWorkModeSelect, device_sn="")
        await entity._async_request_state()
        assert mqtt_client.calls == []

    async def test_publishes_data_get_for_the_right_meter(self):
        entity, _, mqtt_client = _make_entity(JackeryWorkModeSelect, device_sn="SN1")
        await entity._async_request_state()

        assert len(mqtt_client.calls) == 1
        payload = mqtt_client.calls[0]["payload"]
        assert payload["cmd"] == "data_get"
        assert payload["gw_sn"] == "SN1"
        assert payload["info"]["dev_list"][0]["meter_list"] == [MQTT_EMS_WORK_MODE_METER_ID]

    async def test_not_connected_error_is_swallowed(self):
        mqtt_client = _FakeMqttClient(exception=RuntimeError("mqtt client is not connected"))
        entity, _, _ = _make_entity(JackeryWorkModeSelect, mqtt_client=mqtt_client)
        await entity._async_request_state()  # must not raise

    async def test_other_error_is_wrapped_in_home_assistant_error(self):
        mqtt_client = _FakeMqttClient(exception=RuntimeError("broker exploded"))
        entity, _, _ = _make_entity(JackeryWorkModeSelect, mqtt_client=mqtt_client)
        with pytest.raises(HomeAssistantError, match="broker exploded"):
            await entity._async_request_state()
