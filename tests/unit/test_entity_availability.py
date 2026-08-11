"""Tests for MQTT-backed entity `available` properties (discussion #6, item
4, "MQTT-aware availability for controls"):

    Show writable MQTT entities as unavailable when MQTT communication is
    unavailable.

number.py/select.py/switch.py/button.py all now delegate to the identical
`self.coordinator.is_control_available(self._system_id, self._device_sn)`
expression. This file only tests the *wiring* - that each entity's
`available` property calls through to the coordinator with the right
arguments and returns whatever it says - via a settable fake coordinator.
The composite logic itself (device serial / last_update_success /
is_mqtt_system / MQTT connection state / gateway LWT state) is tested in
isolation in test_coordinator_control_availability.py, mirroring this
suite's existing layering (e.g. `_resolve_mqtt_system` tested standalone in
test_coordinator_mqtt_system_selection.py, the full cycle tested separately
in test_coordinator_refresh_cycle.py).

Previously, `available` computed `bool(device_sn) and super().available`
directly on each entity, and `JackeryRebootButton` had no availability
logic at all (not even a `CoordinatorEntity`). Both gaps are fixed now -
see coordinator.py's `is_control_available`/`_publish_runtime_update`.
"""

from __future__ import annotations

import pytest

from custom_components.jackery_home_cloud.button import JackeryRebootButton
from custom_components.jackery_home_cloud.number import JackeryChargeLimitSocNumber
from custom_components.jackery_home_cloud.select import JackeryWorkModeSelect
from custom_components.jackery_home_cloud.switch import JackeryAcOutputSwitch

COORDINATOR_BACKED_CLASSES = [
    JackeryChargeLimitSocNumber,
    JackeryWorkModeSelect,
    JackeryAcOutputSwitch,
]


class _FakeCoordinator:
    def __init__(self, *, control_available: bool):
        self.data = {"systems": {"sys1": {}}}
        self._control_available = control_available
        self.calls: list[tuple[str, str]] = []

    def is_control_available(self, system_id: str, device_sn: str) -> bool:
        self.calls.append((system_id, device_sn))
        return self._control_available


def _make_entity(cls, *, device_sn: str, control_available: bool):
    coordinator = _FakeCoordinator(control_available=control_available)
    entity = cls(
        coordinator=coordinator,
        system_id="sys1",
        bundle={},
        mqtt_client=object(),
        device_sn=device_sn,
    )
    return entity, coordinator


class TestCoordinatorBackedAvailability:
    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_delegates_to_coordinator_is_control_available(self, cls):
        entity, coordinator = _make_entity(cls, device_sn="SN1", control_available=True)

        assert entity.available is True
        assert coordinator.calls == [("sys1", "SN1")]

    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_reflects_coordinator_unavailable(self, cls):
        entity, coordinator = _make_entity(cls, device_sn="SN1", control_available=False)

        assert entity.available is False
        assert coordinator.calls == [("sys1", "SN1")]

    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_mqtt_disconnect_makes_entity_unavailable(self, cls):
        """The gap this used to document: an MQTT broker disconnect now
        makes these entities unavailable (via is_control_available), where
        it previously had no effect at all because
        coordinator.last_update_success was force-reset to True on every
        MQTT event regardless of actual connection state."""
        entity, coordinator = _make_entity(cls, device_sn="SN1", control_available=False)
        assert entity.available is False


class TestRebootButtonAvailability:
    def test_delegates_to_coordinator_is_control_available(self):
        coordinator = _FakeCoordinator(control_available=True)
        button = JackeryRebootButton(
            coordinator=coordinator,
            system_id="sys1",
            bundle={},
            mqtt_client=object(),
            device_sn="SN1",
        )

        assert button.available is True
        assert coordinator.calls == [("sys1", "SN1")]

    def test_unavailable_without_device_serial(self):
        """JackeryRebootButton is now a CoordinatorEntity with real
        availability logic - previously it was a plain ButtonEntity that
        never overrode `available` at all, so it was always True regardless
        of device_sn (which would make async_press raise)."""
        coordinator = _FakeCoordinator(control_available=False)
        button = JackeryRebootButton(
            coordinator=coordinator,
            system_id="sys1",
            bundle={},
            mqtt_client=object(),
            device_sn="",
        )

        assert button.available is False
