"""Tests for config_flow.py's full async_step_* flow (Family E / Tier 2).

Uses the real `hass`/`MockConfigEntry`/flow-manager machinery from
pytest-homeassistant-custom-component (needs tests/conftest.py's
`enable_custom_integrations` fixture). REST calls are intercepted by
monkeypatching `config_flow.async_get_clientsession` to return an
in-repo fake aiohttp session (same pattern as
tests/unit/test_api_client.py's `_FakeSession`) so no real network is
used. `custom_components.jackery_home_cloud.async_setup_entry` (the
integration's own runtime setup, which opens a real MQTT connection) is
also patched to a no-op for every test in this module: creating or
reloading a config entry through the flow manager automatically triggers
component setup, which is out of scope for flow-logic tests and would
otherwise try real network/MQTT I/O.

Regression coverage for two real bugs found while writing these tests
(both fixed in this same change, in custom_components/jackery_home_cloud/
config_flow.py):
  1. `decrypt_text` was used at the options-flow crypto-key-validation
     line without being imported at all -> NameError in production.
  2. That same validation (and the equivalent one in
     async_step_systems/async_step_reconfigure_systems) only caught
     `JackeryHomeCryptoError`, but `decrypt_text` actually raises the
     unrelated `JackeryCryptoError` on a wrong/corrupt key - so a wrong
     (but present) crypto key crashed the flow step with an unhandled
     exception instead of showing "invalid_crypto_key".
"""

from __future__ import annotations

import json as json_module
from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.jackery_home_cloud as integration
import custom_components.jackery_home_cloud.config_flow as config_flow
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DOMAIN,
)
from custom_components.jackery_home_cloud.crypto_utils import encrypt_text

VALID_KEY = "0123456789abcdef"
WRONG_KEY = "fedcba9876543210"

