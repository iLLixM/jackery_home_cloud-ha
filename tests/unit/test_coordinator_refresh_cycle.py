"""Tests for JackeryHomeCloudCoordinator's real `_async_update_data` cycle,
driven through `hass`/`MockConfigEntry` and a fully mocked JackeryApiClient
(backlog discussion #6, item 17, "Multi-system safeguard" section):

    the primary system is frozen during runtime, changed REST ordering
    does not switch it

tests/unit/test_coordinator_mqtt_system_selection.py already proves this
for `_resolve_mqtt_system` in isolation (a pure function); this file
proves the same property through the real multi-poll refresh cycle - the
only place the freeze itself (`if self.mqtt_system_id is None: ...`)
actually lives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.jackery_home_cloud.api.client import JackeryApiClient
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    DOMAIN,
)
from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
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


async def _make_coordinator(hass, client) -> JackeryHomeCloudCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
        options={},  # no explicit selection -> falls back to sorted(all systems)
    )
    entry.add_to_hass(hass)
    coordinator = JackeryHomeCloudCoordinator(hass, entry, client)
    await coordinator._async_setup()
    return coordinator


class TestPrimarySystemFreezeAcrossPolls:
    async def test_first_refresh_freezes_on_first_system_with_resolvable_serial(self, hass):
        client = _make_client([[_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")]])
        coordinator = await _make_coordinator(hass, client)

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.mqtt_system_id == SYSTEM_A
        assert coordinator.mqtt_device_serial == "SN-A"

    async def test_changed_rest_ordering_on_later_poll_does_not_switch_primary(self, hass):
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [_system(SYSTEM_B, "SN-B"), _system(SYSTEM_A, "SN-A")],  # order flipped
            ]
        )
        coordinator = await _make_coordinator(hass, client)

        await coordinator.async_refresh()
        assert coordinator.mqtt_system_id == SYSTEM_A

        await coordinator.async_refresh()
        # _resolve_selected_system_ids falls back to sorted(systems_by_id),
        # so selection order is unaffected by REST list order anyway - the
        # real regression risk is a *different* system resolving first
        # (see next test), but this pins that reordering the raw REST
        # response alone changes nothing.
        assert coordinator.mqtt_system_id == SYSTEM_A
        assert coordinator.mqtt_device_serial == "SN-A"

    async def test_primary_system_losing_its_serial_on_a_later_poll_stays_frozen(self, hass):
        """Documents the current (possibly surprising) behavior: once
        frozen, mqtt_system_id is never re-resolved even if system A can
        no longer be resolved and B now could - only a fresh coordinator
        instance (i.e. a config entry reload) re-resolves.
        """
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [{"id": SYSTEM_A, "systemId": SYSTEM_A, "name": "System 1"}, _system(SYSTEM_B, "SN-B")],
            ]
        )
        coordinator = await _make_coordinator(hass, client)

        await coordinator.async_refresh()
        assert coordinator.mqtt_system_id == SYSTEM_A

        await coordinator.async_refresh()
        assert coordinator.mqtt_system_id == SYSTEM_A
        assert coordinator.mqtt_device_serial == "SN-A"

    async def test_warning_is_only_recorded_once_for_a_persistently_different_resolution(self, hass):
        client = _make_client(
            [
                [_system(SYSTEM_A, "SN-A"), _system(SYSTEM_B, "SN-B")],
                [{"id": SYSTEM_A, "systemId": SYSTEM_A, "name": "System 1"}, _system(SYSTEM_B, "SN-B")],
            ]
        )
        coordinator = await _make_coordinator(hass, client)
        await coordinator.async_refresh()
        assert coordinator._mqtt_system_last_warned is None

        await coordinator.async_refresh()
        first_warned = coordinator._mqtt_system_last_warned
        assert first_warned == (SYSTEM_B, "SN-B")

        await coordinator.async_refresh()
        # Same divergent resolution again - _mqtt_system_last_warned must
        # not be re-set to a "new" identical value repeatedly (it's the
        # dedup marker itself, so this just proves it stays stable).
        assert coordinator._mqtt_system_last_warned == first_warned

    async def test_mqtt_event_forces_last_update_success_back_to_true_after_a_failed_poll(self, hass):
        """Companion, at the full refresh-cycle level, to the entity
        `available` gap pinned in test_entity_availability.py:
        `_publish_runtime_update()` (invoked by the real
        `async_handle_mqtt_message` entry point used by mqtt_client's
        callback) unconditionally forces `last_update_success = True`
        whenever an MQTT event arrives, even if the most recent REST poll
        actually failed - `available` on MQTT-backed entities has no way
        to see a REST outage once any MQTT traffic follows it.
        """
        client = _make_client([[_system(SYSTEM_A, "SN-A")]])
        coordinator = await _make_coordinator(hass, client)
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True

        client.async_list_systems.side_effect = JackeryHomeApiError("REST temporarily unavailable")
        await coordinator.async_refresh()
        assert coordinator.last_update_success is False

        await coordinator.async_handle_mqtt_message({})
        assert coordinator.last_update_success is True
