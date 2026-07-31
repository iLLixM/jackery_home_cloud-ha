"""Sensor platform for Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ENABLE_MQTT, DOMAIN, MANUFACTURER, MODEL_NAME_MAP
from .coordinator import JackeryHomeCloudCoordinator

PARALLEL_UPDATES = 0

MQTT_RESTORE_SENSOR_KEYS: set[str] = {
    "battery_energy_charged_total",
    "battery_energy_discharged_total",
    "pv1_energy_total",
    "pv2_energy_total",
    "pv_energy_total",
}


@dataclass(frozen=True, kw_only=True)
class JackeryMetricDescription(SensorEntityDescription):
    """Description for a derived Jackery sensor.

    The value callback receives the full system bundle. Existing sensors keep a
    source-based unique id suffix so Home Assistant can migrate previously
    created entities onto the new single-device-per-system model.
    """

    value_fn: Callable[[dict[str, Any]], Any]
    unique_id_fn: Callable[[str, dict[str, Any]], str]
    entity_category: EntityCategory | None = None
    requires_mqtt: bool = False


SYSTEM_SENSOR_DESCRIPTIONS: tuple[JackeryMetricDescription, ...] = (
    JackeryMetricDescription(
        key="total_charge_amount",
        name="Total charge amount",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _coerce_float(bundle["monitor"].get("totalChargeAmount")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="co2_saved",
        name="CO2 saved",
        native_unit_of_measurement="kg",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _coerce_float(bundle["monitor"].get("co2")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="grid_power",
        name="Grid power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "gridVO", "gridPower")
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "energyFlowCTVO", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="ac_main_power",
        name="AC main power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acMainVO", "acMainPower")
        ),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "soc")
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="battery_energy_remaining",
        name="Battery energy remaining",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "energyRemain")
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="pv_power",
        name="PV power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "pvInfo", "pvPower")
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "pvInfo", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="eps_load_power",
        name="EPS load power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "epsLoadPower")
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="other_load_power",
        name="Other load power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_float(
            _safe_get(
                bundle,
                "monitor",
                "energyFlowChartVO",
                "otherLoadVO",
                "otherLoadPower",
            )
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "otherLoadVO", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="solar_energy_generated_today",
        name="Solar energy generated today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _daily_energy(bundle, "solar_energy_generated_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_energy_charged_today",
        name="Battery charged today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda bundle: _daily_energy(bundle, "battery_energy_charged_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_energy_discharged_today",
        name="Battery discharged today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda bundle: _daily_energy(bundle, "battery_energy_discharged_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_energy_charged_total",
        name="Battery charged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("battery_energy_charged_total")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_energy_discharged_total",
        name="Battery discharged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("battery_energy_discharged_total")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="grid_energy_exported_today",
        name="Grid energy exported today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _daily_energy(bundle, "grid_energy_exported_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="grid_energy_imported_today",
        name="Grid energy imported today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _daily_energy(bundle, "grid_energy_imported_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="pv1_energy_today",
        name="PV1 energy today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _daily_energy(bundle, "pv1_energy_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="pv2_energy_today",
        name="PV2 energy today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda bundle: _daily_energy(bundle, "pv2_energy_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="pv1_energy_total",
        name="PV1 energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("pv1_energy_total")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="pv2_energy_total",
        name="PV2 energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("pv2_energy_total")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="pv_energy_total",
        name="PV energy total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("pv_energy_total")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="on_grid_energy_exported_today",
        name="On-grid energy exported today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda bundle: _daily_energy(bundle, "on_grid_energy_exported_today"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_count",
        name="Battery count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda bundle: bundle.get("battery_count"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="total_battery_capacity",
        name="Total battery capacity",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:battery-high",
        value_fn=lambda bundle: _coerce_float(bundle.get("total_battery_capacity_kwh")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="device_connection",
        name="Device connection",
        entity_category=EntityCategory.DIAGNOSTIC,
        requires_mqtt=True,
        value_fn=lambda bundle: bundle.get("device_connection"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="mqtt_connection_status",
        name="MQTT connection status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda bundle: _safe_get(bundle, "mqtt_state", "connection_state"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="mqtt_message_count",
        name="MQTT message count",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        requires_mqtt=True,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda bundle: _coerce_int(_safe_get(bundle, "mqtt_state", "message_count")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="mqtt_last_topic",
        name="MQTT last topic",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        requires_mqtt=True,
        value_fn=lambda bundle: _safe_get(bundle, "mqtt_state", "last_topic"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="mqtt_last_message_at",
        name="MQTT last message at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        requires_mqtt=True,
        value_fn=lambda bundle: _safe_get(bundle, "mqtt_state", "last_message_at"),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Jackery sensors from a config entry."""
    runtime = entry.runtime_data
    coordinator: JackeryHomeCloudCoordinator = runtime.coordinator
    known_unique_ids: set[str] = set()
    mqtt_enabled = bool(entry.options.get(CONF_ENABLE_MQTT, False))

    @callback
    def async_add_new_entities() -> None:
        """Create sensors for all currently known systems."""
        new_entities: list[SensorEntity] = []
        for entity in _build_entities(coordinator, mqtt_enabled):
            if entity.unique_id in known_unique_ids:
                continue
            known_unique_ids.add(entity.unique_id)
            new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


