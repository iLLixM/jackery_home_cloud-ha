"""Config flow for Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from .crypto_utils import decrypt_text
from .const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_DEBUG_RAW,
    CONF_MQTT_POLL_INTERVAL,
    CONF_MQTT_TLS_INSECURE,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DEFAULT_BASE_URL,
    DEFAULT_ENABLE_MQTT,
    DEFAULT_MQTT_DEBUG_RAW,
    DEFAULT_MQTT_POLL_INTERVAL_SECONDS,
    DEFAULT_MQTT_TLS_INSECURE,
    DOMAIN,
    MQTT_POLL_INTERVAL_MAX_SECONDS,
    MQTT_POLL_INTERVAL_MIN_SECONDS,
)
from .exceptions import (
    JackeryCryptoError,
    JackeryHomeApiError,
    JackeryHomeAuthError,
    JackeryHomeCryptoError,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_credentials(
    hass: HomeAssistant,
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Validate credentials and return discovered systems plus raw MQTT config."""
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
    mqtt_config = await client.async_get_mqtt_credentials()
    return app_user, systems, mqtt_config


def _mqtt_config_requires_crypto_key(mqtt_config: Mapping[str, Any] | None) -> bool:
    """Return True if MQTT setup still depends on the legacy encoded credential flow."""
    if not isinstance(mqtt_config, Mapping):
        return True
    return not bool(mqtt_config.get("_password_is_plaintext"))


def _system_label(system: Mapping[str, Any]) -> str:
    system_id = str(system.get("id", ""))
    name = str(system.get("name") or system.get("systemNo") or system_id)
    system_no = str(system.get("systemNo") or system_id)
    return f"{name} ({system_no})"


def _system_options(systems: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"value": str(system["id"]), "label": _system_label(system)}
        for system in systems
        if system.get("id") is not None
    ]



def _credential_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build the initial credential schema with an automatically generated phone UID."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=str(defaults.get(CONF_ACCOUNT, ""))): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _reconfigure_credential_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build the reconfigure credential schema with an editable phone UID."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=str(defaults.get(CONF_ACCOUNT, ""))): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(
                CONF_PHONE_UID,
                default=str(defaults.get(CONF_PHONE_UID, "")),
            ): str,
        }
    )


def _systems_mqtt_schema(
    systems: Sequence[Mapping[str, Any]],
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    """Build the combined systems + MQTT options schema."""
    defaults = defaults or {}
    options: list[selector.SelectOptionDict] = []
    default_selection = list(defaults.get(CONF_SELECTED_SYSTEMS, []))

    for system in systems:
        system_id = str(system.get("systemId") or system.get("id") or "")
        if not system_id:
            continue
        system_name = str(
            system.get("systemName")
            or system.get("name")
            or system.get("systemNo")
            or system_id
        )
        label = system_name
        system_no = str(system.get("systemNo") or "")
        if system_no and system_no not in system_name:
            label = f"{system_name} ({system_no})"
        options.append(selector.SelectOptionDict(value=system_id, label=label))

    return vol.Schema(
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
            ),
            vol.Required(
                CONF_ENABLE_MQTT,
                default=bool(defaults.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)),
            ): bool,
            vol.Optional(
                CONF_MQTT_TLS_INSECURE,
                default=bool(defaults.get(CONF_MQTT_TLS_INSECURE, DEFAULT_MQTT_TLS_INSECURE)),
            ): bool,
            vol.Optional(
                CONF_MQTT_DEBUG_RAW,
                default=bool(defaults.get(CONF_MQTT_DEBUG_RAW, DEFAULT_MQTT_DEBUG_RAW)),
            ): bool,
            vol.Optional(
                CONF_MQTT_POLL_INTERVAL,
                default=int(
                    defaults.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS)
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MQTT_POLL_INTERVAL_MIN_SECONDS,
                    max=MQTT_POLL_INTERVAL_MAX_SECONDS,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


def _systems_schema(
    systems: list[dict[str, Any]],
    current_selection: list[str],
) -> vol.Schema:
    """Build the system selection step schema."""
    options = _system_options(systems)
    available_ids = {option["value"] for option in options}
    default_selection = [
        str(system_id)
        for system_id in current_selection
        if str(system_id) in available_ids
    ] or [option["value"] for option in options]

    return vol.Schema(
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
            ),
        }
    )


class JackeryHomeCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Jackery Home Cloud."""

    VERSION = 1
    MINOR_VERSION = 4

    def __init__(self) -> None:
        self._pending_entry_data: dict[str, Any] = {}
        self._pending_options: dict[str, Any] = {}
        self._discovered_systems: list[dict[str, Any]] = []
        self._discovered_mqtt_config: dict[str, Any] = {}
        self._entry_title: str = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return JackeryHomeCloudOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            prepared_input = self._prepare_credential_input(user_input)
            try:
                app_user, systems, mqtt_config = await _validate_credentials(self.hass, prepared_input)
            except JackeryHomeAuthError:
                errors["base"] = "invalid_auth"
            except JackeryHomeApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while validating Jackery credentials")
                errors["base"] = "unknown"
            else:
                if not systems:
                    errors["base"] = "no_systems"
                else:
                    account = str(app_user.get("email") or prepared_input[CONF_ACCOUNT]).lower()
                    await self.async_set_unique_id(account)
                    self._abort_if_unique_id_configured()
                    self._pending_entry_data = {
                        CONF_ACCOUNT: prepared_input[CONF_ACCOUNT],
                        CONF_PASSWORD: prepared_input[CONF_PASSWORD],
                        CONF_PHONE_UID: prepared_input[CONF_PHONE_UID],
                    }
                    self._pending_options = {
                        CONF_ENABLE_MQTT: bool(prepared_input.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)),
                    }
                    self._discovered_systems = systems
                    self._discovered_mqtt_config = mqtt_config
                    self._entry_title = account
                    return await self.async_step_systems()

        defaults = {
            CONF_ACCOUNT: self._pending_entry_data.get(CONF_ACCOUNT, ""),
        }
        return self.async_show_form(
            step_id="user",
            data_schema=_credential_schema(defaults),
            errors=errors,
        )

    async def async_step_systems(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Combined initial systems + MQTT options step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_systems = [
                str(system_id)
                for system_id in user_input.get(CONF_SELECTED_SYSTEMS, [])
                if str(system_id)
            ]
            enable_mqtt = bool(user_input.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT))
            crypto_key = str(self._pending_options.get(CONF_CRYPTO_KEY, "")).strip()
            mqtt_debug_raw = bool(user_input.get(CONF_MQTT_DEBUG_RAW, False))
            mqtt_tls_insecure = bool(user_input.get(CONF_MQTT_TLS_INSECURE, False))
            mqtt_poll_interval = int(
                user_input.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS)
            )

            if not selected_systems:
                errors["base"] = "no_system_selected"
            elif enable_mqtt:
                requires_crypto_key = _mqtt_config_requires_crypto_key(self._discovered_mqtt_config)
                if requires_crypto_key and not crypto_key:
                    errors["base"] = "missing_crypto_key"
                elif requires_crypto_key:
                    try:
                        await JackeryApiClient(
                            session=async_get_clientsession(self.hass),
                            base_url=DEFAULT_BASE_URL,
                        ).async_build_mqtt_credentials(
                            crypto_key,
                            self._discovered_mqtt_config,
                        )
                    except (JackeryHomeCryptoError, JackeryCryptoError):
                        errors["base"] = "invalid_crypto_key"

            if not errors:
                self._pending_options.update(
                    {
                        CONF_SELECTED_SYSTEMS: selected_systems,
                        CONF_ENABLE_MQTT: enable_mqtt,
                        CONF_MQTT_TLS_INSECURE: mqtt_tls_insecure,
                        CONF_MQTT_DEBUG_RAW: mqtt_debug_raw,
                        CONF_MQTT_POLL_INTERVAL: mqtt_poll_interval,
                    }
                )
                if crypto_key:
                    self._pending_options[CONF_CRYPTO_KEY] = crypto_key
                return self.async_create_entry(
                    title=self._entry_title,
                    data=self._pending_entry_data,
                    options=self._pending_options,
                )

        defaults = {
            CONF_SELECTED_SYSTEMS: self._pending_options.get(
                CONF_SELECTED_SYSTEMS,
                [
                    str(system.get("systemId") or system.get("id"))
                    for system in self._discovered_systems
                    if system.get("systemId") or system.get("id")
                ],
            ),
            CONF_ENABLE_MQTT: self._pending_options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT),
            CONF_MQTT_DEBUG_RAW: self._pending_options.get(CONF_MQTT_DEBUG_RAW, DEFAULT_MQTT_DEBUG_RAW),
            CONF_MQTT_TLS_INSECURE: self._pending_options.get(CONF_MQTT_TLS_INSECURE, DEFAULT_MQTT_TLS_INSECURE),
            CONF_MQTT_POLL_INTERVAL: self._pending_options.get(
                CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS
            ),
        }
        return self.async_show_form(
            step_id="systems",
            data_schema=_systems_mqtt_schema(self._discovered_systems, defaults),
            errors=errors,
        )

    async def async_step_reauth(self, _: Mapping[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._get_reauth_entry()
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            )

        updated = {
            CONF_ACCOUNT: entry.data[CONF_ACCOUNT],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
            CONF_PHONE_UID: entry.data[CONF_PHONE_UID],
        }

        try:
            await _validate_credentials(self.hass, updated)
        except JackeryHomeAuthError:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
                errors={"base": "invalid_auth"},
            )
        except JackeryHomeApiError:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
                errors={"base": "cannot_connect"},
            )

        self.hass.config_entries.async_update_entry(entry, data=updated)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Symmetric reconfigure flow step 1: credentials + MQTT enable."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            prepared_input = self._prepare_credential_input(user_input)
            try:
                _, systems, mqtt_config = await _validate_credentials(self.hass, prepared_input)
            except JackeryHomeAuthError:
                errors["base"] = "invalid_auth"
            except JackeryHomeApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while reconfiguring Jackery credentials")
                errors["base"] = "unknown"
            else:
                if not systems:
                    errors["base"] = "no_systems"
                else:
                    self._pending_entry_data = {
                        CONF_ACCOUNT: prepared_input[CONF_ACCOUNT],
                        CONF_PASSWORD: prepared_input[CONF_PASSWORD],
                        CONF_PHONE_UID: prepared_input[CONF_PHONE_UID],
                    }
                    self._pending_options = dict(entry.options)
                    self._discovered_systems = systems
                    self._discovered_mqtt_config = mqtt_config
                    return await self.async_step_reconfigure_systems()

        defaults = {
            CONF_ACCOUNT: entry.data.get(CONF_ACCOUNT, ""),
            CONF_PHONE_UID: entry.data.get(CONF_PHONE_UID, ""),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_credential_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure_systems(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Combined reconfigure systems + MQTT options step."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            selected_systems = [
                str(system_id)
                for system_id in user_input.get(CONF_SELECTED_SYSTEMS, [])
                if str(system_id)
            ]
            enable_mqtt = bool(user_input.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT))
            crypto_key = str(self._pending_options.get(CONF_CRYPTO_KEY, entry.options.get(CONF_CRYPTO_KEY, ""))).strip()
            mqtt_debug_raw = bool(user_input.get(CONF_MQTT_DEBUG_RAW, False))
            mqtt_tls_insecure = bool(user_input.get(CONF_MQTT_TLS_INSECURE, False))
            mqtt_poll_interval = int(
                user_input.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS)
            )

            if not selected_systems:
                errors["base"] = "no_system_selected"
            elif enable_mqtt:
                requires_crypto_key = _mqtt_config_requires_crypto_key(self._discovered_mqtt_config)
                if requires_crypto_key and not crypto_key:
                    errors["base"] = "missing_crypto_key"
                elif requires_crypto_key:
                    try:
                        await JackeryApiClient(
                            session=async_get_clientsession(self.hass),
                            base_url=DEFAULT_BASE_URL,
                        ).async_build_mqtt_credentials(
                            crypto_key,
                            self._discovered_mqtt_config,
                        )
                    except (JackeryHomeCryptoError, JackeryCryptoError):
                        errors["base"] = "invalid_crypto_key"

            if not errors:
                new_data = dict(entry.data)
                new_data.update(
                    {
                        CONF_ACCOUNT: self._pending_entry_data[CONF_ACCOUNT],
                        CONF_PASSWORD: self._pending_entry_data[CONF_PASSWORD],
                        CONF_PHONE_UID: self._pending_entry_data[CONF_PHONE_UID],
                    }
                )
                new_options = dict(self._pending_options)
                new_options.update(
                    {
                        CONF_SELECTED_SYSTEMS: selected_systems,
                        CONF_ENABLE_MQTT: enable_mqtt,
                        CONF_MQTT_TLS_INSECURE: mqtt_tls_insecure,
                        CONF_MQTT_DEBUG_RAW: mqtt_debug_raw,
                        CONF_MQTT_POLL_INTERVAL: mqtt_poll_interval,
                    }
                )
                if crypto_key:
                    new_options[CONF_CRYPTO_KEY] = crypto_key
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=new_data,
                    options=new_options,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        defaults = {
            CONF_SELECTED_SYSTEMS: self._pending_options.get(
                CONF_SELECTED_SYSTEMS,
                entry.options.get(CONF_SELECTED_SYSTEMS, []),
            ),
            CONF_ENABLE_MQTT: self._pending_options.get(
                CONF_ENABLE_MQTT,
                entry.options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT),
            ),
            CONF_MQTT_DEBUG_RAW: self._pending_options.get(
                CONF_MQTT_DEBUG_RAW,
                entry.options.get(CONF_MQTT_DEBUG_RAW, DEFAULT_MQTT_DEBUG_RAW),
            ),
            CONF_MQTT_TLS_INSECURE: self._pending_options.get(
                CONF_MQTT_TLS_INSECURE,
                entry.options.get(CONF_MQTT_TLS_INSECURE, DEFAULT_MQTT_TLS_INSECURE),
            ),
            CONF_MQTT_POLL_INTERVAL: self._pending_options.get(
                CONF_MQTT_POLL_INTERVAL,
                entry.options.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS),
            ),
        }
        return self.async_show_form(
            step_id="reconfigure_systems",
            data_schema=_systems_mqtt_schema(self._discovered_systems, defaults),
            errors=errors,
        )

    def _prepare_credential_input(self, user_input: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize the first-step credentials and connection toggles."""
        account = str(user_input[CONF_ACCOUNT]).strip()
        password = str(user_input[CONF_PASSWORD])
        phone_uid = str(user_input.get(CONF_PHONE_UID, "")).strip() or build_phone_uid(account)
        return {
            CONF_ACCOUNT: account,
            CONF_PASSWORD: password,
            CONF_PHONE_UID: phone_uid,
            CONF_ENABLE_MQTT: bool(user_input.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)),
        }


