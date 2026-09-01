"""Button platform for Jackery Home Cloud MQTT commands."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENABLE_MQTT,
    DOMAIN,
    MANUFACTURER,
    MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE,
    MQTT_EMS_REBOOT_METER_ID,
    MODEL_NAME_MAP,
)
from .coordinator import JackeryHomeCloudCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jackery reboot buttons for a config entry."""
    if not entry.options.get(CONF_ENABLE_MQTT):
        return

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or getattr(runtime, "mqtt_client", None) is None:
        return

    coordinator = runtime.coordinator
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}
    entities: list[JackeryRebootButton] = []

    for system_id, bundle in systems.items():
        if not coordinator.is_mqtt_system(system_id):
            continue
        if not isinstance(bundle, Mapping):
            continue
        device_sn = _extract_system_device_sn(bundle)
        if not device_sn:
            continue
        if not coordinator.supports_meter(system_id, MQTT_EMS_REBOOT_METER_ID):
            continue
        entity = JackeryRebootButton(
            coordinator=coordinator,
            system_id=str(system_id),
            bundle=bundle,
            mqtt_client=runtime.mqtt_client,
            device_sn=device_sn,
        )
        if not coordinator.is_model_confirmed(system_id):
            entity._attr_entity_registry_enabled_default = False
        entities.append(entity)

    if entities:
        async_add_entities(entities)


class JackeryRebootButton(CoordinatorEntity[JackeryHomeCloudCoordinator], ButtonEntity):
    """Button entity that reboots a Jackery device via MQTT."""

    _attr_has_entity_name = True
    _attr_translation_key = "reboot_device"
    _attr_icon = "mdi:restart"
    _attr_entity_category = None

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the reboot button."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_reboot_device"
        self._attr_device_info = _system_device_info(system_id, bundle)

    @property
    def available(self) -> bool:
        """Return availability based on device serial and MQTT connectivity."""
        return self.coordinator.is_control_available(self._system_id, self._device_sn)

    async def async_press(self) -> None:
        """Publish the Jackery reboot command via MQTT."""
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for reboot.")

        topic = MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE.format(device_serial=self._device_sn)
        timestamp_ms = str(int(time.time() * 1000))
        payload = {
            "cmd": "data_set",
            "gw_sn": self._device_sn,
            "timestamp": timestamp_ms,
            "info": {
                "dev_list": [
                    {
                        "dev_sn": f"ems_{self._device_sn}",
                        "meter_list": [
                            [MQTT_EMS_REBOOT_METER_ID, "1"],
                        ],
                    }
                ]
            },
        }

        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            raise HomeAssistantError(f"Failed to send Jackery reboot command: {err}") from err


def _system_device_info(system_id: str, bundle: Mapping[str, Any]) -> DeviceInfo:
    """Build device info for the system-level button."""
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
