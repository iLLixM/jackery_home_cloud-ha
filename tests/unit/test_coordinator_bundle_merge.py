"""Tests for JackeryHomeCloudCoordinator._apply_mqtt_live_values_to_bundle
(Family C: coordinator ingestion/merge logic).

This targets the exact logic discussed for PR #8 in this project's
history: the unsigned AC-main-power MQTT meter is signed using
`-sign(battery_power_mqtt)`, and every MQTT-sourced live value is gated
behind a freshness window before being merged into the REST bundle.

The method under test only reads/writes `self._mqtt_live_values` and
plain dict arguments - it never touches `self.hass`/`self.data`/network
I/O - so the coordinator is constructed via `__new__` here instead of the
real `__init__` (which requires a live `hass` + `ConfigEntry` + API
client). This keeps the test a fast, dependency-free unit test while
still exercising the real production method.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
)
from homeassistant.util import dt as dt_util

SYSTEM_ID = "sys1"


def _make_coordinator(live_values: dict) -> JackeryHomeCloudCoordinator:
    """Build a coordinator instance without running __init__/HA setup."""
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator._mqtt_live_values = {SYSTEM_ID: live_values}
    return coordinator


class TestNoLiveValues:
    def test_missing_system_returns_bundle_unchanged(self):
        coordinator = _make_coordinator({})
        coordinator._mqtt_live_values = {}
        bundle = {"some_key": "value"}
        merged = coordinator._apply_mqtt_live_values_to_bundle("unknown_system", bundle)
        assert merged == bundle
        assert merged is not bundle  # must return a copy, not the original


class TestAcMainPowerSignDerivation:
    @freeze_time("2026-01-01 12:00:00")
    def test_charging_flips_ac_main_to_negative(self):
        now = dt_util.utcnow()
        live = {
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": now,
            "ac_main_power_mqtt": 1469.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["battery_power_mqtt"] == 1390.0
        assert merged["ac_main_power_mqtt"] == -1469.0

    @freeze_time("2026-01-01 12:00:00")
    def test_discharging_flips_ac_main_to_positive(self):
        now = dt_util.utcnow()
        live = {
            "battery_power_mqtt": -1286.0,
            "battery_power_mqtt_at": now,
            "ac_main_power_mqtt": 1222.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["battery_power_mqtt"] == -1286.0
        assert merged["ac_main_power_mqtt"] == 1222.0

    @freeze_time("2026-01-01 12:00:00")
    def test_battery_power_exactly_zero_leaves_ac_main_unsigned(self):
        now = dt_util.utcnow()
        live = {
            "battery_power_mqtt": 0.0,
            "battery_power_mqtt_at": now,
            "ac_main_power_mqtt": 500.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["ac_main_power_mqtt"] == 500.0

    @freeze_time("2026-01-01 12:00:00")
    def test_missing_battery_power_leaves_ac_main_unsigned(self):
        now = dt_util.utcnow()
        live = {
            "ac_main_power_mqtt": 500.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["ac_main_power_mqtt"] == 500.0

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_battery_power_falls_back_to_unsigned_ac_main(self):
        now = dt_util.utcnow()
        stale_timestamp = now - timedelta(seconds=121)
        live = {
            # battery_power_mqtt itself is stale, so it never gets written
            # into `merged` at all - the sign-derivation code must treat
            # that as "sign unknown" and fall back to the unsigned
            # magnitude, rather than crash or use an unrelated value.
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": stale_timestamp,
            "ac_main_power_mqtt": 1469.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert "battery_power_mqtt" not in merged
        assert merged["ac_main_power_mqtt"] == 1469.0

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_ac_main_is_not_merged_at_all(self):
        now = dt_util.utcnow()
        stale_timestamp = now - timedelta(seconds=121)
        live = {
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": now,
            "ac_main_power_mqtt": 1469.0,
            "ac_main_power_mqtt_at": stale_timestamp,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert "ac_main_power_mqtt" not in merged

    @freeze_time("2026-01-01 12:00:00")
    def test_ac_main_freshness_boundary_is_inclusive(self):
        now = dt_util.utcnow()
        boundary_timestamp = now - timedelta(seconds=120)
        live = {
            "battery_power_mqtt": 100.0,
            "battery_power_mqtt_at": now,
            "ac_main_power_mqtt": 42.0,
            "ac_main_power_mqtt_at": boundary_timestamp,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["ac_main_power_mqtt"] == -42.0

    @freeze_time("2026-01-01 12:00:00")
    def test_battery_power_freshness_boundary_still_flips_sign(self):
        """battery_power_mqtt's own freshness gate (evaluated in the
        general power-value loop, separate from ac_main's own gate) is
        exactly at its boundary here, not stale - the general freshness
        loop's `<=` comparison (same as ac_main's own, see
        test_ac_main_freshness_boundary_is_inclusive) must still merge it
        into `merged`, so the sign-derivation step immediately after can
        still read it and flip ac_main's sign, rather than silently falling
        back to unsigned because it looked for staleness in the wrong
        place."""
        now = dt_util.utcnow()
        boundary_timestamp = now - timedelta(seconds=120)
        live = {
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": boundary_timestamp,
            "ac_main_power_mqtt": 1469.0,
            "ac_main_power_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["battery_power_mqtt"] == 1390.0
        assert merged["ac_main_power_mqtt"] == -1469.0


class TestPowerValueFreshnessGating:
    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_power_value_is_merged(self):
        now = dt_util.utcnow()
        live = {"grid_power_mqtt": 77.0, "grid_power_mqtt_at": now}
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["grid_power_mqtt"] == 77.0

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_power_value_is_not_merged(self):
        now = dt_util.utcnow()
        stale_timestamp = now - timedelta(seconds=121)
        live = {"grid_power_mqtt": 77.0, "grid_power_mqtt_at": stale_timestamp}
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert "grid_power_mqtt" not in merged

    def test_missing_timestamp_is_never_merged(self):
        live = {"grid_power_mqtt": 77.0}
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert "grid_power_mqtt" not in merged


class TestEnergyCounterFreshnessGating:
    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_energy_total_is_merged(self):
        now = dt_util.utcnow()
        live = {
            "battery_energy_charged_total": 12.5,
            "battery_energy_charged_total_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["battery_energy_charged_total"] == 12.5

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_energy_total_is_not_merged(self):
        now = dt_util.utcnow()
        stale_timestamp = now - timedelta(seconds=901)
        live = {
            "battery_energy_charged_total": 12.5,
            "battery_energy_charged_total_at": stale_timestamp,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert "battery_energy_charged_total" not in merged


class TestSettingsPersistWithoutFreshnessGate:
    def test_work_mode_raw_has_no_staleness_gate(self):
        # No `_at` timestamp needed at all for settings/limits.
        live = {"work_mode_raw": "02"}
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["work_mode_raw"] == "02"


class TestBms1AndEmsBatteryPowerAreIndependentKeys:
    @freeze_time("2026-01-01 12:00:00")
    def test_bms1_and_ems_merge_independently(self):
        now = dt_util.utcnow()
        live = {
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": now,
            "battery_power_bms1_mqtt": 1385.0,
            "battery_power_bms1_mqtt_at": now,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["battery_power_mqtt"] == 1390.0
        assert merged["battery_power_bms1_mqtt"] == 1385.0
