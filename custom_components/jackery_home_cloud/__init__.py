"""The Jackery Home Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api.auth import build_phone_uid
from .api.client import JackeryApiClient
from .const import (
    CONF_ACCOUNT,
    CONF_CRYPTO_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_DEBUG_RAW,
    CONF_MQTT_POLL_INTERVAL,
    CONF_MQTT_TLS_INSECURE,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DEFAULT_BASE_URL,
    DEFAULT_MQTT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MQTT_TOTALS_POLL_INTERVAL_SECONDS,
    PLATFORMS,
)
from .coordinator import JackeryHomeCloudCoordinator
from .exceptions import JackeryHomeCryptoError, JackeryHomeMqttError
from .mqtt_client import JackeryMqttClient

_LOGGER = logging.getLogger(__name__)

_CONFIG_ENTRY_VERSION = 1
_CONFIG_ENTRY_MINOR_VERSION = 4


@dataclass(slots=True)
class JackeryHomeCloudRuntime:
    """Runtime objects stored for each config entry."""

    client: JackeryApiClient
    coordinator: JackeryHomeCloudCoordinator
    mqtt_client: JackeryMqttClient | None = None


def _extract_main_device_serial(coordinator) -> str:
    """Extract the main device serial used as targeted MQTT identifier."""
    data = getattr(coordinator, "data", {}) or {}
    systems = data.get("systems", {}) if isinstance(data, dict) else {}

    for system_bundle in systems.values():
        if not isinstance(system_bundle, dict):
            continue

        # Prefer values already normalized by coordinator/system assembly.
        for key in ("main_device_serial", "system_no", "systemNo", "serial_number"):
            value = system_bundle.get(key)
            if value:
                return str(value).strip()

        system = system_bundle.get("system", {})
        if isinstance(system, dict):
            for key in ("systemNo", "deviceNo", "sn"):
                value = system.get(key)
                if value:
                    return str(value).strip()

        monitor = system_bundle.get("monitor", {})
        if isinstance(monitor, dict):
            energy_flow = monitor.get("energyFlowChartVO", {})
            if isinstance(energy_flow, dict):
                ems = energy_flow.get("emsGwVO", {})
                if isinstance(ems, dict):
                    value = ems.get("deviceNo") or ems.get("sn")
                    if value:
                        return str(value).strip()

        devices = system_bundle.get("devices", {})
        if isinstance(devices, dict):
            for device in devices.values():
                if not isinstance(device, dict):
                    continue
                product_key = str(device.get("productKey") or "").upper()
                model_key = str(device.get("modelKey") or "").upper()
                if "EMS" in product_key or "EMS" in model_key:
                    value = device.get("deviceNo") or device.get("sn")
                    if value:
                        return str(value).strip()

    return ""


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jackery Home Cloud from a config entry."""
    session = async_get_clientsession(hass)
    client = JackeryApiClient(session=session, base_url=DEFAULT_BASE_URL)
    coordinator = JackeryHomeCloudCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    runtime = JackeryHomeCloudRuntime(client=client, coordinator=coordinator)
    entry.runtime_data = runtime

    if entry.options.get(CONF_ENABLE_MQTT):
        crypto_key = str(entry.options.get(CONF_CRYPTO_KEY, "")).strip()
        try:
            mqtt_credentials = await client.async_build_mqtt_credentials(
                crypto_key,
                coordinator.mqtt_credentials,
            )
            mqtt_tls_insecure = bool(entry.options.get(CONF_MQTT_TLS_INSECURE, False))
            main_device_serial = _extract_main_device_serial(coordinator)
            _LOGGER.debug("Jackery MQTT TLS insecure option from config entry: %s", mqtt_tls_insecure)
            _LOGGER.debug("Jackery main device serial selected for targeted MQTT subscriptions: %s", main_device_serial)
            mqtt_client = JackeryMqttClient(
                hass,
                mqtt_credentials,
                device_serial=main_device_serial,
                debug_raw=bool(entry.options.get(CONF_MQTT_DEBUG_RAW, False)),
                tls_insecure=mqtt_tls_insecure,
                message_callback=coordinator.async_handle_mqtt_message,
                status_callback=coordinator.async_handle_mqtt_status,
            )
            await mqtt_client.async_start()
            runtime.mqtt_client = mqtt_client

            poll_interval_seconds = int(
                entry.options.get(CONF_MQTT_POLL_INTERVAL, DEFAULT_MQTT_POLL_INTERVAL_SECONDS)
            )

            async def _async_poll_fast_live_meters(_now) -> None:
                await coordinator.async_request_fast_live_meter_values()

            async def _async_poll_totals_live_meters(_now) -> None:
                await coordinator.async_request_totals_live_meter_values()

            entry.async_on_unload(
                async_track_time_interval(
                    hass,
                    _async_poll_fast_live_meters,
                    timedelta(seconds=poll_interval_seconds),
                )
            )
            entry.async_on_unload(
                async_track_time_interval(
                    hass,
                    _async_poll_totals_live_meters,
                    timedelta(seconds=MQTT_TOTALS_POLL_INTERVAL_SECONDS),
                )
            )
        except JackeryHomeCryptoError as err:
            _LOGGER.warning(
                "Failed to start Jackery MQTT foundation for %s because a legacy encoded credential still requires a valid integration crypto key: %s",
                entry.title,
                err,
            )
            await coordinator.async_handle_mqtt_status(
                {
                    "connected": False,
                    "connection_state": "disabled",
                    "error": str(err),
                }
            )
        except (JackeryHomeMqttError, Exception) as err:
            _LOGGER.warning(
                "Failed to start Jackery MQTT foundation for %s: %s",
                entry.title,
                err,
            )
            await coordinator.async_handle_mqtt_status(
                {
                    "connected": False,
                    "connection_state": "error",
                    "error": str(err),
                }
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime and runtime.mqtt_client is not None:
        await runtime.mqtt_client.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries to the current data layout."""
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

    if CONF_ENABLE_MQTT not in options:
        options[CONF_ENABLE_MQTT] = False
        changed = True
    if CONF_CRYPTO_KEY not in options:
        options[CONF_CRYPTO_KEY] = ""
        changed = True
    if CONF_MQTT_DEBUG_RAW not in options:
        options[CONF_MQTT_DEBUG_RAW] = False
        changed = True
    if CONF_MQTT_POLL_INTERVAL not in options:
        options[CONF_MQTT_POLL_INTERVAL] = DEFAULT_MQTT_POLL_INTERVAL_SECONDS
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
