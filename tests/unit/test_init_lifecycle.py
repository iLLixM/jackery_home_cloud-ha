"""Tests for custom_components/jackery_home_cloud/__init__.py: config
entry migration, unload, and the setup orchestration logic itself (as
opposed to the coordinator's REST behavior, already covered by
test_coordinator_refresh_cycle.py, or the MQTT client's paho behavior,
already covered by test_mqtt_client.py).

`async_setup_entry` tests patch `JackeryApiClient`/
`JackeryHomeCloudCoordinator`/`JackeryMqttClient` (the names bound in
the `__init__` module's own namespace) with lightweight fakes, and stub
`hass.config_entries.async_forward_entry_setups` - this isolates the
orchestration logic under test (what async_setup_entry does with
different coordinator/MQTT outcomes) from the REST/MQTT/entity-platform
machinery that already has its own dedicated test coverage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.jackery_home_cloud as integration
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_DEBUG_RAW,
    CONF_MQTT_POLL_INTERVAL,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DOMAIN,
)
from custom_components.jackery_home_cloud.coordinator import JackeryMqttSystem
from custom_components.jackery_home_cloud.exceptions import (
    JackeryHomeCryptoError,
    JackeryHomeMqttError,
)


class _FakeApiClient:
    def __init__(self, session=None, base_url=None):
        self.session = session
        self.base_url = base_url
        self.async_build_mqtt_credentials = AsyncMock(
            return_value={"host": "h", "port": 1883, "username": "u", "password": "p", "tls": True}
        )


class _FakeCoordinator:
    def __init__(self, hass, config_entry, client):
        self.hass = hass
        self.config_entry = config_entry
        self.client = client
        self.data = {"selected_system_ids": ["1"], "systems": {}}
        self.mqtt_system = JackeryMqttSystem(system_id="1", device_serial="SN1")
        self.mqtt_credentials = {}
        self.async_config_entry_first_refresh = AsyncMock()
        self.async_handle_mqtt_status = AsyncMock()
        self.async_handle_mqtt_message = AsyncMock()
        self.async_request_fast_live_meter_values = AsyncMock()
        self.async_request_totals_live_meter_values = AsyncMock()


class _FakeMqttClient:
    instances: list["_FakeMqttClient"] = []

    def __init__(self, hass, credentials, *, device_serial, debug_raw, tls_insecure, message_callback, status_callback):
        self.hass = hass
        self.credentials = credentials
        self.device_serial = device_serial
        self.debug_raw = debug_raw
        self.tls_insecure = tls_insecure
        self.message_callback = message_callback
        self.status_callback = status_callback
        self.async_start = AsyncMock()
        self.async_stop = AsyncMock()
        _FakeMqttClient.instances.append(self)


def _entry(*, options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
        options=options or {CONF_ENABLE_MQTT: False, CONF_MQTT_SYSTEM_ID: None},
        version=1,
        minor_version=5,
    )


@pytest.fixture(autouse=True)
def _patch_setup_dependencies(monkeypatch):
    _FakeMqttClient.instances = []
    monkeypatch.setattr(integration, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(integration, "JackeryApiClient", _FakeApiClient)
    monkeypatch.setattr(integration, "JackeryHomeCloudCoordinator", _FakeCoordinator)
    monkeypatch.setattr(integration, "JackeryMqttClient", _FakeMqttClient)


class TestAsyncSetupEntryMqttDisabled:
    async def test_success_forwards_platforms_and_stores_runtime_data(self, hass, monkeypatch):
        forward = AsyncMock()
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
        entry = _entry(options={CONF_ENABLE_MQTT: False})
        entry.add_to_hass(hass)

        result = await integration.async_setup_entry(hass, entry)

        assert result is True
        assert entry.runtime_data.mqtt_client is None
        forward.assert_awaited_once()
        assert _FakeMqttClient.instances == []


class TestAsyncSetupEntryMqttEnabled:
    async def test_no_primary_system_resolved_raises_config_entry_not_ready(self, hass, monkeypatch):
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

        class _NoPrimaryCoordinator(_FakeCoordinator):
            def __init__(self, hass, config_entry, client):
                super().__init__(hass, config_entry, client)
                self.mqtt_system = None

        monkeypatch.setattr(integration, "JackeryHomeCloudCoordinator", _NoPrimaryCoordinator)
        entry = _entry(options={CONF_ENABLE_MQTT: True})
        entry.add_to_hass(hass)

        with pytest.raises(ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)

    async def test_success_starts_mqtt_client_and_registers_poll_timers(self, hass, monkeypatch):
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
        entry = _entry(options={CONF_ENABLE_MQTT: True, CONF_CRYPTO_KEY: "0123456789abcdef"})
        entry.add_to_hass(hass)

        result = await integration.async_setup_entry(hass, entry)

        assert result is True
        assert len(_FakeMqttClient.instances) == 1
        mqtt_client = _FakeMqttClient.instances[0]
        mqtt_client.async_start.assert_awaited_once()
        assert mqtt_client.device_serial == "SN1"
        assert entry.runtime_data.mqtt_client is mqtt_client
        # Two async_track_time_interval unsub callbacks registered (fast +
        # totals polling) via entry.async_on_unload.
        assert len(entry._on_unload) == 2
        # Cancel the real timers registered above so they don't linger past
        # this test (async_track_time_interval schedules a real asyncio
        # timer; nothing else in this direct-call test would ever cancel it).
        for unsub in list(entry._on_unload):
            unsub()

    async def test_crypto_key_error_is_handled_gracefully_not_raised(self, hass, monkeypatch):
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

        class _FailingApiClient(_FakeApiClient):
            def __init__(self, session=None, base_url=None):
                super().__init__(session, base_url)
                self.async_build_mqtt_credentials = AsyncMock(
                    side_effect=JackeryHomeCryptoError("missing crypto key")
                )

        monkeypatch.setattr(integration, "JackeryApiClient", _FailingApiClient)
        entry = _entry(options={CONF_ENABLE_MQTT: True})
        entry.add_to_hass(hass)

        result = await integration.async_setup_entry(hass, entry)

        assert result is True  # setup still succeeds, MQTT just stays off
        assert entry.runtime_data.mqtt_client is None
        assert _FakeMqttClient.instances == []

    async def test_generic_mqtt_error_is_handled_gracefully_not_raised(self, hass, monkeypatch):
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

        class _FailingApiClient(_FakeApiClient):
            def __init__(self, session=None, base_url=None):
                super().__init__(session, base_url)
                self.async_build_mqtt_credentials = AsyncMock(
                    side_effect=JackeryHomeMqttError("broker unreachable")
                )

        monkeypatch.setattr(integration, "JackeryApiClient", _FailingApiClient)
        entry = _entry(options={CONF_ENABLE_MQTT: True})
        entry.add_to_hass(hass)

        result = await integration.async_setup_entry(hass, entry)

        assert result is True
        assert entry.runtime_data.mqtt_client is None


class TestAsyncUnloadEntry:
    async def test_no_mqtt_client_just_unloads_platforms(self, hass, monkeypatch):
        unload = AsyncMock(return_value=True)
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
        entry = _entry()
        entry.add_to_hass(hass)
        entry.runtime_data = integration.JackeryHomeCloudRuntime(client=object(), coordinator=object())

        result = await integration.async_unload_entry(hass, entry)

        assert result is True
        unload.assert_awaited_once()

    async def test_stops_mqtt_client_before_unloading_platforms(self, hass, monkeypatch):
        unload = AsyncMock(return_value=True)
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
        mqtt_client = AsyncMock()
        entry = _entry()
        entry.add_to_hass(hass)
        entry.runtime_data = integration.JackeryHomeCloudRuntime(
            client=object(), coordinator=object(), mqtt_client=mqtt_client
        )

        result = await integration.async_unload_entry(hass, entry)

        assert result is True
        mqtt_client.async_stop.assert_awaited_once()
        unload.assert_awaited_once()


class TestAsyncMigrateEntry:
    async def test_unsupported_version_returns_false_without_changes(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com"},
            version=2,  # not _CONFIG_ENTRY_VERSION (1)
            minor_version=1,
        )
        entry.add_to_hass(hass)

        result = await integration.async_migrate_entry(hass, entry)

        assert result is False

    async def test_derives_phone_uid_when_missing(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"},
            options={
                CONF_ENABLE_MQTT: False,
                CONF_CRYPTO_KEY: "",
                CONF_MQTT_DEBUG_RAW: False,
                CONF_MQTT_POLL_INTERVAL: 60,
            },
            version=1,
            minor_version=4,
        )
        entry.add_to_hass(hass)

        result = await integration.async_migrate_entry(hass, entry)

        assert result is True
        assert entry.data[CONF_PHONE_UID].startswith("ha-")

    async def test_legacy_top_level_selected_systems_moved_to_options(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_ACCOUNT: "user@example.com",
                CONF_PASSWORD: "pw",
                CONF_PHONE_UID: "ha-1",
                CONF_SELECTED_SYSTEMS: ["1", "2"],  # legacy location
            },
            options={
                CONF_ENABLE_MQTT: False,
                CONF_CRYPTO_KEY: "",
                CONF_MQTT_DEBUG_RAW: False,
                CONF_MQTT_POLL_INTERVAL: 60,
            },
            version=1,
            minor_version=4,
        )
        entry.add_to_hass(hass)

        await integration.async_migrate_entry(hass, entry)

        assert CONF_SELECTED_SYSTEMS not in entry.data
        assert entry.options[CONF_SELECTED_SYSTEMS] == ["1", "2"]
        # No CONF_MQTT_SYSTEM_ID stored yet -> defaults to the first system
        # in the now-migrated selection (see async_migrate_entry).
        assert entry.options[CONF_MQTT_SYSTEM_ID] == "1"

    async def test_missing_option_defaults_are_filled_in(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options={},  # nothing set
            version=1,
            minor_version=4,
        )
        entry.add_to_hass(hass)

        await integration.async_migrate_entry(hass, entry)

        assert entry.options[CONF_ENABLE_MQTT] is False
        assert entry.options[CONF_CRYPTO_KEY] == ""
        assert entry.options[CONF_MQTT_DEBUG_RAW] is False
        assert CONF_MQTT_POLL_INTERVAL in entry.options
        # No CONF_SELECTED_SYSTEMS at all -> nothing to default the MQTT
        # system to.
        assert entry.options[CONF_MQTT_SYSTEM_ID] is None

    async def test_seeds_mqtt_system_id_from_first_selected_system(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options={CONF_SELECTED_SYSTEMS: ["2", "1"]},
            version=1,
            minor_version=4,
        )
        entry.add_to_hass(hass)

        await integration.async_migrate_entry(hass, entry)

        assert entry.options[CONF_MQTT_SYSTEM_ID] == "2"

    async def test_leaves_existing_mqtt_system_id_untouched(self, hass):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options={CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_MQTT_SYSTEM_ID: "2"},
            version=1,
            minor_version=4,
        )
        entry.add_to_hass(hass)

        await integration.async_migrate_entry(hass, entry)

        assert entry.options[CONF_MQTT_SYSTEM_ID] == "2"

    async def test_already_fully_migrated_entry_is_left_unchanged(self, hass, monkeypatch):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options={
                CONF_ENABLE_MQTT: False,
                CONF_CRYPTO_KEY: "",
                CONF_MQTT_DEBUG_RAW: False,
                CONF_MQTT_POLL_INTERVAL: 60,
                CONF_MQTT_SYSTEM_ID: None,
            },
            version=1,
            minor_version=5,
        )
        entry.add_to_hass(hass)
        update = AsyncMock()
        # async_update_entry is sync in HA core, but patching to detect any call.
        monkeypatch.setattr(hass.config_entries, "async_update_entry", lambda *a, **k: update())

        result = await integration.async_migrate_entry(hass, entry)

        assert result is True
        update.assert_not_called()
