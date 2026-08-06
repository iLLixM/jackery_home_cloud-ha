"""Number platform for Jackery Home Cloud MQTT-backed controls."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfPower
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
    MQTT_EMS_BATTERY_SOC_SCALE,
    MQTT_EMS_DISCHARGE_LIMIT_SOC_METER_ID,
    MQTT_EMS_FEED_POWER_LIMIT_MAX_W,
    MQTT_EMS_FEED_POWER_LIMIT_METER_ID,
    MQTT_EMS_CHARGE_LIMIT_SOC_METER_ID,
)
from .coordinator import JackeryHomeCloudCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jackery MQTT-backed number controls for a config entry."""
    if not entry.options.get(CONF_ENABLE_MQTT):
        return

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or getattr(runtime, "mqtt_client", None) is None:
        return

    coordinator = runtime.coordinator
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}
    entities: list[_JackeryMqttNumberEntity] = []

    for system_id, bundle in systems.items():
        if not coordinator.is_mqtt_system(system_id):
            continue
        if not isinstance(bundle, Mapping):
            continue
        device_sn = _extract_system_device_sn(bundle)
        if not device_sn:
            continue
        model_confirmed = coordinator.is_model_confirmed(system_id)
        for entity_cls in (
            JackeryDischargeLimitSocNumber,
            JackeryChargeLimitSocNumber,
            JackeryFeedPowerLimitNumber,
        ):
            if not coordinator.supports_meter(system_id, entity_cls._meter_id):
                continue
            entity = entity_cls(
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


class _JackeryMqttNumberEntity(CoordinatorEntity[JackeryHomeCloudCoordinator], NumberEntity):
    """Shared behavior for Jackery MQTT-backed number controls.

    Experimental: the meter mapping was reverse engineered from observed
    traffic rather than official documentation. Verify the value against the
    Jackery app before relying on it for automation.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    _meter_id: str = ""
    _bundle_key: str = ""
    _unique_id_suffix: str = ""

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        bundle: Mapping[str, Any],
        mqtt_client: Any,
        device_sn: str,
    ) -> None:
        """Initialize the number control."""
        super().__init__(coordinator)
        self._system_id = system_id
        self._bundle = dict(bundle)
        self._mqtt_client = mqtt_client
        self._device_sn = str(device_sn).strip()
        self._attr_unique_id = f"system_{system_id}_{self._unique_id_suffix}"
        self._attr_device_info = _system_device_info(system_id, bundle)

    async def async_added_to_hass(self) -> None:
        """Request the initial value once the number entity is added."""
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
    def _system_bundle(self) -> dict[str, Any] | None:
        systems = self.coordinator.data.get("systems", {}) if self.coordinator.data else {}
        bundle = systems.get(self._system_id)
        return bundle if isinstance(bundle, dict) else None

    def _raw_to_native(self, raw: float) -> float:
        """Convert a raw meter value to the entity's native value."""
        return raw

    def _native_to_raw(self, value: float) -> str:
        """Convert the entity's native value to a raw meter value string."""
        return str(int(round(value)))

    def _normalize_native_value(self, value: float) -> float:
        """Normalize a requested value before it is written (no-op by default)."""
        return value

    @property
    def native_value(self) -> float | None:
        """Return the last known value."""
        bundle = self._system_bundle or self._bundle or {}
        raw = bundle.get(self._bundle_key)
        if raw is None:
            return None
        try:
            return self._raw_to_native(float(raw))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Publish the desired value via MQTT and verify it was applied."""
        if not self._device_sn:
            raise HomeAssistantError("No Jackery device serial is available for this control.")
        value = self._normalize_native_value(value)
        raw_value = self._native_to_raw(value)
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=self._meter_id,
            raw_value=raw_value,
            bundle_key=self._bundle_key,
            timestamp_key=f"{self._bundle_key}_at",
            expected_bundle_value=value,
            refresh_group=self.coordinator.async_request_config_live_meter_values,
        )

    async def _async_request_state(self) -> None:
        """Actively request the current value via data_get."""
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
                        "meter_list": [self._meter_id],
                    }
                ]
            },
        }
        try:
            await self._mqtt_client.async_publish_json(topic, payload, qos=1)
        except Exception as err:
            if "not connected" in str(err).lower():
                return
            raise HomeAssistantError(f"Failed to request Jackery {self.name}: {err}") from err


class JackeryDischargeLimitSocNumber(_JackeryMqttNumberEntity):
    """Minimum battery SOC below which discharging should stop. Discharge setting range according to android app is from 5% to 49%"""

    _attr_name = "Discharge limit"
    _attr_icon = "mdi:battery-charging-low"
    _attr_native_min_value = 5
    _attr_native_max_value = 49
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _meter_id = MQTT_EMS_DISCHARGE_LIMIT_SOC_METER_ID
    _bundle_key = "discharge_limit_soc_mqtt"
    _unique_id_suffix = "discharge_limit_soc"

    def _raw_to_native(self, raw: float) -> int:
        """Return the SOC limit as a whole percentage."""
        return int(round(raw))

    def _native_to_raw(self, value: float) -> str:
        return str(int(round(value * MQTT_EMS_BATTERY_SOC_SCALE)))


class JackeryChargeLimitSocNumber(_JackeryMqttNumberEntity):
    """Maximum battery SOC above which charging should stop. Android app allows mimimum value of 50%"""

    _attr_name = "Charge limit"
    _attr_icon = "mdi:battery-charging-high"
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _meter_id = MQTT_EMS_CHARGE_LIMIT_SOC_METER_ID
    _bundle_key = "charge_limit_soc_mqtt"
    _unique_id_suffix = "charge_limit_soc"

    def _raw_to_native(self, raw: float) -> int:
        """Return the SOC limit as a whole percentage."""
        return int(round(raw))

    def _native_to_raw(self, value: float) -> str:
        return str(int(round(value * MQTT_EMS_BATTERY_SOC_SCALE)))


class JackeryFeedPowerLimitNumber(_JackeryMqttNumberEntity):
    """Maximum feed-in power."""

    _attr_name = "Feed-in power limit"
    _attr_icon = "mdi:current-ac"
    _attr_native_min_value = 0
    _attr_native_max_value = MQTT_EMS_FEED_POWER_LIMIT_MAX_W
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _meter_id = MQTT_EMS_FEED_POWER_LIMIT_METER_ID
    _bundle_key = "feed_power_limit_mqtt"
    _unique_id_suffix = "feed_power_limit"

    def _raw_to_native(self, raw: float) -> int:
        """Return the feed power limit as a whole watt value."""
        return int(round(raw))

    def _normalize_native_value(self, value: float) -> int:
        """Normalize the requested value to a whole 10 W step."""
        return int(round(value / 10.0) * 10)


def _system_device_info(system_id: str, bundle: Mapping[str, Any]) -> DeviceInfo:
    """Build device info for the system-level number entities."""
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
