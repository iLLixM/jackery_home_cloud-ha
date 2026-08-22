"""Select platform for Jackery Home Cloud MQTT-backed controls."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENABLE_MQTT,
    DOMAIN,
    MANUFACTURER,
    MODEL_NAME_MAP,
    MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE,
    MQTT_EMS_WORK_MODE_METER_ID,
    MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
)
from .coordinator import JackeryHomeCloudCoordinator

# Value mapping for MQTT_EMS_WORK_MODE_METER_ID (see const.py), using the same
# labels as the Jackery Home app.
MODE_OPTIONS: dict[str, str] = {
    "Self-consumption": "2",
    "Battery priority": "3",
    "Time of use": "5",
    "Intelligent mode": "7",
}
_MODE_VALUE_TO_OPTION: dict[str, str] = {value: label for label, value in MODE_OPTIONS.items()}

# Preset mapping for MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID (see const.py).
# Only these two presets have been observed - there may be more (e.g. a 3rd
# tier) not yet captured.
OUTPUT_POWER_LIMIT_OPTIONS: dict[str, str] = {
    "800 W": "0",
    "1500 W": "1",
}
_OUTPUT_POWER_LIMIT_VALUE_TO_OPTION: dict[str, str] = {
    value: label for label, value in OUTPUT_POWER_LIMIT_OPTIONS.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Jackery MQTT-backed work mode select for a config entry."""
    if not entry.options.get(CONF_ENABLE_MQTT):
        return

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or getattr(runtime, "mqtt_client", None) is None:
        return

    coordinator = runtime.coordinator
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}
    entities: list[JackeryWorkModeSelect | JackeryOutputPowerLimitSelect] = []

    for system_id, bundle in systems.items():
        if not coordinator.is_mqtt_system(system_id):
            continue
        if not isinstance(bundle, Mapping):
            continue
        device_sn = _extract_system_device_sn(bundle)
        if not device_sn:
            continue
        model_confirmed = coordinator.is_model_confirmed(system_id)
        if coordinator.supports_meter(system_id, MQTT_EMS_WORK_MODE_METER_ID):
            entity = JackeryWorkModeSelect(
                coordinator=coordinator,
                system_id=str(system_id),
                bundle=bundle,
                mqtt_client=runtime.mqtt_client,
                device_sn=device_sn,
            )
            if not model_confirmed:
                entity._attr_entity_registry_enabled_default = False
            entities.append(entity)
        if coordinator.supports_meter(system_id, MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID):
            entity = JackeryOutputPowerLimitSelect(
                coordinator=coordinator,
                system_id=str(system_id),
                bundle=bundle,
                mqtt_client=runtime.mqtt_client,
                device_sn=device_sn,
            )
            if not model_confirmed:
                entity._attr_entity_registry_enabled_default = False
            entities.append(entity)

    if entities:
        async_add_entities(entities)


