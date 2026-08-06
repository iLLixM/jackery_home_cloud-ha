"""Tests for JackeryHomeCloudCoordinator's real `_async_update_data` cycle,
driven through `hass`/`MockConfigEntry` and a fully mocked JackeryApiClient.

Covers two related properties:

- (discussion #6, item 17, "Multi-system safeguard" section) the resolved
  MQTT system is frozen during runtime, changed REST ordering does not
  switch it. tests/unit/test_coordinator_mqtt_system_selection.py already
  proves this for `_resolve_mqtt_system` in isolation (a pure function);
  this file proves the same property through the real multi-poll refresh
  cycle - the only place the freeze itself
  (`if self.mqtt_system is None: ...`) actually lives.
- (discussion #6, item 1, "Explicit selection of the MQTT-enabled system")
  which system is even a candidate is now driven by CONF_MQTT_SYSTEM_ID in
  the config entry's options, not inferred - every test below that needs
  an actual resolution passes it explicitly via `_make_coordinator`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.jackery_home_cloud.api.client import JackeryApiClient
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    DOMAIN,
)
from custom_components.jackery_home_cloud.coordinator import (
    _MQTT_SYSTEM_NOT_YET_WARNED,
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)
from custom_components.jackery_home_cloud.exceptions import JackeryHomeApiError
from pytest_homeassistant_custom_component.common import MockConfigEntry

SYSTEM_A = "1"
SYSTEM_B = "2"


def _system(system_id: str, serial: str) -> dict:
    return {"id": system_id, "systemId": system_id, "systemNo": serial, "name": f"System {system_id}"}


def _make_client(systems_sequence: list[list[dict]]) -> AsyncMock:
    """A JackeryApiClient double whose async_list_systems returns the next
    scripted systems list on each call, repeating the last one once the
    script is exhausted (so later polls in a test keep a stable shape)."""
    client = AsyncMock(spec=JackeryApiClient)
    client.async_get_app_user.return_value = {}
    client.async_get_mqtt_credentials.return_value = {}
    client.async_get_monitor.return_value = {}
    client.async_get_device_detail.return_value = {}
    client.async_get_cluster_trend_daily.return_value = {}
    client.async_get_battery_bms_trend_daily.return_value = {}

    remaining = list(systems_sequence)

    async def _list_systems():
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    client.async_list_systems.side_effect = _list_systems
    return client


async def _make_coordinator(hass, client, *, mqtt_system_id: str | None = None) -> JackeryHomeCloudCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
        # No CONF_SELECTED_SYSTEMS -> falls back to sorted(all systems) for
        # REST selection. CONF_MQTT_SYSTEM_ID is the only thing that decides
        # MQTT candidacy now (discussion #6, item 1).
        options={CONF_MQTT_SYSTEM_ID: mqtt_system_id},
    )
    entry.add_to_hass(hass)
    coordinator = JackeryHomeCloudCoordinator(hass, entry, client)
    await coordinator._async_setup()
    return coordinator


class TestMqttSystemFreezeAcrossPolls:
    async def test_first_refresh_resolves_the_configured_system(self, hass):
        client = _make_client([[_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")]])
        coordinator = await _make_coordinator(hass, client, mqtt_system_id=SYSTEM_A)

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.mqtt_system == JackeryMqttSystem(system_id=SYSTEM_A, device_serial="SN-A")

    async def test_configured_system_not_in_selected_set_stays_unresolved(self, hass):
        """A configured id that isn't (or is no longer) among this cycle's
        selected systems - e.g. dropped by the account - resolves to no
        MQTT system at all, rather than falling back to a different one."""
        client = _make_client([[_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")]])
        coordinator = await _make_coordinator(hass, client, mqtt_system_id="sys-missing")

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.mqtt_system is None

    async def test_changed_rest_ordering_on_later_poll_does_not_switch_system(self, hass):
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [_system(SYSTEM_B, "SN-B"), _system(SYSTEM_A, "SN-A")],  # order flipped
            ]
        )
        coordinator = await _make_coordinator(hass, client, mqtt_system_id=SYSTEM_A)

        await coordinator.async_refresh()
        assert coordinator.mqtt_system == JackeryMqttSystem(system_id=SYSTEM_A, device_serial="SN-A")

        await coordinator.async_refresh()
        assert coordinator.mqtt_system == JackeryMqttSystem(system_id=SYSTEM_A, device_serial="SN-A")

    async def test_configured_system_losing_its_serial_on_a_later_poll_stays_frozen(self, hass):
        """Once frozen, coordinator.mqtt_system is never re-resolved even if
        the configured system can no longer be resolved on a later poll -
        only a fresh coordinator instance (i.e. a config entry reload)
        re-resolves.
        """
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [{"id": SYSTEM_A, "systemId": SYSTEM_A, "name": "System 1"}, _system(SYSTEM_B, "SN-B")],
            ]
        )
        coordinator = await _make_coordinator(hass, client, mqtt_system_id=SYSTEM_A)

        await coordinator.async_refresh()
        assert coordinator.mqtt_system == JackeryMqttSystem(system_id=SYSTEM_A, device_serial="SN-A")

        await coordinator.async_refresh()
        assert coordinator.mqtt_system == JackeryMqttSystem(system_id=SYSTEM_A, device_serial="SN-A")

    async def test_warning_is_only_recorded_once_for_a_persistently_different_resolution(self, hass):
        """When the configured system stops resolving a serial on a later
        poll, the frozen value is kept (previous test) but the divergent
        *would-be* resolution - now always None since only the configured
        system is ever a candidate - is logged once, not on every poll.

        `_mqtt_system_last_warned` starts at the `_MQTT_SYSTEM_NOT_YET_WARNED`
        sentinel rather than `None`, precisely so this divergent-resolution-
        equals-None case is distinguishable from "haven't warned yet".
        """
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [{"id": SYSTEM_A, "systemId": SYSTEM_A, "name": "System 1"}, _system(SYSTEM_B, "SN-B")],
            ]
        )
        coordinator = await _make_coordinator(hass, client, mqtt_system_id=SYSTEM_A)
        await coordinator.async_refresh()
        assert coordinator._mqtt_system_last_warned is _MQTT_SYSTEM_NOT_YET_WARNED

        await coordinator.async_refresh()
        first_warned = coordinator._mqtt_system_last_warned
        assert first_warned is None

        await coordinator.async_refresh()
        # Same divergent resolution again - _mqtt_system_last_warned must
        # not be re-set to a "new" identical value repeatedly (it's the
        # dedup marker itself, so this just proves it stays stable).
        assert coordinator._mqtt_system_last_warned == first_warned

    async def test_mqtt_event_no_longer_forces_last_update_success_back_to_true_after_a_failed_poll(self, hass):
        """Companion, at the full refresh-cycle level, to the fix verified
        in test_entity_availability.py and
        test_coordinator_control_availability.py: `_publish_runtime_update()`
        (invoked by the real `async_handle_mqtt_message` entry point used by
        mqtt_client's callback) used to unconditionally force
        `last_update_success = True` whenever an MQTT event arrived, even if
        the most recent REST poll had actually failed. It no longer touches
        `last_update_success` at all (discussion #6, item 4, "MQTT-aware
        availability") - MQTT-specific availability is now a separate
        signal, see `JackeryHomeCloudCoordinator.is_control_available`.
        """
        client = _make_client([[_system(SYSTEM_A, "SN-A")]])
        coordinator = await _make_coordinator(hass, client)
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True

        client.async_list_systems.side_effect = JackeryHomeApiError("REST temporarily unavailable")
        await coordinator.async_refresh()
        assert coordinator.last_update_success is False

        await coordinator.async_handle_mqtt_message({})
        assert coordinator.last_update_success is False
