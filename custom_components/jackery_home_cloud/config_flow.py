"""Config flow for Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.auth import build_phone_uid
from .api.client import JackeryApiClient
from .const import (
    CONF_ACCOUNT,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DEFAULT_BASE_URL,
    DOMAIN,
)
from .exceptions import JackeryHomeApiError, JackeryHomeAuthError

_LOGGER = logging.getLogger(__name__)


async def _validate_credentials(
    hass: HomeAssistant,
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate credentials and return the discovered systems."""
    client = JackeryApiClient(
        session=async_get_clientsession(hass),
        base_url=DEFAULT_BASE_URL,
    )

    await client.async_login(
        account=str(data[CONF_ACCOUNT]),
        password=str(data[CONF_PASSWORD]),
        phone_uid=str(data[CONF_PHONE_UID]),
    )
    app_user = await client.async_get_app_user()
    systems = await client.async_list_systems()
    return app_user, systems


def _system_label(system: Mapping[str, Any]) -> str:
    """Build a user-friendly label for a system selector option."""
    system_id = str(system.get("id", ""))
    name = str(system.get("name") or system.get("systemNo") or system_id)
    system_no = str(system.get("systemNo") or system_id)
    return f"{name} ({system_no})"


def _system_options(systems: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert system objects to selector options."""
    return [
        {"value": str(system["id"]), "label": _system_label(system)}
        for system in systems
        if system.get("id") is not None
    ]


def _credential_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build the credential form schema used by multiple flow steps."""
    defaults = defaults or {}
    account_default = defaults.get(CONF_ACCOUNT, "")
    phone_uid_default = defaults.get(CONF_PHONE_UID, "")
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=account_default): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_PHONE_UID, default=phone_uid_default): str,
        }
    )


class JackeryHomeCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Jackery Home Cloud."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._pending_entry_data: dict[str, Any] = {}
        self._discovered_systems: list[dict[str, Any]] = []
        self._entry_title: str = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the options flow handler."""
        return JackeryHomeCloudOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the first user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            prepared_input = self._prepare_credential_input(user_input)
            try:
                app_user, systems = await _validate_credentials(self.hass, prepared_input)
            except JackeryHomeAuthError:
                errors["base"] = "invalid_auth"
            except JackeryHomeApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive guard for unexpected issues
                _LOGGER.exception(
                    "Unexpected exception while validating Jackery credentials"
                )
                errors["base"] = "unknown"
            else:
                if not systems:
                    errors["base"] = "no_systems"
                else:
                    account = str(
                        app_user.get("email") or prepared_input[CONF_ACCOUNT]
                    ).lower()
                    await self.async_set_unique_id(account)
                    self._abort_if_unique_id_configured()
                    self._pending_entry_data = prepared_input
                    self._discovered_systems = systems
                    self._entry_title = account
                    return await self.async_step_systems()

        defaults = {CONF_PHONE_UID: ""}
        return self.async_show_form(
            step_id="user",
            data_schema=_credential_schema(defaults),
            errors=errors,
        )

    async def async_step_systems(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle system selection after successful login."""
        errors: dict[str, str] = {}
        options = _system_options(self._discovered_systems)
        default_selection = [option["value"] for option in options]

        if user_input is not None:
            selected = [str(item) for item in user_input.get(CONF_SELECTED_SYSTEMS, [])]
            if not selected:
                errors["base"] = "no_system_selected"
            else:
                return self.async_create_entry(
                    title=self._entry_title,
                    data=self._pending_entry_data,
                    options={CONF_SELECTED_SYSTEMS: selected},
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SELECTED_SYSTEMS,
                    default=default_selection,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="systems",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reauth(self, _: Mapping[str, Any]) -> FlowResult:
        """Start the re-authentication flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle re-authentication triggered by Home Assistant."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            prepared_input = self._prepare_credential_input(
                {
                    CONF_ACCOUNT: entry.data[CONF_ACCOUNT],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_PHONE_UID: user_input.get(CONF_PHONE_UID)
                    or entry.data[CONF_PHONE_UID],
                }
            )
            try:
                await _validate_credentials(self.hass, prepared_input)
            except JackeryHomeAuthError:
                errors["base"] = "invalid_auth"
            except JackeryHomeApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(entry.data[CONF_ACCOUNT]).lower())
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: prepared_input[CONF_PASSWORD],
                        CONF_PHONE_UID: prepared_input[CONF_PHONE_UID],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_PHONE_UID,
                    default=entry.data.get(CONF_PHONE_UID, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow users to update connection details without removing the entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            prepared_input = self._prepare_credential_input(user_input)
            if str(prepared_input[CONF_ACCOUNT]).lower() != str(
                entry.data[CONF_ACCOUNT]
            ).lower():
                errors["base"] = "account_mismatch"
            else:
                try:
                    await _validate_credentials(self.hass, prepared_input)
                except JackeryHomeAuthError:
                    errors["base"] = "invalid_auth"
                except JackeryHomeApiError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(str(entry.data[CONF_ACCOUNT]).lower())
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_ACCOUNT: prepared_input[CONF_ACCOUNT],
                            CONF_PASSWORD: prepared_input[CONF_PASSWORD],
                            CONF_PHONE_UID: prepared_input[CONF_PHONE_UID],
                        },
                    )

        defaults = {
            CONF_ACCOUNT: entry.data.get(CONF_ACCOUNT, ""),
            CONF_PHONE_UID: entry.data.get(CONF_PHONE_UID, ""),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credential_schema(defaults),
            errors=errors,
        )

    @staticmethod
    def _prepare_credential_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize account credentials from the flow input."""
        account = str(user_input[CONF_ACCOUNT]).strip()
        password = str(user_input[CONF_PASSWORD])
        phone_uid = (
            str(user_input.get(CONF_PHONE_UID, "")).strip() or build_phone_uid(account)
        )
        return {
            CONF_ACCOUNT: account,
            CONF_PASSWORD: password,
            CONF_PHONE_UID: phone_uid,
        }


class JackeryHomeCloudOptionsFlow(OptionsFlowWithReload):
    """Options flow for changing the selected systems."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options for an existing config entry."""
        errors: dict[str, str] = {}
        systems = self._systems_from_runtime_cache()

        if not systems:
            try:
                _, systems = await _validate_credentials(
                    self.hass,
                    {
                        CONF_ACCOUNT: self.config_entry.data[CONF_ACCOUNT],
                        CONF_PASSWORD: self.config_entry.data[CONF_PASSWORD],
                        CONF_PHONE_UID: self.config_entry.data[CONF_PHONE_UID],
                    },
                )
            except JackeryHomeAuthError:
                errors["base"] = "invalid_auth"
            except JackeryHomeApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive guard for unexpected issues
                _LOGGER.exception(
                    "Unexpected exception while loading Jackery system options"
                )
                errors["base"] = "unknown"

        if systems:
            options = _system_options(systems)
            available_ids = {option["value"] for option in options}
            current_selection = self.config_entry.options.get(CONF_SELECTED_SYSTEMS, [])
            default_selection = [
                str(system_id)
                for system_id in current_selection
                if str(system_id) in available_ids
            ] or [option["value"] for option in options]

            if user_input is not None:
                selected = [str(item) for item in user_input.get(CONF_SELECTED_SYSTEMS, [])]
                if not selected:
                    errors["base"] = "no_system_selected"
                else:
                    return self.async_create_entry(
                        data={CONF_SELECTED_SYSTEMS: selected},
                    )

            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_SELECTED_SYSTEMS,
                        default=default_selection,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            )
        else:
            stored_selection = self.config_entry.options.get(CONF_SELECTED_SYSTEMS, [])
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_SELECTED_SYSTEMS,
                        default=list(stored_selection),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": str(item), "label": str(item)}
                                for item in stored_selection
                            ],
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    def _systems_from_runtime_cache(self) -> list[dict[str, Any]]:
        """Read the latest discovered systems from runtime cache if available."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return []

        coordinator_data = getattr(runtime.coordinator, "data", None)
        if not coordinator_data:
            return []

        available_systems = coordinator_data.get("available_systems")
        if not isinstance(available_systems, dict):
            return []

        return [
            system
            for system in available_systems.values()
            if isinstance(system, dict)
        ]