_THREE_SYSTEMS = [
    {"id": "1", "systemId": "1", "name": "A", "systemNo": "SN1"},
    {"id": "2", "systemId": "2", "name": "B", "systemNo": "SN2"},
    {"id": "3", "systemId": "3", "name": "C", "systemNo": "SN3"},
]


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequestCM:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Dispatches by URL substring - simpler than strict call ordering
    since these flow steps call _validate_credentials repeatedly across
    steps/retries."""

    def __init__(self, routes: dict[str, tuple[int, str]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def request(self, *, method, url, headers, json=None, timeout=None):
        self.calls.append(url)
        for substring, (status, body) in self.routes.items():
            if substring in url:
                return _FakeRequestCM(_FakeResponse(status, body))
        return _FakeRequestCM(_FakeResponse(404, json_module.dumps({"success": False, "msg": "no route"})))


def _ok(result) -> str:
    return json_module.dumps({"success": True, "code": 0, "result": result})


def _default_routes(*, mqtt_plaintext: bool = True, systems=None) -> dict[str, tuple[int, str]]:
    systems = systems if systems is not None else [
        {"id": "1", "systemId": "1", "name": "Home", "systemNo": "SN1"}
    ]
    if mqtt_plaintext:
        mqtt_v2_body = _ok({"mqttServer": "broker.test", "mqttUserName": "u", "mqttPassword": "plain-secret"})
        mqtt_v1_body = _ok({})
    else:
        mqtt_v2_body = _ok({})
        encrypted = encrypt_text("legacy-secret", VALID_KEY)
        mqtt_v1_body = _ok({"mqttServer": "broker.test", "mqttUserName": "u", "mqttPassword": encrypted})
    return {
        "/auth/login": (200, _ok({"accessToken": "tok", "refreshToken": "ref", "tokenPrefix": "Bearer"})),
        "/appUser/getOne": (200, _ok({"email": "user@example.com"})),
        "/system/listByUserV2": (200, _ok(systems)),
        "/v2/idc/config/mqttServer": (200, mqtt_v2_body),
        "/v1/idc/config/mqttServer": (200, mqtt_v1_body),
    }


@pytest.fixture(autouse=True)
def _no_real_component_setup(monkeypatch):
    """Creating/reloading a config entry via the flow manager triggers
    the integration's real async_setup_entry - stub it out so flow tests
    never touch real MQTT/network."""
    monkeypatch.setattr(integration, "async_setup_entry", AsyncMock(return_value=True))
    monkeypatch.setattr(integration, "async_unload_entry", AsyncMock(return_value=True))


def _patch_session(monkeypatch, routes: dict[str, tuple[int, str]]) -> _FakeSession:
    session = _FakeSession(routes)
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda hass: session)
    return session


class TestUserStep:
    async def test_valid_credentials_creates_entry(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes())

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        assert result["step_id"] == "systems"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ACCOUNT] == "user@example.com"
        assert result["options"][CONF_MQTT_SYSTEM_ID] is None

    async def test_invalid_credentials_shows_error(self, hass, monkeypatch):
        routes = _default_routes()
        routes["/auth/login"] = (401, _ok({}))
        _patch_session(monkeypatch, routes)

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "wrong"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_api_error_shows_cannot_connect(self, hass, monkeypatch):
        routes = _default_routes()
        routes["/auth/login"] = (500, _ok({}))
        _patch_session(monkeypatch, routes)

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_already_configured_account_aborts(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes())
        existing = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
        )
        existing.add_to_hass(hass)

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestSystemsStep:
    async def test_no_system_selected_shows_error(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes())
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: [], CONF_ENABLE_MQTT: False}
        )
        assert result["errors"] == {"base": "no_system_selected"}

    async def test_mqtt_enabled_on_legacy_endpoint_requires_crypto_key(self, hass, monkeypatch):
        """The crypto key field only exists in the reconfigure/options
        schemas (sourced from a previously stored entry), never in the
        initial systems-step schema - so enabling MQTT against a legacy
        (non-plaintext) endpoint on first setup always requires a
        follow-up reconfigure/options step to supply the key."""
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=False))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["errors"] == {"base": "missing_crypto_key"}

    async def test_plaintext_mqtt_succeeds_without_crypto_key(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=True))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_ENABLE_MQTT] is True
        assert result["options"][CONF_MQTT_SYSTEM_ID] == "1"


class TestMqttSystemStep:
    async def test_two_selected_systems_with_mqtt_enabled_shows_mqtt_system_step(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mqtt_system"

    async def test_mqtt_system_dropdown_is_restricted_to_selected_systems(self, hass, monkeypatch):
        """The 3rd discovered system wasn't selected in the previous step,
        so it must not be offered as an MQTT system candidate."""
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        select_selector = result["data_schema"].schema[CONF_MQTT_SYSTEM_ID]
        offered_ids = {option["value"] for option in select_selector.config["options"]}
        assert offered_ids == {"1", "2"}

    async def test_two_selected_systems_with_mqtt_disabled_skips_mqtt_system_step(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_MQTT_SYSTEM_ID] is None

    async def test_submitting_mqtt_system_step_creates_entry_with_chosen_system(self, hass, monkeypatch):
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["step_id"] == "mqtt_system"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MQTT_SYSTEM_ID: "2"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_MQTT_SYSTEM_ID] == "2"
        assert result["options"][CONF_SELECTED_SYSTEMS] == ["1", "2"]


class TestReauthConfirm:
    async def _existing_entry(self, hass) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "old-pw", CONF_PHONE_UID: "ha-1"},
        )
        entry.add_to_hass(hass)
        return entry

    async def test_successful_reauth_updates_entry_in_place(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        _patch_session(monkeypatch, _default_routes())

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-pw"}
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_PASSWORD] == "new-pw"

    async def test_failed_reauth_shows_error_and_does_not_change_entry(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        routes = _default_routes()
        routes["/auth/login"] = (401, _ok({}))
        _patch_session(monkeypatch, routes)

        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "still-wrong"}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        assert entry.data[CONF_PASSWORD] == "old-pw"


class TestReconfigure:
    async def _existing_entry(self, hass, *, options=None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "old-pw", CONF_PHONE_UID: "ha-1"},
            options=options or {},
        )
        entry.add_to_hass(hass)
        return entry

    async def test_reconfigure_updates_credentials_and_systems(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        _patch_session(monkeypatch, _default_routes())

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "new-pw"}
        )
        assert result["step_id"] == "reconfigure_systems"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_PASSWORD] == "new-pw"
        assert entry.options[CONF_SELECTED_SYSTEMS] == ["1"]
        # MQTT disabled, so the reconfigure step itself doesn't touch
        # CONF_MQTT_SYSTEM_ID - but async_reload() (triggered by this same
        # reconfigure) re-runs migration on this pre-existing entry, which
        # seeds it from the first selected system regardless of the MQTT
        # toggle (see async_migrate_entry).
        assert entry.options[CONF_MQTT_SYSTEM_ID] == "1"

    async def test_reconfigure_systems_missing_crypto_key(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)  # no stored crypto_key
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=False))

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["errors"] == {"base": "missing_crypto_key"}

    async def test_reconfigure_systems_wrong_stored_crypto_key_is_handled_gracefully(self, hass, monkeypatch):
        """Regression test: a stale/wrong crypto_key already stored on the
        entry, combined with a legacy MQTT endpoint, used to crash this
        step with an unhandled JackeryCryptoError instead of showing
        'invalid_crypto_key' (see module docstring, bug #2)."""
        entry = await self._existing_entry(hass, options={CONF_CRYPTO_KEY: WRONG_KEY})
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=False))

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_crypto_key"}


