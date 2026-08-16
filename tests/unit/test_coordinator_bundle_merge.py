"""Tests for JackeryHomeCloudCoordinator._apply_mqtt_live_values_to_bundle
(Family C: coordinator ingestion/merge logic).

This targets the MQTT bundle merge and the complete AC-main sign decision
sequence: full PV/battery/EPS balance, conservative partial-data fallbacks,
the battery-only fallback, and freshness gating for every input.

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
import pytest

from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
)
from custom_components.jackery_home_cloud.const import AC_MAIN_IDLE_POWER_THRESHOLD_W
from homeassistant.util import dt as dt_util

SYSTEM_ID = "sys1"


def _make_coordinator(live_values: dict) -> JackeryHomeCloudCoordinator:
    """Build a coordinator instance without running __init__/HA setup."""
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator._mqtt_live_values = {SYSTEM_ID: live_values}
    return coordinator


def _ac_main_live_values(
    now,
    *,
    ac_main: float,
    pv1: float | None = None,
    pv2: float | None = None,
    battery: float | None = None,
    eps: float | None = None,
    stale: frozenset[str] = frozenset(),
) -> dict:
    """Build timestamped AC-main inputs, optionally making sources stale."""
    values = {
        "ac_main_power_mqtt": ac_main,
        "ac_main_power_mqtt_at": now,
    }
    inputs = {
        "pv1_power_mqtt": pv1,
        "pv2_power_mqtt": pv2,
        "battery_power_mqtt": battery,
        "eps_load_power_mqtt": eps,
    }
    for key, value in inputs.items():
        if value is None:
            continue
        values[key] = value
        values[f"{key}_at"] = (
            now - timedelta(seconds=121) if key in stale else now
        )
    return values


class TestNoLiveValues:
    def test_missing_system_returns_bundle_unchanged(self):
        coordinator = _make_coordinator({})
        coordinator._mqtt_live_values = {}
        bundle = {"some_key": "value"}
        merged = coordinator._apply_mqtt_live_values_to_bundle("unknown_system", bundle)
        assert merged == bundle
        assert merged is not bundle  # must return a copy, not the original


class TestExistingMqttBundleValuesRemainMerged:
    """Guard unrelated MQTT bundle fields while AC-main logic is refactored.

    Both fields used to be merged immediately next to the AC-main sign logic.
    Keeping these assertions at the coordinator boundary prevents a large
    rewrite of that logic from silently dropping values which downstream
    sensors and switches still expect in the system bundle.
    """

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_mqtt_daily_energy_updates_preserve_rest_summary(self):
        now = dt_util.utcnow()
        day_key = "20260101"
        coordinator = _make_coordinator(
            {
                "battery_energy_charged_today": 2.5,
                "battery_energy_charged_today_at": now,
                "battery_energy_charged_today_day_key": day_key,
                "battery_energy_discharged_today": 1.25,
                "battery_energy_discharged_today_at": now,
                "battery_energy_discharged_today_day_key": day_key,
            }
        )
        rest_daily_energy = {
            "solar_energy_generated_today": 4.0,
            "battery_energy_charged_today": 1.0,
            "battery_energy_discharged_today": 0.5,
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(
            SYSTEM_ID,
            {
                "daily_energy": rest_daily_energy,
                "trend_day_key": day_key,
            },
        )

        # Fresh MQTT counters replace only their matching REST/trend values;
        # unrelated members of the already assembled summary must survive.
        assert merged["daily_energy"] == {
            "solar_energy_generated_today": 4.0,
            "battery_energy_charged_today": 2.5,
            "battery_energy_discharged_today": 1.25,
        }
        assert merged["mqtt_live"]["battery_energy_charged_today"]["source"] == "mqtt"
        assert merged["mqtt_live"]["battery_energy_discharged_today"]["source"] == "mqtt"

    @pytest.mark.parametrize("ac_output_state", (True, False))
    @freeze_time("2026-01-01 12:00:00")
    def test_ac_output_state_and_mqtt_metadata_are_merged(self, ac_output_state):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                "ac_output_state": ac_output_state,
                "ac_output_state_at": now,
                "ac_output_state_source": "mqtt_data_set",
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        # The switch consumes the top-level value, while diagnostics and
        # availability handling consume the accompanying mqtt_live metadata.
        assert merged["ac_output_state"] is ac_output_state
        assert merged["mqtt_live"]["ac_output_state"] == {
            "value": ac_output_state,
            "source": "mqtt_data_set",
            "age_seconds": 0.0,
        }


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


class TestAcMainPowerDecisionMatrix:
    """Lock down every documented AC-main sign-decision path.

    The balance inputs determine only the sign. The value's magnitude must
    always remain the absolute raw AC-main MQTT magnitude, and mqtt_live must
    explain the selected decision for field diagnostics.
    """

    @pytest.mark.parametrize(
        (
            "inputs",
            "expected_value",
            "expected_candidate",
            "expected_delta",
            "expected_indicator",
            "expected_source",
        ),
        (
            pytest.param(
                {
                    "ac_main": 363.0,
                    "pv1": 0.0,
                    "pv2": 0.0,
                    "battery": 234.0,
                    "eps": -632.0,
                },
                363.0,
                398.0,
                35.0,
                1.0,
                "pv_battery_eps_balance_positive",
                id="complete-positive-observed-device-values",
            ),
            pytest.param(
                {
                    "ac_main": 363.0,
                    "pv1": 0.0,
                    "pv2": 0.0,
                    "battery": 200.0,
                    "eps": 100.0,
                },
                -363.0,
                -300.0,
                -663.0,
                -1.0,
                "pv_battery_eps_balance_negative",
                id="complete-negative-balance",
            ),
            pytest.param(
                {
                    "ac_main": AC_MAIN_IDLE_POWER_THRESHOLD_W,
                    "pv1": AC_MAIN_IDLE_POWER_THRESHOLD_W,
                    "pv2": AC_MAIN_IDLE_POWER_THRESHOLD_W,
                    "battery": AC_MAIN_IDLE_POWER_THRESHOLD_W,
                    "eps": AC_MAIN_IDLE_POWER_THRESHOLD_W,
                },
                -AC_MAIN_IDLE_POWER_THRESHOLD_W,
                0.0,
                -AC_MAIN_IDLE_POWER_THRESHOLD_W,
                -1.0,
                "internal_consumption_idle",
                id="idle-threshold-is-inclusive",
            ),
            pytest.param(
                {
                    "ac_main": AC_MAIN_IDLE_POWER_THRESHOLD_W + 0.01,
                    "pv1": 0.0,
                    "pv2": 0.0,
                    "battery": 0.0,
                    "eps": 0.0,
                },
                AC_MAIN_IDLE_POWER_THRESHOLD_W + 0.01,
                0.0,
                -(AC_MAIN_IDLE_POWER_THRESHOLD_W + 0.01),
                None,
                "zero_balance_fallback",
                id="complete-zero-balance-remains-unsigned",
            ),
            pytest.param(
                {"ac_main": 120.0, "battery": -100.0, "eps": 20.0},
                120.0,
                80.0,
                -40.0,
                1.0,
                "battery_eps_minimum_balance_positive",
                id="missing-pv-positive-minimum-balance",
            ),
            pytest.param(
                {"ac_main": 120.0, "battery": 100.0, "eps": 20.0},
                120.0,
                -120.0,
                -240.0,
                None,
                "battery_eps_minimum_balance_inconclusive",
                id="missing-pv-inconclusive-remains-unsigned",
            ),
            pytest.param(
                {"ac_main": 40.0, "pv1": 100.0, "pv2": 10.0, "battery": 50.0},
                40.0,
                60.0,
                20.0,
                1.0,
                "pv_battery_fallback_positive",
                id="missing-eps-pv-battery-positive",
            ),
            pytest.param(
                {"ac_main": 40.0, "pv1": 0.0, "pv2": 0.0, "battery": 100.0},
                -40.0,
                -100.0,
                -140.0,
                -1.0,
                "pv_battery_fallback_negative",
                id="missing-eps-pv-battery-negative",
            ),
            pytest.param(
                {"ac_main": 40.0, "pv1": 50.0, "pv2": 50.0, "battery": 100.0},
                40.0,
                0.0,
                -40.0,
                None,
                "pv_battery_fallback_zero",
                id="missing-eps-pv-battery-zero-remains-unsigned",
            ),
            pytest.param(
                {"ac_main": 363.0, "battery": 234.0},
                -363.0,
                None,
                None,
                -234.0,
                "battery_fallback",
                id="battery-only-charging",
            ),
            pytest.param(
                {"ac_main": 363.0, "battery": -234.0},
                363.0,
                None,
                None,
                234.0,
                "battery_fallback",
                id="battery-only-discharging",
            ),
            pytest.param(
                {"ac_main": -363.0, "battery": 0.0},
                363.0,
                None,
                None,
                None,
                "unsigned_fallback",
                id="zero-battery-remains-unsigned",
            ),
            pytest.param(
                {"ac_main": -363.0},
                363.0,
                None,
                None,
                None,
                "unsigned_fallback",
                id="no-sign-input-remains-unsigned",
            ),
            pytest.param(
                {
                    "ac_main": 120.0,
                    "pv1": 100.0,
                    "pv2": 100.0,
                    "battery": -100.0,
                    "eps": 20.0,
                    "stale": frozenset({"pv2_power_mqtt"}),
                },
                120.0,
                80.0,
                -40.0,
                1.0,
                "battery_eps_minimum_balance_positive",
                id="stale-pv-uses-minimum-balance",
            ),
            pytest.param(
                {
                    "ac_main": 40.0,
                    "pv1": 100.0,
                    "pv2": 0.0,
                    "battery": 50.0,
                    "eps": 100.0,
                    "stale": frozenset({"eps_load_power_mqtt"}),
                },
                40.0,
                50.0,
                10.0,
                1.0,
                "pv_battery_fallback_positive",
                id="stale-eps-uses-pv-battery-fallback",
            ),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_sign_decision_and_diagnostics(
        self,
        inputs,
        expected_value,
        expected_candidate,
        expected_delta,
        expected_indicator,
        expected_source,
    ):
        now = dt_util.utcnow()
        raw_magnitude = inputs["ac_main"]
        live = _ac_main_live_values(
            now,
            ac_main=raw_magnitude,
            pv1=inputs.get("pv1"),
            pv2=inputs.get("pv2"),
            battery=inputs.get("battery"),
            eps=inputs.get("eps"),
            stale=inputs.get("stale", frozenset()),
        )
        coordinator = _make_coordinator(live)

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == expected_value
        # Balance inputs may select only the sign; they must never replace the
        # raw meter's magnitude, even if their numerical difference is large.
        assert abs(merged["ac_main_power_mqtt"]) == abs(raw_magnitude)
        metadata = merged["mqtt_live"]["ac_main_power_mqtt"]
        assert metadata == {
            "value": expected_value,
            "source": "mqtt",
            "raw_magnitude": raw_magnitude,
            "balance_candidate": expected_candidate,
            "balance_delta": expected_delta,
            "sign_indicator": expected_indicator,
            "sign_source": expected_source,
        }


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

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_ac_output_energy_in_total_is_merged(self):
        now = dt_util.utcnow()
        live = {
            "ac_output_energy_in": 12.5,
            "ac_output_energy_in_at": now,
        }
        coordinator = _make_coordinator(live)

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_output_energy_in"] == 12.5

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_ac_output_energy_out_total_is_merged(self):
        now = dt_util.utcnow()
        live = {
            "ac_output_energy_out": 8.25,
            "ac_output_energy_out_at": now,
        }
        coordinator = _make_coordinator(live)

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_output_energy_out"] == 8.25

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_ac_output_energy_total_is_not_merged(self):
        stale_timestamp = dt_util.utcnow() - timedelta(seconds=901)
        coordinator = _make_coordinator(
            {
                "ac_output_energy_in": 12.5,
                "ac_output_energy_in_at": stale_timestamp,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert "ac_output_energy_in" not in merged

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_ac_output_energy_out_total_is_not_merged(self):
        stale_timestamp = dt_util.utcnow() - timedelta(seconds=901)
        coordinator = _make_coordinator(
            {
                "ac_output_energy_out": 8.25,
                "ac_output_energy_out_at": stale_timestamp,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert "ac_output_energy_out" not in merged


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
