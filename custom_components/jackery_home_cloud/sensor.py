"""Sensor platform for Jackery Home Cloud."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ENABLE_MQTT,
    DOMAIN,
    MANUFACTURER,
    MODEL_NAME_MAP,
    MQTT_EMS_CHARGE_WINDOW_METER_IDS,
    MQTT_EMS_DISCHARGE_WINDOW_METER_IDS,
)
from .coordinator import JackeryHomeCloudCoordinator, _validate_and_pad_schedule_raw

PARALLEL_UPDATES = 0

MQTT_RESTORE_SENSOR_KEYS: set[str] = {
    "ac_output_energy_in",
    "ac_output_energy_out",
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
        icon="mdi:transmission-tower-import",
        # Prefers the MQTT-sourced value (sign flipped, see
        # MQTT_EMS_GRID_POWER_METER_ID in const.py) when fresh, falling back
        # to REST.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "grid_power_mqtt",
            _safe_get(bundle, "monitor", "energyFlowChartVO", "gridVO", "gridPower"),
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
        # Prefers the MQTT-sourced value when fresh, falling back to REST.
        # See MQTT_PCS_AC_MAIN_POWER_METER_ID in const.py for the raw
        # meter's sign convention.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "ac_main_power_mqtt",
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acMainVO", "acMainPower"),
        ),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_soc",
        name="Battery SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        # Prefers the MQTT-sourced value (raw / 10, see
        # MQTT_EMS_BATTERY_SOC_METER_ID in const.py) when fresh, falling
        # back to REST.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "battery_soc_mqtt",
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "soc"),
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "emsGwVO", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="battery_power",
        name="Battery power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        requires_mqtt=True,
        # No REST equivalent exists for this at all. See
        # MQTT_EMS_BATTERY_POWER_METER_ID in const.py for the sign
        # convention.
        value_fn=lambda bundle: _coerce_float(bundle.get("battery_power_mqtt")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="battery_power_bms1",
        name="Battery power BMS1",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        requires_mqtt=True,
        # No REST equivalent exists for this at all. See
        # MQTT_BMS1_BATTERY_POWER_METER_ID in const.py for the sign
        # convention.
        value_fn=lambda bundle: _coerce_float(bundle.get("battery_power_bms1_mqtt")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
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
        icon="mdi:solar-power",
        # Prefers the MQTT-sourced value (PV1 + PV2 meters summed, see
        # MQTT_PCS_PV1_POWER_METER_ID / MQTT_PCS_PV2_POWER_METER_ID in
        # const.py) when fresh, falling back to REST.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "pv_power_mqtt",
            _safe_get(bundle, "monitor", "energyFlowChartVO", "pvInfo", "pvPower"),
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "pvInfo", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="eps_load_power",
        name="AC-socket power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power-plug",
        # Prefers the MQTT-sourced value (see
        # MQTT_EMS_EPS_LOAD_POWER_METER_ID in const.py) when fresh, falling
        # back to REST.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "eps_load_power_mqtt",
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "epsLoadPower"),
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="eps_load_power_inverted",
        translation_key="eps_load_power_inverted",
        name="AC-socket power inverted",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power-plug",
        entity_registry_enabled_default=False,
        # Uses the exact same fresh-MQTT-with-REST-fallback source as
        # eps_load_power, but reverses its sign for installations where an
        # external AC-coupled solar inverter feeds power into the socket.
        value_fn=lambda bundle: _invert_power(
            _mqtt_or_rest(
                bundle,
                "eps_load_power_mqtt",
                _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "epsLoadPower"),
            )
        ),
        unique_id_fn=lambda system_id, bundle: _unique_source_or_system(
            system_id,
            _safe_get(bundle, "monitor", "energyFlowChartVO", "acInfo", "deviceNo"),
        ),
    ),
    JackeryMetricDescription(
        key="other_load_power",
        name="Home-supply power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-import-outline",
        # Prefers the MQTT-sourced value (see
        # MQTT_EMS_OTHER_LOAD_POWER_METER_ID in const.py) when fresh,
        # falling back to REST. This is the true household load meter - do
        # not confuse with ac_main_power, which only coincides with this one
        # when grid_power is ~0.
        value_fn=lambda bundle: _mqtt_or_rest(
            bundle,
            "other_load_power_mqtt",
            _safe_get(
                bundle,
                "monitor",
                "energyFlowChartVO",
                "otherLoadVO",
                "otherLoadPower",
            ),
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
        key="ac_output_energy_in",
        translation_key="ac_output_energy_in",
        name="AC-Output energy in",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("ac_output_energy_in")),
        unique_id_fn=lambda system_id, _: f"system_{system_id}",
    ),
    JackeryMetricDescription(
        key="ac_output_energy_out",
        translation_key="ac_output_energy_out",
        name="AC-Output energy out",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        requires_mqtt=True,
        value_fn=lambda bundle: _coerce_float(bundle.get("ac_output_energy_out")),
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
        requires_mqtt=True,
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

    if mqtt_enabled:
        platform = entity_platform.async_get_current_platform()
        window_schema = {
            vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
            vol.Required("start"): cv.time,
            vol.Required("end"): cv.time,
        }
        clear_schema = {
            vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(min=0, max=9)),
        }
        platform.async_register_entity_service(
            "set_charge_window", window_schema, "async_set_charge_window"
        )
        platform.async_register_entity_service(
            "set_discharge_window", window_schema, "async_set_discharge_window"
        )
        platform.async_register_entity_service(
            "clear_charge_window", clear_schema, "async_clear_charge_window"
        )
        platform.async_register_entity_service(
            "clear_discharge_window", clear_schema, "async_clear_discharge_window"
        )


def _build_entities(
    coordinator: JackeryHomeCloudCoordinator,
    mqtt_enabled: bool,
) -> list[SensorEntity]:
    """Create all sensor entities for the coordinator data snapshot."""
    entities: list[SensorEntity] = []
    systems = coordinator.data.get("systems", {}) if coordinator.data else {}

    for system_id, bundle in systems.items():
        # requires_mqtt sensors have no REST equivalent (e.g. instantaneous
        # battery power, cumulative MQTT-only energy totals), so they only
        # make sense for the single system the MQTT client is actually
        # subscribed to (coordinator.is_mqtt_system). Merged REST/MQTT
        # sensors (grid power, battery SOC, ...) are created for every
        # system regardless, since they fall back to REST automatically for
        # non-primary systems.
        system_has_mqtt = mqtt_enabled and coordinator.is_mqtt_system(system_id)
        # requires_mqtt sensors have no per-meter capability gating in the
        # MVP of discussion #6 item 9 ("Device capability and model
        # detection") - unlike number/select/switch/button, none of these
        # descriptions carry an individual meter id today. Instead, on an
        # unconfirmed model they're created disabled-by-default so a user
        # must explicitly opt in, same soft signal as the other platforms.
        system_model_confirmed = system_has_mqtt and coordinator.is_model_confirmed(system_id)
        for description in SYSTEM_SENSOR_DESCRIPTIONS:
            if description.requires_mqtt and not system_has_mqtt:
                continue
            entity = JackeryMetricSensor(
                coordinator=coordinator,
                system_id=system_id,
                description=description,
            )
            if description.requires_mqtt and not system_model_confirmed:
                entity._attr_entity_registry_enabled_default = False
            entities.append(entity)
        if system_has_mqtt:
            schedule_entity = JackeryScheduleSensor(coordinator=coordinator, system_id=system_id)
            if not system_model_confirmed:
                schedule_entity._attr_entity_registry_enabled_default = False
            entities.append(schedule_entity)

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

        # Every key in MQTT_RESTORE_SENSOR_KEYS represents a numeric energy
        # counter. Do not expose arbitrary recorder data as a string-valued
        # energy sensor if a historical state cannot be converted.
        restored_value = _coerce_float(last_state.state)
        if restored_value is not None:
            self._restored_native_value = restored_value

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


class JackeryScheduleSensor(JackeryBaseSensor):
    """Sensor exposing the Jackery scheduled charge/discharge time (Time of use) windows.

    Experimental: reverse engineered from observed traffic rather than
    official documentation (see MQTT_EMS_CHARGE_WINDOW_METER_IDS /
    MQTT_EMS_DISCHARGE_WINDOW_METER_IDS in const.py). The schedule only
    takes effect while the work mode select is set to "Time of use".
    The protocol supports up to 10
    charge and 10 discharge windows; rather than pre-declaring up to 40 time
    entities, individual windows are managed through this entity's
    set_charge_window / set_discharge_window / clear_charge_window /
    clear_discharge_window services (slot 0-9), with the full current
    schedule exposed as attributes.
    """

    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        *,
        coordinator: JackeryHomeCloudCoordinator,
        system_id: str,
    ) -> None:
        """Initialize the schedule sensor."""
        super().__init__(coordinator, system_id)
        self._attr_unique_id = f"system_{system_id}_charge_discharge_schedule"
        self._attr_name = "Charge/discharge schedule"

    @property
    def native_value(self) -> str | None:
        """Return a short summary of how many windows are currently configured."""
        bundle = self._system_bundle
        if bundle is None:
            return None
        charge_windows = _schedule_windows(bundle, "charge_window_")
        discharge_windows = _schedule_windows(bundle, "discharge_window_")
        return f"{len(charge_windows)} charge, {len(discharge_windows)} discharge"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full current schedule."""
        bundle = self._system_bundle
        if bundle is None:
            return {}
        return {
            "charge_windows": _schedule_windows(bundle, "charge_window_"),
            "discharge_windows": _schedule_windows(bundle, "discharge_window_"),
        }

    async def async_set_charge_window(self, slot: int, start: dt_time, end: dt_time) -> None:
        """Set one of the scheduled charge windows (service handler)."""
        await self._async_set_window("charge", MQTT_EMS_CHARGE_WINDOW_METER_IDS, slot, start, end)

    async def async_set_discharge_window(self, slot: int, start: dt_time, end: dt_time) -> None:
        """Set one of the scheduled discharge windows (service handler)."""
        await self._async_set_window("discharge", MQTT_EMS_DISCHARGE_WINDOW_METER_IDS, slot, start, end)

    async def async_clear_charge_window(self, slot: int) -> None:
        """Clear one of the scheduled charge windows (service handler)."""
        await self._async_clear_window("charge", MQTT_EMS_CHARGE_WINDOW_METER_IDS, slot)

    async def async_clear_discharge_window(self, slot: int) -> None:
        """Clear one of the scheduled discharge windows (service handler)."""
        await self._async_clear_window("discharge", MQTT_EMS_DISCHARGE_WINDOW_METER_IDS, slot)

    async def _async_set_window(
        self,
        kind: str,
        meter_ids: tuple[str, ...],
        slot: int,
        start: dt_time,
        end: dt_time,
    ) -> None:
        if not 0 <= slot <= 9:
            raise HomeAssistantError(f"Jackery schedule slot must be 0-9, got {slot}.")
        raw_value = f"{start:%H%M}{end:%H%M}"
        # Reuses coordinator.py's ingestion-side validator so the write path
        # and the MQTT read-back path can never accept a window one way and
        # reject it the other (see CONTRIBUTING.md #2) - a window rejected
        # here would otherwise fail write verification with a confusing
        # timeout instead of this clear, immediate error.
        if _validate_and_pad_schedule_raw(raw_value) is None:
            raise HomeAssistantError(
                f"Jackery schedule window start ({start:%H:%M}) must be strictly before "
                f"end ({end:%H:%M}); overnight-spanning windows are not supported."
            )
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=meter_ids[slot],
            raw_value=raw_value,
            bundle_key=f"{kind}_window_{slot}",
            timestamp_key=f"{kind}_window_{slot}_at",
            expected_bundle_value=raw_value,
            refresh_group=self.coordinator.async_request_schedule_live_meter_values,
        )

    async def _async_clear_window(self, kind: str, meter_ids: tuple[str, ...], slot: int) -> None:
        if not 0 <= slot <= 9:
            raise HomeAssistantError(f"Jackery schedule slot must be 0-9, got {slot}.")
        await self.coordinator.async_set_meter_value(
            system_id=self._system_id,
            meter_id=meter_ids[slot],
            raw_value="0",
            bundle_key=f"{kind}_window_{slot}",
            timestamp_key=f"{kind}_window_{slot}_at",
            expected_bundle_value="0",
            refresh_group=self.coordinator.async_request_schedule_live_meter_values,
        )