class JackeryHomeCloudOptionsFlow(OptionsFlowWithReload):
    """Compatibility options flow.

    The main user-facing path should now be the symmetric reconfigure flow.
    This flow remains available and keeps all MQTT options on one page.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        systems = self._systems_from_runtime_cache()
        mqtt_config: dict[str, Any] = {}

        if not systems:
            try:
                _, systems, mqtt_config = await _validate_credentials(
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
            except Exception:
                _LOGGER.exception("Unexpected exception while loading Jackery system options")
                errors["base"] = "unknown"
        else:
            runtime = getattr(self.config_entry, "runtime_data", None)
            coordinator = getattr(runtime, "coordinator", None)
            mqtt_config = getattr(coordinator, "mqtt_credentials", {}) if coordinator else {}

        if systems and user_input is not None:
            selected = [str(item) for item in user_input.get(CONF_SELECTED_SYSTEMS, [])]
            enable_mqtt = bool(user_input.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT))
            crypto_key = str(self.config_entry.options.get(CONF_CRYPTO_KEY, "")).strip()
            mqtt_debug_raw = bool(user_input.get(CONF_MQTT_DEBUG_RAW, DEFAULT_MQTT_DEBUG_RAW))
            mqtt_tls_insecure = bool(user_input.get(CONF_MQTT_TLS_INSECURE, False))
            mqtt_poll_interval = int(
                user_input.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS)
            )

            if not selected:
                errors["base"] = "no_system_selected"
            elif enable_mqtt:
                requires_crypto_key = _mqtt_config_requires_crypto_key(mqtt_config)
                if requires_crypto_key and not crypto_key:
                    errors["base"] = "missing_crypto_key"
                elif requires_crypto_key and mqtt_config.get("mqttPassword"):
                    try:
                        decrypt_text(str(mqtt_config["mqttPassword"]), crypto_key)
                    except (JackeryHomeCryptoError, JackeryCryptoError):
                        errors["base"] = "invalid_crypto_key"

            if not errors:
                data = dict(self.config_entry.options)
                data.update(
                    {
                        CONF_SELECTED_SYSTEMS: selected,
                        CONF_ENABLE_MQTT: enable_mqtt,
                        CONF_MQTT_TLS_INSECURE: mqtt_tls_insecure,
                        CONF_MQTT_DEBUG_RAW: mqtt_debug_raw,
                        CONF_MQTT_POLL_INTERVAL: mqtt_poll_interval,
                    }
                )
                if crypto_key:
                    data[CONF_CRYPTO_KEY] = crypto_key
                return self.async_create_entry(data=data)

        current_selection = self.config_entry.options.get(CONF_SELECTED_SYSTEMS, [])
        current_options = {
            CONF_ENABLE_MQTT: self.config_entry.options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT),
            CONF_MQTT_TLS_INSECURE: self.config_entry.options.get(CONF_MQTT_TLS_INSECURE, DEFAULT_MQTT_TLS_INSECURE),
            CONF_MQTT_DEBUG_RAW: self.config_entry.options.get(CONF_MQTT_DEBUG_RAW, DEFAULT_MQTT_DEBUG_RAW),
            CONF_MQTT_POLL_INTERVAL: self.config_entry.options.get(
                CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS
            ),
        }

        schema = vol.Schema(
            {
                **_systems_schema(systems, list(current_selection)).schema,
                vol.Required(CONF_ENABLE_MQTT, default=bool(current_options[CONF_ENABLE_MQTT])): bool,
                vol.Optional(
                    CONF_MQTT_TLS_INSECURE,
                    default=bool(current_options[CONF_MQTT_TLS_INSECURE]),
                ): bool,
                vol.Optional(
                    CONF_MQTT_DEBUG_RAW,
                    default=bool(current_options[CONF_MQTT_DEBUG_RAW]),
                ): bool,
                vol.Optional(
                    CONF_MQTT_POLL_INTERVAL,
                    default=int(current_options[CONF_MQTT_POLL_INTERVAL]),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MQTT_POLL_INTERVAL_MIN_SECONDS,
                        max=MQTT_POLL_INTERVAL_MAX_SECONDS,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    def _systems_from_runtime_cache(self) -> list[dict[str, Any]]:
        runtime = getattr(self.config_entry, "runtime_data", None)
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is None or not getattr(coordinator, "data", None):
            return []

        systems_by_id = coordinator.data.get("systems", {})
        systems: list[dict[str, Any]] = []
        for system_bundle in systems_by_id.values():
            if not isinstance(system_bundle, dict):
                continue
            system = system_bundle.get("system", {})
            if isinstance(system, dict):
                systems.append(system)
        return systems
