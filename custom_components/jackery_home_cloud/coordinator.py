"""Data coordinator for Jackery Home Cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
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
    DOMAIN,
    TREND_DATE_FORMAT,
    UPDATE_INTERVAL_SECONDS,
)
from .exceptions import JackeryHomeApiError, JackeryHomeAuthError

_LOGGER = logging.getLogger(__name__)


class JackeryHomeCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate REST refreshes for all selected Jackery systems."""

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

    async def _async_setup(self) -> None:
        """Perform one-time bootstrap work before the first refresh."""
        await self._async_login()
        self._app_user = await self._async_api_call(self.client.async_get_app_user)
        self._mqtt_credentials = await self._async_api_call(
            self.client.async_get_mqtt_credentials
        )

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

        system_results = await asyncio.gather(
            *(
                self._async_fetch_system_bundle(
                    system_id,
                    systems_by_id.get(system_id, {}),
                )
                for system_id in selected_system_ids
            )
        )

        return {
            "account": self._account,
            "app_user": self._app_user,
            "mqtt_credentials": self._mqtt_credentials,
            "available_systems": {
                system_id: dict(system)
                for system_id, system in systems_by_id.items()
            },
            "selected_system_ids": list(selected_system_ids),
            "systems": {system_id: bundle for system_id, bundle in system_results},
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
    ) -> tuple[str, dict[str, Any]]:
        """Fetch all currently mapped data for a single system.

        The integration keeps the live monitor call as the primary source for
        current power values. In addition, it loads the daily trend endpoints to
        derive cumulative energy sensors for the current local day of the system.
        """
        monitor = await self._async_api_call(self.client.async_get_monitor, system_id)

        day_key = _system_local_day_key(monitor)
        trend_cluster_daily, battery_bms_daily = await asyncio.gather(
            self._async_optional_api_call(
                self.client.async_get_cluster_trend_daily,
                system_id,
                day_key,
                default={},
            ),
            self._async_optional_api_call(
                self.client.async_get_battery_bms_trend_daily,
                system_id,
                day_key,
                default={},
            ),
        )

        merged_system = dict(system)
        merged_system.update(monitor.get("systemVO") or {})

        return system_id, {
            "system": merged_system,
            "monitor": monitor,
            "devices": {},
            "trend_cluster_daily": trend_cluster_daily,
            "battery_bms_daily": battery_bms_daily,
            "daily_energy": _build_daily_energy_summary(
                trend_cluster_daily,
                battery_bms_daily,
            ),
            "trend_day_key": day_key,
        }

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
        """Run a non-critical API call and degrade gracefully on API errors.

        The daily trend endpoints are valuable but should not break the complete
        integration when the cloud backend temporarily omits them. Auth failures
        still propagate because they indicate that the whole session is invalid.
        """
        try:
            return await self._async_api_call(func, *args)
        except UpdateFailed as err:
            _LOGGER.warning("Optional Jackery API call failed: %s", err)
            return default

    def _resolve_selected_system_ids(
        self, systems_by_id: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Resolve the selected system ids for the current account.

        The options flow is the canonical storage location. The entry.data
        fallback is kept for safety while older entries are migrated.
        """
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
    """Return the current day key for the system timezone.

    The trend endpoints expect local calendar dates in YYYYMMDD format. The
    monitor payload contains the timezone configured for the system, which is
    used here to align the request with the same day the mobile app shows.
    """
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
    trend_cluster_daily: Mapping[str, Any],
    battery_bms_daily: Mapping[str, Any],
) -> dict[str, float | None]:
    """Derive user facing daily energy totals from raw trend payloads."""
    trend_system = _first_cluster_system(trend_cluster_daily)
    trend_list = _trend_list(trend_system)

    return {
        "solar_energy_generated_today": _prefer_value(
            _coerce_float(trend_system.get("pvChargeAmountTotal")),
            _coerce_float(trend_cluster_daily.get("pvChargeAmountTotal")),
            _sum_positive(trend_list, "pvChargeAmount"),
        ),
        "battery_energy_charged_today": _prefer_value(
            _battery_total_kwh(battery_bms_daily.get("totalCharge")),
            _sum_bms_values(battery_bms_daily.get("bmsList"), "charge"),
            _sum_negative_as_positive(trend_list, "batteryCharge"),
        ),
        "battery_energy_discharged_today": _prefer_value(
            _battery_total_kwh(battery_bms_daily.get("totalDisCharge")),
            _sum_bms_values(battery_bms_daily.get("bmsList"), "disCharge"),
            _sum_positive(trend_list, "batteryDischarge"),
        ),
        "grid_energy_exported_today": _sum_positive(trend_list, "gridOut"),
        "grid_energy_imported_today": _sum_positive(trend_list, "gridInput"),
        "pv1_energy_today": _sum_positive(trend_list, "pv1TotalGen"),
        "pv2_energy_today": _sum_positive(trend_list, "pv2TotalGen"),
        "on_grid_energy_exported_today": _sum_positive(trend_list, "onGridOut"),
    }


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

    The observed daily cluster trend reports battery charging as a negative
    value. For Home Assistant, the daily charged energy is exposed as a positive
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


def _battery_total_kwh(value: Any) -> float | None:
    """Normalize the BMS total fields to kWh.

    The observed API returns daily BMS totals in Wh-like units such as 1976.00
    while the per-BMS list exposes 1.97 for the same day. The helper converts
    the larger total fields to kWh for a consistent Home Assistant entity model.
    """
    numeric = _coerce_float(value)
    if numeric is None:
        return None
    if numeric >= 50:
        return round(numeric / 1000, 3)
    return round(numeric, 3)


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


def _coerce_float(value: Any) -> float | None:
    """Convert API values to float when possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
