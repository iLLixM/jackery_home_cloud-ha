"""Tests for number.py entity instance behavior (backlog discussion #6,
item 17, "Entity behavior" section): "integer and step normalization
works".

Complements tests/unit/test_number_entities.py, which only inspects class
attributes as static data and never instantiates an entity. The
constructor here only needs `coordinator`, `system_id`, `bundle`,
`mqtt_client`, `device_sn` - no `hass` - confirmed by reading
`_JackeryMqttNumberEntity.__init__` (number.py:93-109), and
`CoordinatorEntity.__init__` (HA core) only stores the coordinator
reference without touching it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.jackery_home_cloud.number import (
    JackeryChargeLimitSocNumber,
    JackeryDischargeLimitSocNumber,
    JackeryFeedPowerLimitNumber,
)


class _FakeCoordinator:
    def __init__(self):
        self.data = {"systems": {"sys1": {}}}
        self.async_set_meter_value = AsyncMock()
        self.async_request_config_live_meter_values = AsyncMock()


def _make_entity(cls, *, device_sn="SN1"):
    coordinator = _FakeCoordinator()
    entity = cls(
        coordinator=coordinator,
        system_id="sys1",
        bundle={},
        mqtt_client=object(),
        device_sn=device_sn,
    )
    return entity, coordinator


class TestFeedPowerLimitStepNormalization:
    def test_rounds_down_to_nearest_10w(self):
        entity, _ = _make_entity(JackeryFeedPowerLimitNumber)
        assert entity._normalize_native_value(223) == 220

    def test_rounds_up_to_nearest_10w(self):
        entity, _ = _make_entity(JackeryFeedPowerLimitNumber)
        assert entity._normalize_native_value(226) == 230

    def test_native_to_raw_uses_plain_integer_rounding(self):
        entity, _ = _make_entity(JackeryFeedPowerLimitNumber)
        assert entity._native_to_raw(219.6) == "220"


class TestSocEntitiesHaveNoStepNormalization:
    """Regression pin: unlike feed-power-limit, the two SOC number
    entities inherit the base no-op `_normalize_native_value` - a value
    is passed through unchanged before being scaled into a raw value.
    """

    @pytest.mark.parametrize("cls", [JackeryChargeLimitSocNumber, JackeryDischargeLimitSocNumber])
    def test_normalize_native_value_is_a_no_op(self, cls):
        entity, _ = _make_entity(cls)
        assert entity._normalize_native_value(23.4) == 23.4


class TestSocRawValueScaling:
    def test_charge_limit_native_to_raw_applies_soc_scale(self):
        entity, _ = _make_entity(JackeryChargeLimitSocNumber)
        assert entity._native_to_raw(23.0) == "230"

    def test_discharge_limit_native_to_raw_applies_soc_scale(self):
        entity, _ = _make_entity(JackeryDischargeLimitSocNumber)
        assert entity._native_to_raw(20.0) == "200"

    def test_charge_limit_raw_to_native_is_whole_percentage(self):
        entity, _ = _make_entity(JackeryChargeLimitSocNumber)
        assert entity._raw_to_native(72.4) == 72


class TestAsyncSetNativeValue:
    async def test_calls_coordinator_with_normalized_value_and_meter_metadata(self):
        entity, coordinator = _make_entity(JackeryFeedPowerLimitNumber)

        await entity.async_set_native_value(226)

        coordinator.async_set_meter_value.assert_awaited_once()
        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["system_id"] == "sys1"
        assert kwargs["meter_id"] == JackeryFeedPowerLimitNumber._meter_id
        assert kwargs["bundle_key"] == "feed_power_limit_mqtt"
        assert kwargs["timestamp_key"] == "feed_power_limit_mqtt_at"
        assert kwargs["raw_value"] == "230"  # 226 normalized to 230 (nearest 10W)
        assert kwargs["expected_bundle_value"] == 230

    async def test_raises_without_device_serial(self):
        entity, coordinator = _make_entity(JackeryFeedPowerLimitNumber, device_sn="")

        with pytest.raises(HomeAssistantError, match="device serial"):
            await entity.async_set_native_value(100)

        coordinator.async_set_meter_value.assert_not_awaited()

    async def test_no_min_max_clamping_happens_in_this_codebase(self):
        """Documents current behavior, not a fix: a value far outside
        native_min_value/native_max_value passed directly to
        async_set_native_value is not rejected or clamped here - HA core's
        number service (homeassistant/components/number/__init__.py)
        is the only place that enforces those bounds, and only when a
        write goes through the service call, not when this method is
        invoked directly.
        """
        entity, coordinator = _make_entity(JackeryFeedPowerLimitNumber)
        assert entity.native_max_value < 100_000

        await entity.async_set_native_value(100_000)

        kwargs = coordinator.async_set_meter_value.await_args.kwargs
        assert kwargs["expected_bundle_value"] == 100_000  # passed through, only step-rounded
