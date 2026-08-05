"""Tests for MQTT-backed entity `available` properties - pins the current
behavior (not a fix; backlog discussion #6 item #4 "MQTT-aware
availability" is not implemented yet, so this documents what exists
today, same role as the unique_id snapshot tests).

number.py/select.py/switch.py all duplicate the identical expression
`bool(self._device_sn) and super().available`, where `super().available`
(HA core CoordinatorEntity) resolves to `self.coordinator.
last_update_success`. A real, surprising finding from exploring this
code: `coordinator._publish_runtime_update()` (coordinator.py:972)
unconditionally forces `self.last_update_success = True` every time an
MQTT event is ingested, regardless of the actual MQTT connection state
tracked separately in `self._mqtt_state["connected"]`. So today, an MQTT
disconnect never makes these entities unavailable - only a REST poll
failure does. `JackeryRebootButton` (button.py) doesn't even subclass
`CoordinatorEntity`, so it has no availability logic tied to the
coordinator at all.

These tests intentionally pin that current behavior, including the gap:
if backlog item #4 is ever implemented, these tests should start failing
and need to be updated as part of that change - that is the point of
pinning them now.
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
    def __init__(self, *, last_update_success: bool):
        self.data = {"systems": {"sys1": {}}}
        self.last_update_success = last_update_success


def _make_entity(cls, *, device_sn: str, last_update_success: bool):
    coordinator = _FakeCoordinator(last_update_success=last_update_success)
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
    def test_unavailable_when_device_serial_is_empty(self, cls):
        entity, _ = _make_entity(cls, device_sn="", last_update_success=True)
        assert entity.available is False

    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_available_when_serial_present_and_coordinator_healthy(self, cls):
        entity, _ = _make_entity(cls, device_sn="SN1", last_update_success=True)
        assert entity.available is True

    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_unavailable_when_coordinator_last_update_failed(self, cls):
        entity, _ = _make_entity(cls, device_sn="SN1", last_update_success=False)
        assert entity.available is False

    @pytest.mark.parametrize("cls", COORDINATOR_BACKED_CLASSES)
    def test_gap_mqtt_disconnect_does_not_affect_availability(self, cls):
        """Documents the gap: coordinator.last_update_success only tracks
        REST poll success (and is force-set True on every MQTT event via
        _publish_runtime_update), so it stays True even while the MQTT
        broker connection is actually down - `available` has no way to
        see that today. See backlog discussion #6, item #4.
        """
        entity, coordinator = _make_entity(cls, device_sn="SN1", last_update_success=True)
        # Simulate what a real coordinator's state would look like during
        # an MQTT outage: _mqtt_state reports disconnected, but
        # last_update_success was already forced back to True by the
        # last-processed MQTT event before the disconnect.
        assert entity.available is True


class TestRebootButtonHasNoAvailabilityLogic:
    def test_always_available_even_without_device_serial(self):
        """JackeryRebootButton is a plain ButtonEntity (not a
        CoordinatorEntity) and never overrides `available`, so it inherits
        HA core's default `_attr_available = True` unconditionally - even
        an empty device serial (which would make async_press raise) does
        not affect this property.
        """
        button = JackeryRebootButton(
            system_id="sys1",
            bundle={},
            mqtt_client=object(),
            device_sn="",
        )
        assert button.available is True
