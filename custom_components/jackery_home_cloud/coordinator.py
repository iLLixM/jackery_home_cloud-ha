"""Data coordinator for Jackery Home Cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.client import JackeryApiClient
from .const import (
    CONF_ACCOUNT,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
    DAILY_TREND_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID,
    MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID,
    MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID,
    MQTT_EMS_PV2_ENERGY_TOTAL_METER_ID,
    MQTT_EMS_PV_TOTAL_CHARGE_METER_ID,
    MQTT_EMS_AC_OUTPUT_METER_ID,
    MQTT_LIVE_VALUE_MAX_AGE_SECONDS,
    TREND_DATE_FORMAT,
    UPDATE_INTERVAL_SECONDS,
)
from .exceptions import JackeryHomeApiError, JackeryHomeAuthError
from .mqtt_parser import extract_ems_meter_value

_LOGGER = logging.getLogger(__name__)

# The battery trend values are cumulative daily values. The cloud backend seems
# to publish new buckets only about once per hour, therefore the same-day jump
# protection must be loose enough to accept realistic hourly increases.
_BATTERY_ABSOLUTE_CAPACITY_MULTIPLIER = 3.0
_BATTERY_ABSOLUTE_MIN_LIMIT_KWH = 4.0
_BATTERY_ALLOWED_INCREMENT_FRACTION = 0.75
_BATTERY_ALLOWED_INCREMENT_MIN_KWH = 0.20
_BATTERY_ALLOWED_DECREASE_TOLERANCE_KWH = 0.05

# When multiple raw sources are available, candidates are compared against each
# other. The tolerance is intentionally small but not exact because the cloud
# backend mixes rounded values and slightly different aggregation paths.
_SOURCE_MATCH_MIN_TOLERANCE_KWH = 0.05
_SOURCE_MATCH_RELATIVE_TOLERANCE = 0.10
_MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH = 0.01


@dataclass(slots=True)
class DailyTrendCache:
    """Cache entry for daily trend payloads of a single system."""

    day_key: str
    fetched_at: Any
    trend_cluster_daily: dict[str, Any]
    battery_bms_daily: dict[str, Any]


@dataclass(slots=True)
class BatteryResolution:
    """Resolved battery daily value plus debug metadata."""

    accepted_value: float | None
    selected_source: str
    candidate_value: float | None
    decision: str
    raw_bms_total: float | None
    bms_total_direct_kwh: float | None
    bms_total_div1000_kwh: float | None
    bms_list_sum_kwh: float | None
    cluster_reference_kwh: float | None
    all_zero_sources: bool


class JackeryHomeCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate REST refreshes and MQTT runtime state for Jackery systems."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: JackeryApiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            always_update=False,
        )
        self.client = client
        self._login_lock = asyncio.Lock()
        self._app_user: dict[str, Any] = {}
        self._mqtt_credentials: dict[str, Any] = {}
        self._daily_trend_cache: dict[str, DailyTrendCache] = {}
        self._mqtt_state: dict[str, Any] = {
            "connected": False,
            "connection_state": "inactive",
            "error": None,
            "last_topic": None,
            "last_message_at": None,
            "last_message_preview": None,
            "message_count": 0,
            "last_gw_sn": None,
            "last_dev_sn": None,
            "last_method": None,
        }
        self._mqtt_live_values: dict[str, dict[str, Any]] = {}

    @property
    def mqtt_credentials(self) -> dict[str, Any]:
        """Expose the latest encrypted MQTT REST payload for setup/runtime use."""
        return dict(self._mqtt_credentials)

    async def _async_setup(self) -> None:
        """Perform one-time bootstrap work before the first refresh."""
        await self._async_login()
        self._app_user = await self._async_api_call(self.client.async_get_app_user)
        self._mqtt_credentials = await self._async_api_call(self.client.async_get_mqtt_credentials)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch fresh data for all selected systems."""
        systems = await self._async_api_call(self.client.async_list_systems)
        systems_by_id = {
            str(item["id"]): item
            for item in systems
            if item.get("id") is not None
        }

        selected_system_ids = self._resolve_selected_system_ids(systems_by_id)
        if not selected_system_ids:
            raise UpdateFailed(
                "No selected Jackery systems are currently available for this account."
            )

        previous_systems = self.data.get("systems", {}) if self.data else {}
        system_results = await asyncio.gather(
            *(
                self._async_fetch_system_bundle(
                    system_id,
                    systems_by_id.get(system_id, {}),
                    previous_systems.get(system_id, {}),
                )
                for system_id in selected_system_ids
            )
        )

        bundles: dict[str, dict[str, Any]] = {}
        for system_id, bundle in system_results:
            bundle["mqtt_state"] = dict(self._mqtt_state)
            bundles[system_id] = self._apply_mqtt_live_values_to_bundle(system_id, bundle)

        return {
            "account": self._account,
            "app_user": self._app_user,
            "mqtt_credentials": self._mqtt_credentials,
            "mqtt_state": dict(self._mqtt_state),
            "available_systems": {
                system_id: dict(system)
                for system_id, system in systems_by_id.items()
            },
            "selected_system_ids": list(selected_system_ids),
            "systems": bundles,
        }

    @property
    def _account(self) -> str:
        """Return the configured account name."""
        return str(self.config_entry.data[CONF_ACCOUNT])

    @property
    def _phone_uid(self) -> str:
        """Return the configured stable phone UID."""
        return str(self.config_entry.data[CONF_PHONE_UID])

    async def _async_login(self) -> None:
        """Log in with the credentials stored in the config entry.

        The password is read directly from the config entry data to avoid
        caching it on the coordinator object longer than necessary.
        """
        async with self._login_lock:
            await self.client.async_login(
                account=self._account,
                password=str(self.config_entry.data[CONF_PASSWORD]),
                phone_uid=self._phone_uid,
            )

    async def _async_fetch_system_bundle(
        self,
        system_id: str,
        system: dict[str, Any],
        previous_bundle: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Fetch all currently mapped data for a single system.

        Live monitor values are refreshed on every coordinator cycle. Daily
        trend data is cached for 15 minutes because the cloud backend appears to
        aggregate those values only about once per hour.
        """
        monitor = await self._async_api_call(self.client.async_get_monitor, system_id)

        main_device_detail: dict[str, Any] = {}
        main_device_no = str(system.get("systemNo") or system.get("deviceNo") or "").strip()
        if main_device_no:
            main_device_detail = await self._async_optional_api_call(
                self.client.async_get_device_detail,
                main_device_no,
                default={},
            )

        day_key = _system_local_day_key(monitor)
        trend_cluster_daily, battery_bms_daily = await self._async_get_daily_trends(
            system_id,
            day_key,
        )

        merged_system = dict(system)
        merged_system.update(monitor.get("systemVO") or {})

        previous_daily_energy = previous_bundle.get("daily_energy") if isinstance(previous_bundle, Mapping) else None
        if not isinstance(previous_daily_energy, Mapping):
            previous_daily_energy = {}

        previous_day_key = None
        if isinstance(previous_bundle, Mapping):
            previous_day_key = previous_bundle.get("trend_day_key")
        same_day = previous_day_key == day_key

        daily_energy = _build_daily_energy_summary(
            system_id=system_id,
            day_key=day_key,
            monitor=monitor,
            trend_cluster_daily=trend_cluster_daily,
            battery_bms_daily=battery_bms_daily,
            previous_daily_energy=previous_daily_energy,
            same_day=same_day,
        )

        battery_count = self._compute_battery_count(merged_system, monitor)
        return system_id, {
            "system": merged_system,
            "monitor": monitor,
            "devices": {},
            "main_device_firmware": self._extract_main_device_firmware(
                merged_system,
                monitor,
                main_device_detail,
            ),
            "battery_count": battery_count,
            "total_battery_capacity_kwh": self._compute_total_battery_capacity_kwh(
                merged_system,
                battery_count,
            ),
            "trend_cluster_daily": trend_cluster_daily,
            "battery_bms_daily": battery_bms_daily,
            "daily_energy": daily_energy,
            "trend_day_key": day_key,
        }


    def _extract_main_device_firmware(
        self,
        merged_system: Mapping[str, Any],
        monitor: Mapping[str, Any],
        main_device_detail: Mapping[str, Any],
    ) -> str | None:
        """Extract the main device firmware from the best available REST sources.

        Preferred source:
        - /v1/home/device/detail?deviceNo=<systemNo> -> result.baseVO.softVer

        Fallbacks:
        - monitor.energyFlowChartVO.emsGwVO.softVer
        - merged_system version-like fields
        """
        if isinstance(main_device_detail, Mapping):
            result = main_device_detail.get("result", main_device_detail)
            if isinstance(result, Mapping):
                base_vo = result.get("baseVO", {})
                if isinstance(base_vo, Mapping):
                    value = base_vo.get("softVer")
                    if value not in (None, ""):
                        return str(value)

        if isinstance(monitor, Mapping):
            energy = monitor.get("energyFlowChartVO", {})
            if isinstance(energy, Mapping):
                ems = energy.get("emsGwVO", {})
                if isinstance(ems, Mapping):
                    for key in (
                        "softVer",
                        "firmwareVersion",
                        "firmware",
                        "swVersion",
                        "version",
                    ):
                        value = ems.get(key)
                        if value not in (None, ""):
                            return str(value)

        if isinstance(merged_system, Mapping):
            for key in (
                "softVer",
                "firmwareVersion",
                "firmware",
                "swVersion",
                "version",
                "masterVersion",
                "appVersion",
            ):
                value = merged_system.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    def _compute_battery_count(
        self,
        merged_system: Mapping[str, Any],
        monitor: Mapping[str, Any],
    ) -> int:
        """Return the total battery count including the main unit battery.

        Preferred source:
        - monitor.energyFlowChartVO.emsGwVO.powerPackList
          This list contains the main unit battery (bms1_...) and any
          additional battery packs (bms2_..., bms3_..., ...).

        Fallbacks:
        - 1 + emsGwVO.powerPacks
        - 1 + merged_system power-pack count fields
        """
        if isinstance(monitor, Mapping):
            energy = monitor.get("energyFlowChartVO", {})
            if isinstance(energy, Mapping):
                ems = energy.get("emsGwVO", {})
                if isinstance(ems, Mapping):
                    power_pack_list = ems.get("powerPackList")
                    if isinstance(power_pack_list, list) and power_pack_list:
                        return max(len(power_pack_list), 1)

                    power_packs = ems.get("powerPacks")
                    try:
                        if power_packs not in (None, ""):
                            return 1 + max(int(power_packs), 0)
                    except (TypeError, ValueError):
                        pass

        if isinstance(merged_system, Mapping):
            for key in ("powerPackNum", "powerPackCount", "batteryPackCount", "batteryNum"):
                value = merged_system.get(key)
                try:
                    if value not in (None, ""):
                        return 1 + max(int(value), 0)
                except (TypeError, ValueError):
                    continue

        return 1

    def _compute_total_battery_capacity_kwh(
        self,
        merged_system: Mapping[str, Any],
        battery_count: int,
    ) -> float | None:
        """Return the installed total battery capacity with strict field priority.

        Based on observed monitor payloads, systemVO.batteryCapacity already
        contains the summed total capacity of the complete storage system.
        Therefore we prefer it explicitly before considering fallback fields.
        """
        if not isinstance(merged_system, Mapping):
            return None

        primary = merged_system.get("batteryCapacity")
        try:
            if primary not in (None, ""):
                return round(float(primary), 3)
        except (TypeError, ValueError):
            pass

        for key in ("capacity", "mainBatteryCapacity", "energyStorageCapacity"):
            value = merged_system.get(key)
            try:
                if value not in (None, ""):
                    return round(float(value), 3)
            except (TypeError, ValueError):
                continue
        return None

    async def _async_get_daily_trends(
        self,
        system_id: str,
        day_key: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return cached or freshly loaded daily trend payloads for a system."""
        now = dt_util.utcnow()
        cache_entry = self._daily_trend_cache.get(system_id)
        refresh_due = True
        if cache_entry and cache_entry.day_key == day_key:
            age_seconds = (now - cache_entry.fetched_at).total_seconds()
            refresh_due = age_seconds >= DAILY_TREND_UPDATE_INTERVAL_SECONDS
            if not refresh_due:
                return cache_entry.trend_cluster_daily, cache_entry.battery_bms_daily

        trend_cluster_daily, battery_bms_daily = await asyncio.gather(
            self._async_optional_api_call(
                self.client.async_get_cluster_trend_daily,
                system_id,
                day_key,
                default=(cache_entry.trend_cluster_daily if cache_entry else {}),
            ),
            self._async_optional_api_call(
                self.client.async_get_battery_bms_trend_daily,
                system_id,
                day_key,
                default=(cache_entry.battery_bms_daily if cache_entry else {}),
            ),
        )

        self._daily_trend_cache[system_id] = DailyTrendCache(
            day_key=day_key,
            fetched_at=now,
            trend_cluster_daily=trend_cluster_daily,
            battery_bms_daily=battery_bms_daily,
        )
        return trend_cluster_daily, battery_bms_daily

    async def async_handle_mqtt_status(self, status: Mapping[str, Any]) -> None:
        was_connected = bool(self._mqtt_state.get("connected", False))
        now_connected = bool(status.get("connected", False))
        self._mqtt_state.update(
            {
                "connected": now_connected,
                "connection_state": status.get("connection_state") or self._mqtt_state.get("connection_state"),
                "error": status.get("error"),
            }
        )
        if now_connected and not was_connected:
            await self.async_request_ac_output_states()
        self._publish_runtime_update()


    async def async_request_ac_output_states(self) -> None:
        """Request AC output states for all selected systems via MQTT."""
        mqtt_client = getattr(getattr(self.config_entry, "runtime_data", None), "mqtt_client", None)
        if mqtt_client is None:
            return

        systems = self.data.get("systems", {}) if isinstance(self.data, Mapping) else {}
        for system_id, bundle in systems.items():
            if not isinstance(bundle, Mapping):
                continue

            device_sn = self._extract_system_device_sn(bundle)
            if not device_sn:
                continue

            topic = self._mqtt_command_topic(device_sn)
            payload = {
                "cmd": "data_get",
                "gw_sn": device_sn,
                "timestamp": _mqtt_timestamp_ms(),
                "info": {
                    "dev_list": [
                        {
                            "dev_sn": f"ems_{device_sn}",
                            "meter_list": [MQTT_EMS_AC_OUTPUT_METER_ID],
                        }
                    ]
                },
            }
            try:
                await mqtt_client.async_publish_json(topic, payload, qos=1)
                _LOGGER.debug(
                    "Requested Jackery AC output state for %s via MQTT topic %s",
                    system_id,
                    topic,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Failed to request Jackery AC output state for %s: %s",
                    system_id,
                    err,
                )

    async def async_handle_mqtt_message(self, message: Mapping[str, Any]) -> None:
        payload_text = message.get("payload_text")
        preview = str(payload_text)[:300] if payload_text is not None else None
        self._mqtt_state.update(
            {
                "connected": True,
                "connection_state": "connected",
                "error": None,
                "last_topic": message.get("topic"),
                "last_message_at": dt_util.utcnow(),
                "last_message_preview": preview,
                "message_count": int(self._mqtt_state.get("message_count", 0)) + 1,
                "last_gw_sn": message.get("gw_sn"),
                "last_dev_sn": message.get("dev_sn"),
                "last_method": message.get("method"),
            }
        )

        try:
            self._ingest_mqtt_live_values(message)
        except Exception as err:
            _LOGGER.exception("Failed to ingest Jackery MQTT live values: %s", err)

        try:
            self._ingest_mqtt_control_values(message)
        except Exception as err:
            _LOGGER.exception("Failed to ingest Jackery MQTT control values: %s", err)

        try:
            self._ingest_mqtt_connection_values(message)
        except Exception as err:
            _LOGGER.exception("Failed to ingest Jackery MQTT connection values: %s", err)

        self._publish_runtime_update()


    def _ingest_mqtt_control_values(self, message: Mapping[str, Any]) -> None:
        """Extract selected control values from MQTT data_get/data_set responses."""
        payload = message.get("payload_json")
        if not isinstance(payload, Mapping):
            return

        cmd = str(payload.get("cmd") or "").strip()
        if cmd not in ("data_get", "data_set"):
            return

        gw_sn = str(payload.get("gw_sn") or "").strip()
        if not gw_sn:
            return

        system_id = self._resolve_system_id_from_gw_sn(gw_sn)
        if not system_id:
            return

        ac_output_value = self._extract_ac_output_state(payload, gw_sn)
        if ac_output_value is None:
            _LOGGER.debug(
                "Ignoring MQTT AC output state payload for %s because meter %s was not found in cmd=%s payload",
                system_id,
                MQTT_EMS_AC_OUTPUT_METER_ID,
                cmd,
            )
            return

        is_on = ac_output_value == "1"
        existing = self._mqtt_live_values.get(system_id, {})
        updated = dict(existing)
        updated.update(
            {
                "ac_output_state": is_on,
                "ac_output_state_at": dt_util.utcnow(),
                "ac_output_state_source": f"mqtt_{cmd}",
            }
        )
        self._mqtt_live_values[system_id] = updated
        _LOGGER.debug(
            "Accepted MQTT AC output state for %s via %s: raw=%s is_on=%s",
            system_id,
            cmd,
            ac_output_value,
            is_on,
        )


    def _ingest_mqtt_connection_values(self, message: Mapping[str, Any]) -> None:
        """Extract device connection state from MQTT LWT topic payloads."""
        payload = message.get("payload_json")
        if not isinstance(payload, Mapping):
            return

        gw_sn = str(payload.get("gw_sn") or "").strip()
        if not gw_sn:
            return

        info = str(payload.get("info") or "").strip().lower()
        if info not in ("online", "offline"):
            return

        system_id = self._resolve_system_id_from_gw_sn(gw_sn)
        if not system_id:
            return

        existing = self._mqtt_live_values.get(system_id, {})
        updated = dict(existing)
        updated.update(
            {
                "device_connection": info,
                "device_connection_at": dt_util.utcnow(),
                "device_connection_source": "mqtt_lwt",
            }
        )
        self._mqtt_live_values[system_id] = updated
        _LOGGER.debug(
            "Accepted MQTT device connection state for %s: %s",
            system_id,
            info,
        )

    def _publish_runtime_update(self) -> None:
        if not self.data:
            return
        new_data = dict(self.data)
        new_data["mqtt_state"] = dict(self._mqtt_state)
        systems = {}
        for system_id, bundle in self.data.get("systems", {}).items():
            merged_bundle = {**bundle, "mqtt_state": dict(self._mqtt_state)}
            systems[system_id] = self._apply_mqtt_live_values_to_bundle(system_id, merged_bundle)
        new_data["systems"] = systems
        self.async_set_updated_data(new_data)

    def _ingest_mqtt_live_values(self, message: Mapping[str, Any]) -> None:
        """Extract selected live values from MQTT data_report payloads."""
        payload = message.get("payload_json")
        if not isinstance(payload, Mapping):
            return
        if str(payload.get("cmd") or "") != "data_report":
            return

        gw_sn = str(payload.get("gw_sn") or "").strip()
        if not gw_sn:
            return

        system_id = self._resolve_system_id_from_gw_sn(gw_sn)
        if not system_id:
            _LOGGER.debug(
                "Ignoring Jackery MQTT data_report for unknown gw_sn=%s",
                gw_sn,
            )
            return

        battery_charged_total = extract_ems_meter_value(
            payload,
            device_serial=gw_sn,
            meter_id=MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID,
        )
        battery_discharged_total = extract_ems_meter_value(
            payload,
            device_serial=gw_sn,
            meter_id=MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID,
        )
        pv1_energy_total = extract_ems_meter_value(
            payload,
            device_serial=gw_sn,
            meter_id=MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID,
        )
        pv2_energy_total = extract_ems_meter_value(
            payload,
            device_serial=gw_sn,
            meter_id=MQTT_EMS_PV2_ENERGY_TOTAL_METER_ID,
        )
        total_pv_charge = extract_ems_meter_value(
            payload,
            device_serial=gw_sn,
            meter_id=MQTT_EMS_PV_TOTAL_CHARGE_METER_ID,
        )
        if (
            battery_charged_total is None
            and battery_discharged_total is None
            and pv1_energy_total is None
            and pv2_energy_total is None
            and total_pv_charge is None
        ):
            return

        day_key = self._resolve_system_day_key(system_id)
        existing = self._mqtt_live_values.get(system_id, {})
        updated = dict(existing)

        if battery_charged_total is not None:
            previous_value = _coerce_float(existing.get("battery_energy_charged_total"))
            if previous_value is not None and battery_charged_total + _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH < previous_value:
                _LOGGER.debug(
                    "Ignoring decreasing MQTT battery charged total for %s: incoming=%s previous=%s",
                    system_id,
                    battery_charged_total,
                    previous_value,
                )
            else:
                updated.update(
                    {
                        "battery_energy_charged_total": battery_charged_total,
                        "battery_energy_charged_total_at": dt_util.utcnow(),
                        "battery_energy_charged_total_source": "mqtt",
                    }
                )
                _LOGGER.debug(
                    "Accepted MQTT battery charged total for %s from meter %s: %s kWh",
                    system_id,
                    MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID,
                    battery_charged_total,
                )

        if battery_discharged_total is not None:
            previous_value = _coerce_float(existing.get("battery_energy_discharged_total"))
            if previous_value is not None and battery_discharged_total + _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH < previous_value:
                _LOGGER.debug(
                    "Ignoring decreasing MQTT battery discharged total for %s: incoming=%s previous=%s",
                    system_id,
                    battery_discharged_total,
                    previous_value,
                )
            else:
                updated.update(
                    {
                        "battery_energy_discharged_total": battery_discharged_total,
                        "battery_energy_discharged_total_at": dt_util.utcnow(),
                        "battery_energy_discharged_total_source": "mqtt",
                    }
                )
                _LOGGER.debug(
                    "Accepted MQTT battery discharged total for %s from meter %s: %s kWh",
                    system_id,
                    MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID,
                    battery_discharged_total,
                )

        if pv1_energy_total is not None:
            previous_value = _coerce_float(existing.get("pv1_energy_total"))
            if previous_value is not None and pv1_energy_total + _BATTERY_ALLOWED_DECREASE_TOLERANCE_KWH < previous_value:
                _LOGGER.debug(
                    "Ignoring decreasing MQTT PV1 energy total value for %s: incoming=%s previous=%s",
                    system_id,
                    pv1_energy_total,
                    previous_value,
                )
            else:
                updated.update(
                    {
                        "pv1_energy_total": pv1_energy_total,
                        "pv1_energy_total_at": dt_util.utcnow(),
                        "pv1_energy_total_source": "mqtt",
                    }
                )
                _LOGGER.debug(
                    "Accepted MQTT PV1 energy total for %s from meter %s: %s kWh",
                    system_id,
                    MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID,
                    pv1_energy_total,
                )

        if pv2_energy_total is not None:
            previous_value = _coerce_float(existing.get("pv2_energy_total"))
            if previous_value is not None and pv2_energy_total + _BATTERY_ALLOWED_DECREASE_TOLERANCE_KWH < previous_value:
                _LOGGER.debug(
                    "Ignoring decreasing MQTT PV2 energy total value for %s: incoming=%s previous=%s",
                    system_id,
                    pv2_energy_total,
                    previous_value,
                )
            else:
                updated.update(
                    {
                        "pv2_energy_total": pv2_energy_total,
                        "pv2_energy_total_at": dt_util.utcnow(),
                        "pv2_energy_total_source": "mqtt",
                    }
                )
                _LOGGER.debug(
                    "Accepted MQTT PV2 energy total for %s from meter %s: %s kWh",
                    system_id,
                    MQTT_EMS_PV2_ENERGY_TOTAL_METER_ID,
                    pv2_energy_total,
                )

        if total_pv_charge is not None:
            previous_value = _coerce_float(existing.get("total_pv_charge"))
            if previous_value is not None and total_pv_charge + _BATTERY_ALLOWED_DECREASE_TOLERANCE_KWH < previous_value:
                _LOGGER.debug(
                    "Ignoring decreasing MQTT total PV charge value for %s: incoming=%s previous=%s",
                    system_id,
                    total_pv_charge,
                    previous_value,
                )
            else:
                updated.update(
                    {
                        "total_pv_charge": total_pv_charge,
                        "total_pv_charge_at": dt_util.utcnow(),
                        "total_pv_charge_source": "mqtt",
                    }
                )
                _LOGGER.debug(
                    "Accepted MQTT total PV charge for %s from meter %s: %s kWh",
                    system_id,
                    MQTT_EMS_PV_TOTAL_CHARGE_METER_ID,
                    total_pv_charge,
                )

        self._mqtt_live_values[system_id] = updated

    def _extract_ac_output_state(self, payload: Mapping[str, Any], gw_sn: str) -> str | None:
        """Return the raw AC output state from an MQTT control payload."""
        info = payload.get("info")
        if not isinstance(info, Mapping):
            return None

        dev_list = info.get("dev_list")
        if not isinstance(dev_list, list):
            return None

        expected_dev_sn = f"ems_{gw_sn}"
        for dev in dev_list:
            if not isinstance(dev, Mapping):
                continue
            dev_sn = str(dev.get("dev_sn") or "").strip()
            if dev_sn != expected_dev_sn:
                continue

            meter_list = dev.get("meter_list")
            if not isinstance(meter_list, list):
                continue

            for item in meter_list:
                if not isinstance(item, list) or len(item) < 2:
                    continue
                meter_id = str(item[0]).strip()
                meter_value = str(item[1]).strip()
                if meter_id == MQTT_EMS_AC_OUTPUT_METER_ID and meter_value in ("0", "1"):
                    return meter_value

        return None

    def _resolve_system_id_from_gw_sn(self, gw_sn: str) -> str | None:
        """Map a MQTT gw_sn to a selected system id."""
        normalized = str(gw_sn).strip()
        if not normalized:
            return None

        systems = self.data.get("systems", {}) if isinstance(self.data, Mapping) else {}
        for system_id, bundle in systems.items():
            if not isinstance(bundle, Mapping):
                continue

            candidates = [
                bundle.get("system", {}).get("systemNo") if isinstance(bundle.get("system"), Mapping) else None,
                bundle.get("system", {}).get("deviceNo") if isinstance(bundle.get("system"), Mapping) else None,
                bundle.get("system", {}).get("sn") if isinstance(bundle.get("system"), Mapping) else None,
            ]
            for candidate in candidates:
                if str(candidate or "").strip() == normalized:
                    return str(system_id)

        available_systems = self.data.get("available_systems", {}) if isinstance(self.data, Mapping) else {}
        for system_id, system in available_systems.items():
            if not isinstance(system, Mapping):
                continue
            for key in ("systemNo", "deviceNo", "sn"):
                if str(system.get(key) or "").strip() == normalized:
                    return str(system_id)

        return None


    def _extract_system_device_sn(self, bundle: Mapping[str, Any]) -> str:
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

    def _mqtt_command_topic(self, device_sn: str) -> str:
        """Return the MQTT command topic for a device serial."""
        from .const import MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE
        return MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE.format(device_serial=device_sn)

    def _resolve_system_day_key(self, system_id: str) -> str:
        """Return the current local day key for a selected system."""
        systems = self.data.get("systems", {}) if isinstance(self.data, Mapping) else {}
        bundle = systems.get(system_id, {}) if isinstance(systems, Mapping) else {}
        monitor = bundle.get("monitor") if isinstance(bundle, Mapping) else None
        if isinstance(monitor, Mapping):
            return _system_local_day_key(monitor)
        return dt_util.utcnow().strftime(TREND_DATE_FORMAT)

    def _apply_mqtt_live_values_to_bundle(
        self,
        system_id: str,
        bundle: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge fresh MQTT live values into a system bundle."""
        merged = dict(bundle)
        live = self._mqtt_live_values.get(system_id)
        if not isinstance(live, Mapping):
            return merged

        now = dt_util.utcnow()
        bundle_day_key = merged.get("trend_day_key")
        daily_energy = dict(merged.get("daily_energy") or {})
        mqtt_live = dict(merged.get("mqtt_live") or {})

        charged_timestamp = live.get("battery_energy_charged_today_at")
        charged_day_key = live.get("battery_energy_charged_today_day_key")
        charged_value = _coerce_float(live.get("battery_energy_charged_today"))
        if charged_value is not None and charged_timestamp is not None:
            charged_age_seconds = (now - charged_timestamp).total_seconds()
            if charged_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS and bundle_day_key in (None, charged_day_key):
                daily_energy["battery_energy_charged_today"] = charged_value
                mqtt_live["battery_energy_charged_today"] = {
                    "value": charged_value,
                    "source": "mqtt",
                    "day_key": charged_day_key,
                    "age_seconds": charged_age_seconds,
                }

        discharged_timestamp = live.get("battery_energy_discharged_today_at")
        discharged_day_key = live.get("battery_energy_discharged_today_day_key")
        discharged_value = _coerce_float(live.get("battery_energy_discharged_today"))
        if discharged_value is not None and discharged_timestamp is not None:
            discharged_age_seconds = (now - discharged_timestamp).total_seconds()
            if discharged_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS and bundle_day_key in (None, discharged_day_key):
                daily_energy["battery_energy_discharged_today"] = discharged_value
                mqtt_live["battery_energy_discharged_today"] = {
                    "value": discharged_value,
                    "source": "mqtt",
                    "day_key": discharged_day_key,
                    "age_seconds": discharged_age_seconds,
                }

        charged_total_timestamp = live.get("battery_energy_charged_total_at")
        charged_total_value = _coerce_float(live.get("battery_energy_charged_total"))
        if charged_total_value is not None and charged_total_timestamp is not None:
            charged_total_age_seconds = (now - charged_total_timestamp).total_seconds()
            if charged_total_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS:
                merged["battery_energy_charged_total"] = charged_total_value
                mqtt_live["battery_energy_charged_total"] = {
                    "value": charged_total_value,
                    "source": "mqtt",
                    "age_seconds": charged_total_age_seconds,
                }

        discharged_total_timestamp = live.get("battery_energy_discharged_total_at")
        discharged_total_value = _coerce_float(live.get("battery_energy_discharged_total"))
        if discharged_total_value is not None and discharged_total_timestamp is not None:
            discharged_total_age_seconds = (now - discharged_total_timestamp).total_seconds()
            if discharged_total_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS:
                merged["battery_energy_discharged_total"] = discharged_total_value
                mqtt_live["battery_energy_discharged_total"] = {
                    "value": discharged_total_value,
                    "source": "mqtt",
                    "age_seconds": discharged_total_age_seconds,
                }

        pv1_total_timestamp = live.get("pv1_energy_total_at")
        pv1_total_value = _coerce_float(live.get("pv1_energy_total"))
        if pv1_total_value is not None and pv1_total_timestamp is not None:
            pv1_total_age_seconds = (now - pv1_total_timestamp).total_seconds()
            if pv1_total_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS:
                merged["pv1_energy_total"] = pv1_total_value
                mqtt_live["pv1_energy_total"] = {
                    "value": pv1_total_value,
                    "source": "mqtt",
                    "age_seconds": pv1_total_age_seconds,
                }

        pv2_total_timestamp = live.get("pv2_energy_total_at")
        pv2_total_value = _coerce_float(live.get("pv2_energy_total"))
        if pv2_total_value is not None and pv2_total_timestamp is not None:
            pv2_total_age_seconds = (now - pv2_total_timestamp).total_seconds()
            if pv2_total_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS:
                merged["pv2_energy_total"] = pv2_total_value
                mqtt_live["pv2_energy_total"] = {
                    "value": pv2_total_value,
                    "source": "mqtt",
                    "age_seconds": pv2_total_age_seconds,
                }

        total_pv_charge_timestamp = live.get("total_pv_charge_at")
        total_pv_charge_value = _coerce_float(live.get("total_pv_charge"))
        if total_pv_charge_value is not None and total_pv_charge_timestamp is not None:
            total_pv_charge_age_seconds = (now - total_pv_charge_timestamp).total_seconds()
            if total_pv_charge_age_seconds <= MQTT_LIVE_VALUE_MAX_AGE_SECONDS:
                merged["total_pv_charge"] = total_pv_charge_value
                mqtt_live["total_pv_charge"] = {
                    "value": total_pv_charge_value,
                    "source": "mqtt",
                    "age_seconds": total_pv_charge_age_seconds,
                }

        if daily_energy:
            merged["daily_energy"] = daily_energy
        ac_output_timestamp = live.get("ac_output_state_at")
        ac_output_value = live.get("ac_output_state")
        if isinstance(ac_output_value, bool):
            merged["ac_output_state"] = ac_output_value
            ac_output_age_seconds = None
            if ac_output_timestamp is not None:
                ac_output_age_seconds = (now - ac_output_timestamp).total_seconds()
            mqtt_live["ac_output_state"] = {
                "value": ac_output_value,
                "source": live.get("ac_output_state_source", "mqtt"),
                "age_seconds": ac_output_age_seconds,
            }

        device_connection_timestamp = live.get("device_connection_at")
        device_connection_value = live.get("device_connection")
        if isinstance(device_connection_value, str):
            merged["device_connection"] = device_connection_value
            device_connection_age_seconds = None
            if device_connection_timestamp is not None:
                device_connection_age_seconds = (now - device_connection_timestamp).total_seconds()
            mqtt_live["device_connection"] = {
                "value": device_connection_value,
                "source": live.get("device_connection_source", "mqtt_lwt"),
                "age_seconds": device_connection_age_seconds,
            }

        if mqtt_live:
            merged["mqtt_live"] = mqtt_live
        return merged

    async def _async_api_call(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
    ) -> Any:
        """Run an API call and retry once after re-authentication."""
        try:
            return await func(*args)
        except JackeryHomeAuthError as err:
            _LOGGER.debug("Authentication failed, retrying login once: %s", err)
            try:
                await self._async_login()
                return await func(*args)
            except JackeryHomeAuthError as retry_err:
                raise ConfigEntryAuthFailed(
                    "Authentication with Jackery Home Cloud failed."
                ) from retry_err
        except JackeryHomeApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_optional_api_call(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        default: Any,
    ) -> Any:
        """Run a non-critical API call and degrade gracefully on API errors."""
        try:
            return await self._async_api_call(func, *args)
        except UpdateFailed as err:
            _LOGGER.warning("Optional Jackery API call failed: %s", err)
            return default

    def _resolve_selected_system_ids(
        self, systems_by_id: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Resolve the selected system ids for the current account."""
        configured = self.config_entry.options.get(CONF_SELECTED_SYSTEMS)
        if configured is None:
            configured = self.config_entry.data.get(CONF_SELECTED_SYSTEMS)

        if configured:
            return [
                str(system_id)
                for system_id in configured
                if str(system_id) in systems_by_id
            ]

        return list(systems_by_id)

def _system_local_day_key(monitor: Mapping[str, Any]) -> str:
    """Return the current day key for the system timezone."""
    time_zone_name = _safe_get(monitor, "systemVO", "timeZone")
    time_zone = dt_util.DEFAULT_TIME_ZONE
    if isinstance(time_zone_name, str) and time_zone_name:
        try:
            time_zone = ZoneInfo(time_zone_name)
        except ZoneInfoNotFoundError:
            _LOGGER.debug(
                "Unknown Jackery system timezone %s, falling back to Home Assistant timezone",
                time_zone_name,
            )

    return dt_util.utcnow().astimezone(time_zone).strftime(TREND_DATE_FORMAT)


def _build_daily_energy_summary(
    *,
    system_id: str,
    day_key: str,
    monitor: Mapping[str, Any],
    trend_cluster_daily: Mapping[str, Any],
    battery_bms_daily: Mapping[str, Any],
    previous_daily_energy: Mapping[str, Any],
    same_day: bool,
) -> dict[str, float | None]:
    """Derive user facing daily energy totals from raw trend payloads.

    The caller provides whether the previous coordinator snapshot belongs to the
    same local system day. This prevents a legitimate midnight reset to 0 from
    being treated as an invalid same-day decrease.
    """
    trend_system = _first_cluster_system(trend_cluster_daily)
    trend_list = _trend_list(trend_system)

    system_battery_capacity = _system_battery_capacity_kwh(monitor)

    charged_resolution = _resolve_battery_daily_value(
        metric_key="battery_energy_charged_today",
        system_id=system_id,
        day_key=day_key,
        battery_capacity_kwh=system_battery_capacity,
        raw_bms_total=battery_bms_daily.get("totalCharge"),
        bms_list_sum_kwh=_sum_bms_values(battery_bms_daily.get("bmsList"), "charge"),
        cluster_reference_kwh=_sum_negative_as_positive(trend_list, "batteryCharge"),
        previous_value=_coerce_float(previous_daily_energy.get("battery_energy_charged_today")) if same_day else None,
        same_day=same_day,
    )

    discharged_resolution = _resolve_battery_daily_value(
        metric_key="battery_energy_discharged_today",
        system_id=system_id,
        day_key=day_key,
        battery_capacity_kwh=system_battery_capacity,
        raw_bms_total=battery_bms_daily.get("totalDisCharge"),
        bms_list_sum_kwh=_sum_bms_values(battery_bms_daily.get("bmsList"), "disCharge"),
        cluster_reference_kwh=_sum_positive(trend_list, "batteryDischarge"),
        previous_value=_coerce_float(previous_daily_energy.get("battery_energy_discharged_today")) if same_day else None,
        same_day=same_day,
    )

    return {
        "solar_energy_generated_today": _prefer_value(
            _coerce_float(trend_system.get("pvChargeAmountTotal")),
            _coerce_float(trend_cluster_daily.get("pvChargeAmountTotal")),
            _sum_positive(trend_list, "pvChargeAmount"),
        ),
        "battery_energy_charged_today": charged_resolution.accepted_value,
        "battery_energy_discharged_today": discharged_resolution.accepted_value,
        "grid_energy_exported_today": _sum_positive(trend_list, "gridOut"),
        "grid_energy_imported_today": _sum_positive(trend_list, "gridInput"),
        "pv1_energy_today": _sum_positive(trend_list, "pv1TotalGen"),
        "pv2_energy_today": _sum_positive(trend_list, "pv2TotalGen"),
        "on_grid_energy_exported_today": _sum_positive(trend_list, "onGridOut"),
    }


def _resolve_battery_daily_value(
    *,
    metric_key: str,
    system_id: str,
    day_key: str,
    battery_capacity_kwh: float | None,
    raw_bms_total: Any,
    bms_list_sum_kwh: float | None,
    cluster_reference_kwh: float | None,
    previous_value: float | None,
    same_day: bool,
) -> BatteryResolution:
    """Resolve the best daily battery energy value from multiple sources.

    The cloud API exposes the same daily concept through several paths that do
    not always use the same unit. The resolver therefore normalizes the BMS
    total into both plausible variants and then chooses the candidate that is
    most consistent with the per-BMS list and the cluster trend reference.
    """
    raw_total_numeric = _coerce_float(raw_bms_total)
    total_direct_kwh = round(raw_total_numeric, 3) if raw_total_numeric is not None else None
    total_div1000_kwh = (
        round(raw_total_numeric / 1000, 3) if raw_total_numeric is not None else None
    )

    candidate_values: dict[str, float] = {}
    if total_direct_kwh is not None:
        candidate_values["bms_total_direct"] = total_direct_kwh
    if total_div1000_kwh is not None:
        candidate_values["bms_total_div1000"] = total_div1000_kwh
    if bms_list_sum_kwh is not None:
        candidate_values["bms_list_sum"] = bms_list_sum_kwh
    if cluster_reference_kwh is not None:
        candidate_values["cluster_reference"] = cluster_reference_kwh

    # A midnight reset is trustworthy when all three raw API perspectives
    # independently report a zero value. In that case the resolver must allow
    # a reset to 0 even if the previous accepted value belonged to the old day
    # or the previous refresh still carried the previous day's total.
    all_zero_sources = (
        raw_total_numeric == 0.0
        and bms_list_sum_kwh == 0.0
        and cluster_reference_kwh == 0.0
    )

    selected_source = "none"
    candidate_value: float | None = None
    decision = "no_candidate_keep_previous"

    if candidate_values:
        scored_candidates = []
        for source_name, value in candidate_values.items():
            close_matches = 0
            total_distance = 0.0
            for other_name, other_value in candidate_values.items():
                if other_name == source_name:
                    continue
                total_distance += abs(value - other_value)
                if _values_are_consistent(value, other_value):
                    close_matches += 1
            scored_candidates.append(
                (
                    close_matches,
                    _source_priority(source_name),
                    total_distance,
                    source_name,
                    value,
                )
            )

        # More matches are better. Lower source priority and lower aggregate
        # distance are better for tie-breaking.
        scored_candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        _, _, _, selected_source, candidate_value = scored_candidates[0]
        decision = "selected_consistent_candidate"

    accepted_value = candidate_value
    if candidate_value is not None:
        accepted_value, plausibility_decision = _apply_daily_value_plausibility(
            metric_key=metric_key,
            candidate_value=candidate_value,
            previous_value=previous_value,
            battery_capacity_kwh=battery_capacity_kwh,
            same_day=same_day,
            all_zero_sources=all_zero_sources,
        )
        decision = plausibility_decision
    else:
        accepted_value = previous_value

    _LOGGER.debug(
        (
            "Battery daily energy resolution for %s on %s (%s): "
            "raw_bms_total=%s bms_total_direct_kwh=%s bms_total_div1000_kwh=%s "
            "bms_list_sum_kwh=%s cluster_reference_kwh=%s all_zero_sources=%s "
            "selected_source=%s candidate=%s previous_value=%s same_day=%s "
            "decision=%s accepted_value=%s"
        ),
        system_id,
        day_key,
        metric_key,
        raw_total_numeric,
        total_direct_kwh,
        total_div1000_kwh,
        bms_list_sum_kwh,
        cluster_reference_kwh,
        all_zero_sources,
        selected_source,
        candidate_value,
        previous_value,
        same_day,
        decision,
        accepted_value,
    )

    return BatteryResolution(
        accepted_value=accepted_value,
        selected_source=selected_source,
        candidate_value=candidate_value,
        decision=decision,
        raw_bms_total=raw_total_numeric,
        bms_total_direct_kwh=total_direct_kwh,
        bms_total_div1000_kwh=total_div1000_kwh,
        bms_list_sum_kwh=bms_list_sum_kwh,
        cluster_reference_kwh=cluster_reference_kwh,
        all_zero_sources=all_zero_sources,
    )


def _apply_daily_value_plausibility(
    *,
    metric_key: str,
    candidate_value: float,
    previous_value: float | None,
    battery_capacity_kwh: float | None,
    same_day: bool,
    all_zero_sources: bool,
) -> tuple[float | None, str]:
    """Apply protective plausibility checks to a resolved daily battery value."""
    absolute_limit = max(
        (battery_capacity_kwh or 0.0) * _BATTERY_ABSOLUTE_CAPACITY_MULTIPLIER,
        _BATTERY_ABSOLUTE_MIN_LIMIT_KWH,
    )
    if candidate_value > absolute_limit:
        _LOGGER.warning(
            "Ignoring implausible %s candidate %.3f kWh because it exceeds the absolute limit %.3f kWh",
            metric_key,
            candidate_value,
            absolute_limit,
        )
        return previous_value, "rejected_absolute_limit_keep_previous"

    if all_zero_sources and candidate_value == 0.0:
        return 0.0, "accepted_all_zero_reset"

    if previous_value is None:
        return candidate_value, "accepted"

    if same_day:
        allowed_increment = max(
            (battery_capacity_kwh or 0.0) * _BATTERY_ALLOWED_INCREMENT_FRACTION,
            _BATTERY_ALLOWED_INCREMENT_MIN_KWH,
        )
        delta = candidate_value - previous_value
        if delta > allowed_increment:
            _LOGGER.warning(
                "Ignoring implausible %s jump from %.3f to %.3f kWh because it exceeds the allowed increment %.3f kWh",
                metric_key,
                previous_value,
                candidate_value,
                allowed_increment,
            )
            return previous_value, "rejected_jump_keep_previous"

        if delta < -_BATTERY_ALLOWED_DECREASE_TOLERANCE_KWH:
            _LOGGER.warning(
                "Ignoring decreasing %s candidate %.3f kWh because same-day totals should not fall below %.3f kWh",
                metric_key,
                candidate_value,
                previous_value,
            )
            return previous_value, "rejected_decrease_keep_previous"

    return candidate_value, "accepted"


def _system_battery_capacity_kwh(monitor: Mapping[str, Any]) -> float | None:
    """Return the nominal system battery capacity in kWh when available."""
    return _coerce_float(_safe_get(monitor, "systemVO", "batteryCapacity"))


def _values_are_consistent(left: float | None, right: float | None) -> bool:
    """Return True when two numeric candidates are close enough to agree."""
    if left is None or right is None:
        return False
    tolerance = max(
        _SOURCE_MATCH_MIN_TOLERANCE_KWH,
        min(abs(left), abs(right)) * _SOURCE_MATCH_RELATIVE_TOLERANCE,
    )
    return abs(left - right) <= tolerance


def _source_priority(source_name: str) -> int:
    """Return the tie-break priority for candidate source selection.

    Lower values are preferred. The priorities reflect the desired production
    order once consistency has been established:

    1. a normalized BMS total value that matches the other sources
    2. the summed per-BMS list
    3. the cluster trend reference as the final fallback
    4. the direct unscaled BMS total only when nothing else is better
    """
    order = {
        "bms_total_div1000": 0,
        "bms_list_sum": 1,
        "cluster_reference": 2,
        "bms_total_direct": 3,
    }
    return order.get(source_name, 99)


def _first_cluster_system(trend_cluster_daily: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the first trend system payload from the cluster trend response."""
    for key in ("trendClusterSystemList", "realTrendClusterSystemList"):
        systems = trend_cluster_daily.get(key)
        if isinstance(systems, list):
            for item in systems:
                if isinstance(item, Mapping):
                    return item
    return {}


def _trend_list(trend_system: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the hourly trend rows for the current system."""
    rows = trend_system.get("trendList")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _sum_positive(rows: Iterable[Mapping[str, Any]], key: str) -> float | None:
    """Sum only the positive numeric values of a trend column."""
    total = 0.0
    seen = False
    for row in rows:
        value = _coerce_float(row.get(key))
        if value is None:
            continue
        if value > 0:
            total += value
        seen = True
    return round(total, 3) if seen else None


def _sum_negative_as_positive(rows: Iterable[Mapping[str, Any]], key: str) -> float | None:
    """Sum negative values as positive numbers.

    The observed cluster trend reports battery charging as a negative value.
    For Home Assistant, the daily charged energy is exposed as a positive
    cumulative amount.
    """
    total = 0.0
    seen = False
    for row in rows:
        value = _coerce_float(row.get(key))
        if value is None:
            continue
        if value < 0:
            total += abs(value)
        seen = True
    return round(total, 3) if seen else None


def _sum_bms_values(rows: Any, key: str) -> float | None:
    """Sum the per-BMS trend values as a fallback when no total is present."""
    if not isinstance(rows, list):
        return None
    total = 0.0
    seen = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = _coerce_float(row.get(key))
        if value is None:
            continue
        total += value
        seen = True
    return round(total, 3) if seen else None


def _prefer_value(*values: float | None) -> float | None:
    """Return the first non-None value from multiple candidates."""
    for value in values:
        if value is not None:
            return value
    return None


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


def _mqtt_timestamp_ms() -> str:
    """Return a Unix timestamp in milliseconds as a string."""
    return str(int(dt_util.utcnow().timestamp() * 1000))


def _coerce_float(value: Any) -> float | None:
    """Convert API values to float when possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