class TestReconfigureMqttSystem:
    async def _existing_entry(self, hass, *, options=None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "old-pw", CONF_PHONE_UID: "ha-1"},
            options=options or {},
        )
        entry.add_to_hass(hass)
        return entry

    async def _reach_reconfigure_systems(self, hass, entry, monkeypatch):
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"}
        )
        assert result["step_id"] == "reconfigure_systems"
        return result

    async def test_two_selected_systems_with_mqtt_enabled_shows_step(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        result = await self._reach_reconfigure_systems(hass, entry, monkeypatch)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure_mqtt_system"

    async def test_default_preselects_previously_configured_system_when_still_selected(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_MQTT_SYSTEM_ID: "2"})
        result = await self._reach_reconfigure_systems(hass, entry, monkeypatch)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["data_schema"]({})[CONF_MQTT_SYSTEM_ID] == "2"

    async def test_default_falls_back_to_first_selected_when_previous_system_deselected(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_MQTT_SYSTEM_ID: "3"})
        result = await self._reach_reconfigure_systems(hass, entry, monkeypatch)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["data_schema"]({})[CONF_MQTT_SYSTEM_ID] == "1"

    async def test_submitting_reconfigure_mqtt_system_step_updates_entry(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_MQTT_SYSTEM_ID: "1"})
        result = await self._reach_reconfigure_systems(hass, entry, monkeypatch)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["step_id"] == "reconfigure_mqtt_system"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MQTT_SYSTEM_ID: "2"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.options[CONF_MQTT_SYSTEM_ID] == "2"

    async def test_mqtt_disabled_preserves_previously_configured_system(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_MQTT_SYSTEM_ID: "2"})
        result = await self._reach_reconfigure_systems(hass, entry, monkeypatch)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.ABORT
        assert entry.options[CONF_MQTT_SYSTEM_ID] == "2"


class TestOptionsFlow:
    async def _existing_entry(self, hass, *, options=None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options=options or {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: False},
        )
        entry.add_to_hass(hass)
        return entry

    async def test_happy_path_updates_selected_systems(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        _patch_session(monkeypatch, _default_routes())

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_SELECTED_SYSTEMS] == ["1"]
        assert result["data"].get(CONF_MQTT_SYSTEM_ID) is None

    async def test_missing_crypto_key(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)  # no stored crypto_key
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=False))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["errors"] == {"base": "missing_crypto_key"}

    async def test_wrong_stored_crypto_key_is_handled_gracefully(self, hass, monkeypatch):
        """Regression test for bug #1 (decrypt_text NameError) and bug #2
        (wrong exception type caught): this exact branch - requires_crypto_key
        and mqtt_config.get("mqttPassword") - calls decrypt_text directly
        rather than through async_build_mqtt_credentials."""
        entry = await self._existing_entry(hass, options={CONF_CRYPTO_KEY: WRONG_KEY, CONF_SELECTED_SYSTEMS: ["1"]})
        _patch_session(monkeypatch, _default_routes(mqtt_plaintext=False))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_crypto_key"}


class TestOptionsFlowMqttSystem:
    async def _existing_entry(self, hass, *, options=None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-1"},
            options=options or {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: False},
        )
        entry.add_to_hass(hass)
        return entry

    async def test_two_selected_systems_with_mqtt_enabled_shows_step(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mqtt_system"

    async def test_default_falls_back_to_first_selected_when_previous_system_deselected(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_SELECTED_SYSTEMS: ["1", "3"], CONF_MQTT_SYSTEM_ID: "3"})
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["data_schema"]({})[CONF_MQTT_SYSTEM_ID] == "1"

    async def test_submitting_mqtt_system_step_updates_entry(self, hass, monkeypatch):
        entry = await self._existing_entry(hass)
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: True}
        )
        assert result["step_id"] == "mqtt_system"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MQTT_SYSTEM_ID: "2"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_MQTT_SYSTEM_ID] == "2"

    async def test_mqtt_disabled_preserves_previously_configured_system(self, hass, monkeypatch):
        entry = await self._existing_entry(hass, options={CONF_SELECTED_SYSTEMS: ["1"], CONF_MQTT_SYSTEM_ID: "1"})
        _patch_session(monkeypatch, _default_routes(systems=_THREE_SYSTEMS))

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SELECTED_SYSTEMS: ["1", "2"], CONF_ENABLE_MQTT: False}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_MQTT_SYSTEM_ID] == "1"
