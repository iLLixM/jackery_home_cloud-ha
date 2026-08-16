"""Restore-lifecycle tests for MQTT-only cumulative energy sensors.

The coordinator cannot provide these counters until the first matching MQTT
report arrives after a restart. Home Assistant's recorder state bridges that
gap. These tests exercise the entity lifecycle rather than only inspecting the
sensor descriptions, so changes to ``async_added_to_hass`` or ``native_value``
cannot silently break restoration.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.jackery_home_cloud.sensor import (
    MQTT_RESTORE_SENSOR_KEYS,
    SYSTEM_SENSOR_DESCRIPTIONS,
    JackeryBaseSensor,
    JackeryMetricSensor,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

SYSTEM_ID = "sys1"
AC_OUTPUT_ENERGY_KEYS = ("ac_output_energy_in", "ac_output_energy_out")
RESTORE_SENSOR_KEYS = tuple(sorted(MQTT_RESTORE_SENSOR_KEYS))


def _make_sensor(key: str) -> tuple[JackeryMetricSensor, SimpleNamespace]:
    descriptions = {description.key: description for description in SYSTEM_SENSOR_DESCRIPTIONS}
    coordinator = SimpleNamespace(data={"systems": {SYSTEM_ID: {}}})
    sensor = JackeryMetricSensor(
        coordinator=coordinator,
        system_id=SYSTEM_ID,
        description=descriptions[key],
    )
    return sensor, coordinator


async def _add_with_last_state(sensor: JackeryMetricSensor, state: object) -> AsyncMock:
    """Run the restore hook without requiring a complete HA entity platform."""
    get_last_state = AsyncMock(
        return_value=None if state is None else SimpleNamespace(state=state)
    )
    sensor.async_get_last_state = get_last_state

    # The inherited CoordinatorEntity hook only registers listeners; it is
    # unrelated to restore parsing and is already covered by HA itself.
    with patch.object(JackeryBaseSensor, "async_added_to_hass", new=AsyncMock()):
        await sensor.async_added_to_hass()

    return get_last_state


def test_ac_output_energy_counters_are_selected_for_restore():
    """Both new counters must remain members of the explicit restore set."""
    assert set(AC_OUTPUT_ENERGY_KEYS) <= MQTT_RESTORE_SENSOR_KEYS


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
def test_every_restore_sensor_is_a_numeric_total_increasing_energy_sensor(key):
    """Enforce the numeric energy-counter contract for the complete set.

    ``MQTT_RESTORE_SENSOR_KEYS`` is a string set and cannot express this
    invariant through typing alone. This test prevents a future text sensor,
    missing description, or non-coercing value callback from being added to
    the numeric restore path unnoticed.
    """
    descriptions = {
        description.key: description for description in SYSTEM_SENSOR_DESCRIPTIONS
    }

    assert key in descriptions
    description = descriptions[key]
    assert description.device_class == SensorDeviceClass.ENERGY
    assert description.state_class == SensorStateClass.TOTAL_INCREASING
    assert description.value_fn({key: "12.345"}) == 12.345
    assert description.value_fn({key: "not-a-number"}) is None


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
async def test_numeric_recorder_state_is_restored(key):
    sensor, _ = _make_sensor(key)

    await _add_with_last_state(sensor, "12.345")

    assert sensor.native_value == 12.345
    assert sensor._last_known_native_value == 12.345


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
@pytest.mark.parametrize("state", (None, "unknown", "unavailable", "", "not-a-number"))
async def test_missing_or_invalid_recorder_state_is_ignored(key, state):
    sensor, _ = _make_sensor(key)

    await _add_with_last_state(sensor, state)

    assert sensor.native_value is None
    assert sensor._last_known_native_value is None


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
async def test_current_mqtt_value_advances_last_known_fallback(key):
    sensor, coordinator = _make_sensor(key)
    await _add_with_last_state(sensor, "12.345")

    coordinator.data["systems"][SYSTEM_ID][key] = "15.5"
    assert sensor.native_value == 15.5
    assert sensor._last_known_native_value == 15.5

    # If the live value disappears or becomes stale on a later bundle merge,
    # retain the newest accepted live counter, not the older startup state.
    coordinator.data["systems"][SYSTEM_ID].pop(key)
    assert sensor.native_value == 15.5


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
async def test_live_value_becomes_fallback_without_recorder_state(key):
    sensor, coordinator = _make_sensor(key)
    await _add_with_last_state(sensor, None)

    coordinator.data["systems"][SYSTEM_ID][key] = "15.5"
    assert sensor.native_value == 15.5
    assert sensor._last_known_native_value == 15.5

    coordinator.data["systems"][SYSTEM_ID].pop(key)
    assert sensor.native_value == 15.5


@pytest.mark.parametrize("key", RESTORE_SENSOR_KEYS)
async def test_invalid_current_value_does_not_poison_last_known_fallback(key):
    sensor, coordinator = _make_sensor(key)
    await _add_with_last_state(sensor, "12.345")

    coordinator.data["systems"][SYSTEM_ID][key] = "15.5"
    assert sensor.native_value == 15.5

    coordinator.data["systems"][SYSTEM_ID][key] = "not-a-number"
    assert sensor.native_value == 15.5
    assert sensor._last_known_native_value == 15.5


async def test_non_restore_sensor_does_not_query_the_recorder():
    """Keep recorder access restricted to the explicitly selected counters."""
    sensor, _ = _make_sensor("grid_power")

    get_last_state = await _add_with_last_state(sensor, "42.0")

    get_last_state.assert_not_awaited()
    assert sensor.native_value is None