def _build_entities(
    coordinator: JackeryHomeCloudCoordinator,
    mqtt_enabled: bool,
) -> list[SensorEntity]:
    """Create all sensor entities for the coordinator data snapshot."""
    entities: list[SensorEntity] = []
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}

    for system_id, bundle in systems.items():
        for description in SYSTEM_SENSOR_DESCRIPTIONS:
            if description.requires_mqtt and not mqtt_enabled:
                continue
            entities.append(
                JackeryMetricSensor(
                    coordinator=coordinator,
                    system_id=system_id,
                    description=description,
                )
            )

    return entities


class JackeryBaseSensor(CoordinatorEntity[JackeryHomeCloudCoordinator], SensorEntity):
    """Common coordinator-backed sensor base class."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
    ) -> None:
        """Initialize the shared sensor state."""
        super().__init__(coordinator)
        self._system_id = system_id

    @property
    def unique_id(self) -> str:
        """Return the entity unique id."""
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        """Return whether the backing coordinator data is available."""
        return self._system_bundle is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the system-level device registry information.

        This integration intentionally exposes one Home Assistant device per
        selected Jackery system. All metrics from sub-components such as EMS,
        inverter, battery pack, and meter are grouped under that one logical
        system device for a clearer user experience.
        """
        bundle = self._system_bundle
        if not bundle:
            return DeviceInfo(identifiers={(DOMAIN, f"missing_{self._system_id}")})

        return _system_device_info(self._system_id, bundle)

    @property
    def _system_bundle(self) -> dict[str, Any] | None:
        """Return the current system bundle for this entity."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("systems", {}).get(self._system_id)


class JackeryMetricSensor(JackeryBaseSensor, RestoreEntity):
    """Sensor based on a metric description."""

    entity_description: JackeryMetricDescription

    def __init__(
        self,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
        description: JackeryMetricDescription,
    ) -> None:
        """Initialize the metric sensor."""
        super().__init__(coordinator, system_id)
        self.entity_description = description
        bundle = self._system_bundle or {}
        unique_source = description.unique_id_fn(system_id, bundle)
        self._attr_unique_id = f"{unique_source}_{description.key}"
        self._attr_name = description.name
        self._attr_entity_category = description.entity_category
        self._restored_native_value: Any | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known state for selected MQTT-only sensors."""
        await super().async_added_to_hass()

        if self.entity_description.key not in MQTT_RESTORE_SENSOR_KEYS:
            return

        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in ("unknown", "unavailable", "", None):
            return

        try:
            self._restored_native_value = float(last_state.state)
        except (TypeError, ValueError):
            self._restored_native_value = last_state.state

    @property
    def native_value(self) -> Any:
        """Return the current metric value from the coordinator snapshot."""
        bundle = self._system_bundle
        current_value = None
        if bundle:
            current_value = self.entity_description.value_fn(bundle)
        if current_value is not None:
            return current_value
        if self.entity_description.key in MQTT_RESTORE_SENSOR_KEYS:
            return self._restored_native_value
        return None


def _system_device_info(system_id: str, bundle: Mapping[str, Any]) -> DeviceInfo:
    """Build the device registry payload for a Jackery system."""
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


def _friendly_model_name(raw_model: Any) -> str:
    """Return a combined technical and market-facing model name when known."""
    raw = _safe_str(raw_model)
    if not raw:
        return "Jackery Home System"
    friendly = MODEL_NAME_MAP.get(raw)
    if not friendly:
        return raw
    return f"{raw} ({friendly})"


def _unique_source_or_system(system_id: str, source: Any) -> str:
    """Keep stable unique ids for existing sensors when a source device exists."""
    text = _safe_str(source)
    if text:
        return text
    return f"system_{system_id}"


def _daily_energy(bundle: Mapping[str, Any], key: str) -> float | None:
    """Return a derived daily energy total from the coordinator bundle."""
    daily_energy = bundle.get("daily_energy")
    if not isinstance(daily_energy, Mapping):
        return None
    return _coerce_float(daily_energy.get(key))


def _safe_get(data: Any, *path: str) -> Any:
    """Safely traverse nested dictionaries."""
    current = data
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _safe_str(value: Any) -> str | None:
    """Convert a value to string while keeping None as None."""
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _coerce_float(value: Any) -> float | None:
    """Convert API values to float when possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    """Convert API values to int when possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