class JackeryWorkModeSelect(CoordinatorEntity[JackeryHomeCloudCoordinator], SelectEntity):
    """MQTT-backed select for the Jackery work mode (see MODE_OPTIONS above).

    Experimental: the meter mapping was reverse engineered from observed
    traffic rather than official documentation. Verify the selected mode
    against the Jackery app before relying on it for automation.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "work_mode"
    _attr_icon = "mdi:battery-sync"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(MODE_OPTIONS.keys())

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the work mode select."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_work_mode"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial mode once the select entity is added."""
        await super().async_added_to_hass()
        await self._async_request_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated coordinator data."""
        self._bundle = dict(self._system_bundle or {})
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return availability based on device serial and MQTT connectivity."""
        return self.coordinator.is_control_available(self._system_id, self._device_sn)

    @property
    def current_option(self) -> str | None:
        """Return the last known work mode."""
        bundle = self._system_bundle or self._bundle or {}
        raw_value = bundle.get("work_mode_raw")
        if not isinstance(raw_value, str):
            return None
        return _MODE_VALUE_TO_OPTION.get(raw_value)

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    async def async_select_option(self, option: str) -> None:
        """Publish the desired work mode via MQTT and verify it was applied."""
        value = MODE_OPTIONS.get(option)
        if value is None:
            raise HomeAssistantError(f"Unknown Jackery work mode option: {option}")
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for work mode control.")

        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=MQTT_EMS_WORK_MODE_METER_ID,
            raw_value=value,
            bundle_key="work_mode_raw",
            timestamp_key="work_mode_raw_at",
            expected_bundle_value=value,
            refresh_group=self.coordinator.async_request_config_live_meter_values,
        )

    async def _async_request_state(self) -> None:
        """Actively request the current work mode via data_get."""
        if not self._device_sn:
            return
        topic = MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE.format(device_serial=self._device_sn)
        timestamp_ms = str(int(time.time() * 1000))
        payload = {
            "cmd": "data_get",
            "gw_sn": self._device_sn,
            "timestamp": timestamp_ms,
            "info": {
                "dev_list": [
                    {
                        "dev_sn": f"ems_{self._device_sn}",
                        "meter_list": [MQTT_EMS_WORK_MODE_METER_ID],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery work mode: {err}") from err


class JackeryOutputPowerLimitSelect(CoordinatorEntity[JackeryHomeCloudCoordinator], SelectEntity):
    """MQTT-backed select for the Jackery max output (discharge) power preset.

    Experimental: the meter mapping was reverse engineered from observed
    traffic rather than official documentation. Verify the selected value
    against the Jackery app before relying on it for automation. Only 2
    presets have been observed so far - there may be more.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "output_power_limit"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(OUTPUT_POWER_LIMIT_OPTIONS.keys())

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the output power limit select."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_output_power_limit"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial value once the select entity is added."""
        await super().async_added_to_hass()
        await self._async_request_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated coordinator data."""
        self._bundle = dict(self._system_bundle or {})
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return availability based on device serial and MQTT connectivity."""
        return self.coordinator.is_control_available(self._system_id, self._device_sn)

    @property
    def current_option(self) -> str | None:
        """Return the last known output power limit preset."""
        bundle = self._system_bundle or self._bundle or {}
        raw_value = bundle.get("output_power_limit_raw")
        if not isinstance(raw_value, str):
            return None
        return _OUTPUT_POWER_LIMIT_VALUE_TO_OPTION.get(raw_value)

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    async def async_select_option(self, option: str) -> None:
        """Publish the desired output power limit preset via MQTT and verify it was applied."""
        value = OUTPUT_POWER_LIMIT_OPTIONS.get(option)
        if value is None:
            raise HomeAssistantError(f"Unknown Jackery output power limit option: {option}")
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for output power limit control.")

        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
            raw_value=value,
            bundle_key="output_power_limit_raw",
            timestamp_key="output_power_limit_raw_at",
            expected_bundle_value=value,
            refresh_group=self.coordinator.async_request_config_live_meter_values,
        )

    async def _async_request_state(self) -> None:
        """Actively request the current output power limit via data_get."""
        if not self._device_sn:
            return
        topic = MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE.format(device_serial=self._device_sn)
        timestamp_ms = str(int(time.time() * 1000))
        payload = {
            "cmd": "data_get",
            "gw_sn": self._device_sn,
            "timestamp": timestamp_ms,
            "info": {
                "dev_list": [
                    {
                        "dev_sn": f"ems_{self._device_sn}",
                        "meter_list": [MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery output power limit: {err}") from err


def _system_device_info(system_id: str, bundle: Mapping[str, Any]) -> DeviceInfo:
    """Build device info for the system-level select entity."""
    system = bundle.get("system", {}) if isinstance(bundle, Mapping) else {}
    system_no = str(system.get("systemNo") or system_id)
    name = str(system.get("name") or system_no)
    return DeviceInfo(
        identifiers={(DOMAIN, f"system_{system_id}")},
        name=name,
        manufacturer=MANUFACTURER,
        model=_friendly_model_name(
            system.get("factoryModel")
            or system.get("series")
            or system.get("model")
        ),
        serial_number=system_no,
        sw_version=_safe_str(bundle.get("main_device_firmware")),
    )


def _extract_system_device_sn(bundle: Mapping[str, Any]) -> str:
    """Return the primary device serial used for Jackery MQTT commands."""
    for key in ("main_device_serial", "system_no", "systemNo", "serial_number"):
        value = bundle.get(key)
        if value:
            return str(value).strip()

    system = bundle.get("system", {})
    if isinstance(system, Mapping):
        for key in ("systemNo", "deviceNo", "sn"):
            value = system.get(key)
            if value:
                return str(value).strip()

    return ""


def _friendly_model_name(raw_model: Any) -> str:
    """Return a combined technical and market-facing model name when known."""
    raw = _safe_str(raw_model)
    if not raw:
        return "Jackery Home System"
    friendly = MODEL_NAME_MAP.get(raw)
    if not friendly:
        return raw
    return f"{raw} ({friendly})"


def _safe_str(value: Any) -> str:
    """Convert arbitrary values to a safe stripped string."""
    if value is None:
        return ""
    return str(value).strip()
