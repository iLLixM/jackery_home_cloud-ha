"""The Jackery Home Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.auth import build_phone_uid
from .api.client import JackeryApiClient
from .const import (
    CONF_ACCOUNT,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DEFAULT_BASE_URL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import JackeryHomeCloudCoordinator

_LOGGER = logging.getLogger(__name__)

_CONFIG_ENTRY_VERSION = 1
_CONFIG_ENTRY_MINOR_VERSION = 2


@dataclass(slots=True)
class JackeryHomeCloudRuntime:
    """Runtime objects stored for each config entry."""

    client: JackeryApiClient
    coordinator: JackeryHomeCloudCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jackery Home Cloud from a config entry."""
    session = async_get_clientsession(hass)
    client = JackeryApiClient(session=session, base_url=DEFAULT_BASE_URL)
    coordinator = JackeryHomeCloudCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = JackeryHomeCloudRuntime(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries to the current data layout.

    Migration goals:
    - move selected systems from entry.data to entry.options
    - ensure the config entry minor version reflects the new layout
    """
    _LOGGER.debug(
        "Migrating Jackery Home Cloud entry %s from version=%s minor_version=%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )

    if entry.version != _CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Unsupported config entry version %s for %s",
            entry.version,
            DOMAIN,
        )
        return False

    data = dict(entry.data)
    options = dict(entry.options)
    changed = False

    if CONF_PHONE_UID not in data and CONF_ACCOUNT in data:
        data[CONF_PHONE_UID] = build_phone_uid(str(data[CONF_ACCOUNT]))
        changed = True

    legacy_selected_systems = data.pop(CONF_SELECTED_SYSTEMS, None)
    if legacy_selected_systems is not None and CONF_SELECTED_SYSTEMS not in options:
        if isinstance(legacy_selected_systems, (list, tuple, set)):
            options[CONF_SELECTED_SYSTEMS] = [str(item) for item in legacy_selected_systems]
        else:
            options[CONF_SELECTED_SYSTEMS] = [str(legacy_selected_systems)]
        changed = True

    if entry.minor_version < _CONFIG_ENTRY_MINOR_VERSION:
        changed = True

    if changed:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=_CONFIG_ENTRY_VERSION,
            minor_version=_CONFIG_ENTRY_MINOR_VERSION,
        )

    _LOGGER.debug("Finished migrating Jackery Home Cloud entry %s", entry.entry_id)
    return True
