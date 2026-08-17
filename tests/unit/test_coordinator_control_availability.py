"""Tests for JackeryHomeCloudCoordinator.is_control_available (discussion
#6, item 4, "MQTT-aware availability for controls"):

    Availability should consider: MQTT client connection state, selected
    MQTT system, device serial availability, gateway online state where
    available.

`is_control_available` reads `self.mqtt_system` (via `is_mqtt_system`),
`self._mqtt_state`, and `self.data`. REST health is deliberately independent:
`last_update_success` is retained in the fixture so its non-influence can be
verified explicitly. No `hass` is needed, so the coordinator is built via
`object.__new__`, same pattern as test_coordinator_mqtt_system_selection.py.
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)

SYSTEM_ID = "sys1"
OTHER_SYSTEM_ID = "sys2"


def _make_coordinator(
    *,
    mqtt_system_id: str | None = SYSTEM_ID,
    last_update_success: bool = True,
    mqtt_connected: bool = True,
    device_connection: str | None = None,
) -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system = (
        JackeryMqttSystem(system_id=mqtt_system_id, device_serial="SN1")
        if mqtt_system_id is not None
        else None
    )
    coordinator.last_update_success = last_update_success
    coordinator._mqtt_state = {"connected": mqtt_connected}
    bundle: dict = {}
    if device_connection is not None:
        bundle["device_connection"] = device_connection
    coordinator.data = {"systems": {SYSTEM_ID: bundle}}
    return coordinator


class TestIsControlAvailable:
    def test_true_when_everything_is_healthy(self):
        coordinator = _make_coordinator()
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is True

    def test_false_when_device_serial_is_empty(self):
        coordinator = _make_coordinator()
        assert coordinator.is_control_available(SYSTEM_ID, "") is False

    def test_true_when_last_rest_poll_failed_but_mqtt_is_healthy(self):
        """A cloud API outage must not disable the independent MQTT path."""
        coordinator = _make_coordinator(last_update_success=False)
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is True

    def test_false_when_not_the_configured_mqtt_system(self):
        coordinator = _make_coordinator(mqtt_system_id=SYSTEM_ID)
        assert coordinator.is_control_available(OTHER_SYSTEM_ID, "SN2") is False

    def test_false_when_no_mqtt_system_resolved_yet(self):
        coordinator = _make_coordinator(mqtt_system_id=None)
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is False

    def test_false_when_mqtt_broker_disconnected(self):
        """A real MQTT outage remains authoritative for MQTT controls."""
        coordinator = _make_coordinator(mqtt_connected=False)
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is False

    def test_true_when_no_lwt_state_has_been_observed_yet(self):
        """Optimistic default: absence of any LWT message (fresh/reloaded
        coordinator) must not make controls unavailable - only an explicit
        "offline" gateway state should."""
        coordinator = _make_coordinator(device_connection=None)
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is True

    def test_false_when_gateway_reports_offline(self):
        coordinator = _make_coordinator(device_connection="offline")
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is False

    def test_true_when_gateway_reports_online(self):
        coordinator = _make_coordinator(device_connection="online")
        assert coordinator.is_control_available(SYSTEM_ID, "SN1") is True
