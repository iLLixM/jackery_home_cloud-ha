"""Switch platform for Jackery Home Cloud MQTT-backed controls."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    MQTT_EMS_AC_OUTPUT_METER_ID,
    MQTT_EMS_AUTO_STANDBY_METER_ID,
    MQTT_EMS_AUTO_STANDBY_RAW_OFF,
    MQTT_EMS_AUTO_STANDBY_RAW_ON,
    MQTT_EMS_STANDBY_METER_ID,
    MQTT_EMS_STANDBY_RAW_OFF,
    MQTT_EMS_STANDBY_RAW_ON,
)
from .coordinator import JackeryHomeCloudCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jackery MQTT-backed switches for a config entry."""
    if not entry.options.get(CONF_ENABLE_MQTT):
        return

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or getattr(runtime, "mqtt_client", None) is None:
        return

    coordinator = runtime.coordinator
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}
    entities: list[JackeryAcOutputSwitch | JackeryStandbySwitch | JackeryAutoStandbySwitch] = []

    for system_id, bundle in systems.items():
        if not isinstance(bundle, Mapping):
            continue
        device_sn = _extract_system_device_sn(bundle)
        if not device_sn:
            continue
        entities.append(
            JackeryAcOutputSwitch(
                coordinator=coordinator,
                system_id=str(system_id),
                bundle=bundle,
                mqtt_client=runtime.mqtt_client,
                device_sn=device_sn,
            )
        )
        entities.append(
            JackeryStandbySwitch(
                coordinator=coordinator,
                system_id=str(system_id),
                bundle=bundle,
                mqtt_client=runtime.mqtt_client,
                device_sn=device_sn,
            )
        )
        entities.append(
            JackeryAutoStandbySwitch(
                coordinator=coordinator,
                system_id=str(system_id),
                bundle=bundle,
                mqtt_client=runtime.mqtt_client,
                device_sn=device_sn,
            )
        )

    if entities:
        async_add_entities(entities)


class JackeryAcOutputSwitch(CoordinatorEntity[JackeryHomeCloudCoordinator], SwitchEntity):
    """MQTT-backed switch for the Jackery AC output state."""

    _attr_has_entity_name = True
    _attr_name = "AC Output"
    _attr_icon = "mdi:power-plug"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the AC output switch."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_ac_output"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial AC output state once the switch is added."""
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
        return bool(self._device_sn) and super().available

    @property
    def is_on(self) -> bool | None:
        """Return the last known AC output state."""
        bundle = self._system_bundle or self._bundle or {}
        value = bundle.get("ac_output_state")
        if isinstance(value, bool):
            return value
        return None

    @property
    def icon(self) -> str:
        """Return a state-aware icon for the AC output switch."""
        if self.is_on is True:
            return "mdi:power-plug"
        if self.is_on is False:
            return "mdi:power-plug-off-outline"
        return "mdi:power-plug"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes for the last known AC output state."""
        bundle = self._system_bundle or self._bundle or {}
        mqtt_live = bundle.get("mqtt_live") if isinstance(bundle, dict) else None
        ac_meta = mqtt_live.get("ac_output_state") if isinstance(mqtt_live, dict) else None
        attrs: dict[str, Any] = {}
        if isinstance(ac_meta, dict):
            if "source" in ac_meta:
                attrs["state_source"] = ac_meta.get("source")
            if "age_seconds" in ac_meta:
                attrs["state_age_seconds"] = ac_meta.get("age_seconds")
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on AC output via MQTT."""
        await self._async_send_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off AC output via MQTT."""
        await self._async_send_set(False)

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    async def _async_request_state(self) -> None:
        """Actively request the current AC output state via data_get."""
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
                        "meter_list": [MQTT_EMS_AC_OUTPUT_METER_ID],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery AC output state: {err}") from err

    async def _async_send_set(self, is_on: bool) -> None:
        """Publish the desired AC output state via MQTT and verify it was applied."""
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for AC output control.")
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=MQTT_EMS_AC_OUTPUT_METER_ID,
            raw_value="1" if is_on else "0",
            bundle_key="ac_output_state",
            expected_bundle_value=is_on,
        )