def _schedule_windows(bundle: Mapping[str, Any], key_prefix: str) -> list[dict[str, Any]]:
    """Return {"slot", "start", "end"} entries for populated schedule slots.

    No length/content re-validation here: coordinator.py's
    _validate_and_pad_schedule_raw() is the single source of truth for
    schedule-window validity, applied at MQTT ingestion time, so any value
    reaching this bundle key is already a confirmed "0" sentinel or an
    8-digit HHMMHHMM string.
    """
    windows: list[dict[str, Any]] = []
    for index in range(10):
        raw = bundle.get(f"{key_prefix}{index}")
        if isinstance(raw, str) and raw != "0":
            windows.append(
                {
                    "slot": index,
                    "start": f"{raw[0:2]}:{raw[2:4]}",
                    "end": f"{raw[4:6]}:{raw[6:8]}",
                }
            )
    return windows


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


def _mqtt_or_rest(bundle: Mapping[str, Any], mqtt_key: str, rest_value: Any) -> float | None:
    """Prefer a fresh MQTT-sourced bundle value, falling back to the REST value.

    The MQTT bundle key is only present when coordinator.py's
    _apply_mqtt_live_values_to_bundle merged a recent-enough live value (see
    MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS), so this naturally falls back to
    REST whenever MQTT is disabled, disconnected, or the value went stale.
    """
    mqtt_value = _coerce_float(bundle.get(mqtt_key))
    if mqtt_value is not None:
        return mqtt_value
    return _coerce_float(rest_value)


def _invert_power(value: Any) -> float | None:
    """Invert a power value while normalizing negative zero."""
    numeric_value = _coerce_float(value)
    if numeric_value is None:
        return None
    if numeric_value == 0:
        return 0.0
    return -numeric_value


def _coerce_int(value: Any) -> int | None:
    """Convert API values to int when possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