class JackeryStandbySwitch(CoordinatorEntity[JackeryHomeCloudCoordinator], SwitchEntity):
    """MQTT-backed switch for the Jackery standby state.

    Experimental: the meter mapping was reverse engineered from observed
    traffic rather than official documentation. Verify against the Jackery
    app before relying on it for automation. See JackeryAutoStandbySwitch
    below for the separate "auto standby" setting.
    """

    _attr_has_entity_name = True
    _attr_name = "Standby"
    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the standby switch."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_standby"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial standby state once the switch is added."""
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
        return bool(self._device_sn) and super().available

    @property
    def is_on(self) -> bool | None:
        """Return True if the device is currently in standby."""
        bundle = self._system_bundle or self._bundle or {}
        raw_value = bundle.get("standby_raw")
        if not isinstance(raw_value, str):
            return None
        if raw_value == MQTT_EMS_STANDBY_RAW_ON:
            return True
        if raw_value == MQTT_EMS_STANDBY_RAW_OFF:
            return False
        return None

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enter standby via MQTT."""
        await self._async_send_set(MQTT_EMS_STANDBY_RAW_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Exit standby via MQTT."""
        await self._async_send_set(MQTT_EMS_STANDBY_RAW_OFF)

    async def _async_request_state(self) -> None:
        """Actively request the current standby state via data_get."""
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
                        "meter_list": [MQTT_EMS_STANDBY_METER_ID],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery standby state: {err}") from err

    async def _async_send_set(self, raw_value: str) -> None:
        """Publish the desired standby state via MQTT and verify it was applied."""
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for standby control.")
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=MQTT_EMS_STANDBY_METER_ID,
            raw_value=raw_value,
            bundle_key="standby_raw",
            expected_bundle_value=raw_value,
            refresh_group=self.coordinator.async_request_config_live_meter_values,
        )


class JackeryAutoStandbySwitch(CoordinatorEntity[JackeryHomeCloudCoordinator], SwitchEntity):
    """MQTT-backed switch for the Jackery "auto standby" setting.

    Distinct from JackeryStandbySwitch above (which enters/exits standby
    immediately) - this toggles whether the device automatically enters
    standby on its own.

    Experimental: the meter mapping was reverse engineered from observed
    traffic rather than official documentation. Verify against the Jackery
    app before relying on it for automation. See MQTT_EMS_AUTO_STANDBY_METER_ID
    in const.py for the raw value convention.
    """

    _attr_has_entity_name = True
    _attr_name = "Auto standby"
    _attr_icon = "mdi:sleep-off"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the auto standby switch."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_auto_standby"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial auto standby state once the switch is added."""
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
        return bool(self._device_sn) and super().available

    @property
    def is_on(self) -> bool | None:
        """Return True if auto standby is currently enabled."""
        bundle = self._system_bundle or self._bundle or {}
        raw_value = bundle.get("auto_standby_raw")
        if not isinstance(raw_value, str):
            return None
        if raw_value == MQTT_EMS_AUTO_STANDBY_RAW_ON:
            return True
        if raw_value == MQTT_EMS_AUTO_STANDBY_RAW_OFF:
            return False
        return None

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable auto standby via MQTT."""
        await self._async_send_set(MQTT_EMS_AUTO_STANDBY_RAW_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable auto standby via MQTT."""
        await self._async_send_set(MQTT_EMS_AUTO_STANDBY_RAW_OFF)

    async def _async_request_state(self) -> None:
        """Actively request the current auto standby state via data_get."""
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
                        "meter_list": [MQTT_EMS_AUTO_STANDBY_METER_ID],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery auto standby state: {err}") from err

    async def _async_send_set(self, raw_value: str) -> None:
        """Publish the desired auto standby state via MQTT and verify it was applied."""
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for auto standby control.")
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=MQTT_EMS_AUTO_STANDBY_METER_ID,
            raw_value=raw_value,
            bundle_key="auto_standby_raw",
            expected_bundle_value=raw_value,
            refresh_group=self.coordinator.async_request_config_live_meter_values,
        )


def _system_device_info(system_id: str, bundle: Mapping[str, Any]) -> DeviceInfo:
    """Build device info for the system-level switch."""
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
