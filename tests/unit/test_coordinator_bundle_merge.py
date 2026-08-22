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
    _MQTT_FRESHNESS_GATED_DAILY_ENERGY_KEYS,
    _MQTT_FRESHNESS_GATED_ENERGY_KEYS,
    _MQTT_FRESHNESS_GATED_POWER_KEYS,
    _MQTT_FRESHNESS_GATED_SLOW_BMS1_KEYS,
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)
from custom_components.jackery_home_cloud.const import (
    AC_MAIN_IDLE_POWER_THRESHOLD_W,
    AC_MAIN_MINIMUM_BALANCE_MARGIN_W,
    AC_MAIN_SAMPLE_MAX_SKEW_SECONDS,
    MQTT_SLOW_BMS1_VALUE_MAX_AGE_SECONDS,
)
from homeassistant.util import dt as dt_util

SYSTEM_ID = "sys1"

EXPECTED_FRESHNESS_GATED_ENERGY_KEYS = {
    "battery_energy_charged_total",
    "battery_energy_discharged_total",
    "ac_output_energy_in",
    "ac_output_energy_out",
    "pv1_energy_total",
    "pv2_energy_total",
    "pv_energy_total",
}
EXPECTED_FRESHNESS_GATED_POWER_KEYS = {
    "battery_soc_mqtt",
    "pv_power_mqtt",
    "battery_power_mqtt",
    "battery_power_bms1_mqtt",
    "other_load_power_mqtt",
    "grid_power_mqtt",
    "eps_load_power_mqtt",
    "ac_main_power_mqtt",
    "pcs_active_power_l1_mqtt",
    "pcs_apparent_power_mqtt",
    "pcs_active_power_mqtt",
    "ems_other_load_power_l1_mqtt",
    "ems_on_grid_power_mqtt",
}
EXPECTED_FRESHNESS_GATED_SLOW_BMS1_KEYS = {
    "bms1_temperature_ambient_mqtt",
    "bms1_temperature_avg_cell_mqtt",
}
EXPECTED_FRESHNESS_GATED_DAILY_ENERGY_KEYS = {
    "battery_energy_charged_today",
    "battery_energy_discharged_today",
}


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
    skew_seconds: dict[str, float] | None = None,
) -> dict:
    """Build timestamped AC-main inputs with optional age/skew overrides."""
    skew_seconds = skew_seconds or {}
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
        sample_skew = 121 if key in stale else skew_seconds.get(key, 0)
        values[f"{key}_at"] = now - timedelta(seconds=sample_skew)
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
        assert merged["mqtt_live"]["battery_energy_charged_today"][
            "fallback_value"
        ] == 1.0
        assert merged["mqtt_live"]["battery_energy_discharged_today"][
            "fallback_value"
        ] == 0.5

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


class TestExpiredMqttOverlaysAreRemoved:
    """Exercise repeated merges of an already MQTT-enriched bundle.

    A simple merge into an empty REST bundle cannot reproduce the runtime
    failure: `_publish_runtime_update` starts from `self.data`, which may still
    contain values and metadata written by an earlier merge. Every sampled
    MQTT overlay must therefore be cleared before freshness is evaluated.
    """

    def test_freshness_key_sets_cover_every_current_sampled_overlay(self):
        """Make additions to the production cleanup lists an explicit choice."""
        assert (
            _MQTT_FRESHNESS_GATED_ENERGY_KEYS
            == EXPECTED_FRESHNESS_GATED_ENERGY_KEYS
        )
        assert (
            _MQTT_FRESHNESS_GATED_POWER_KEYS
            == EXPECTED_FRESHNESS_GATED_POWER_KEYS
        )
        assert (
            _MQTT_FRESHNESS_GATED_SLOW_BMS1_KEYS
            == EXPECTED_FRESHNESS_GATED_SLOW_BMS1_KEYS
        )
        assert (
            _MQTT_FRESHNESS_GATED_DAILY_ENERGY_KEYS
            == EXPECTED_FRESHNESS_GATED_DAILY_ENERGY_KEYS
        )

    @pytest.mark.parametrize("key", sorted(EXPECTED_FRESHNESS_GATED_ENERGY_KEYS))
    @freeze_time("2026-01-01 12:00:00")
    def test_expired_energy_overlay_and_metadata_are_removed(self, key):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                key: 42.0,
                f"{key}_at": now - timedelta(seconds=901),
                # Settings are deliberately not freshness-gated and provide
                # a guard against clearing the whole mqtt_live structure.
                "work_mode_raw": "02",
            }
        )
        bundle = {
            key: 42.0,
            "work_mode_raw": "02",
            "mqtt_live": {
                key: {"value": 42.0, "source": "mqtt", "age_seconds": 0.0},
                "work_mode_raw": {"value": "02", "source": "mqtt"},
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert key not in merged
        assert key not in merged["mqtt_live"]
        assert merged["work_mode_raw"] == "02"
        assert merged["mqtt_live"]["work_mode_raw"]["value"] == "02"

    @pytest.mark.parametrize(
        "key", sorted(EXPECTED_FRESHNESS_GATED_SLOW_BMS1_KEYS)
    )
    @pytest.mark.parametrize("age_seconds", (121, 300, 600, 900))
    @freeze_time("2026-01-01 12:00:00")
    def test_slow_bms1_temperature_remains_fresh_through_three_poll_cycles(
        self, key, age_seconds
    ):
        """Keep temperatures stable between polls and across one lost poll."""
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                key: 26.1,
                f"{key}_at": now - timedelta(seconds=age_seconds),
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert age_seconds <= MQTT_SLOW_BMS1_VALUE_MAX_AGE_SECONDS
        assert merged[key] == 26.1
        assert merged["mqtt_live"][key] == {
            "value": 26.1,
            "source": "mqtt",
        }

    @pytest.mark.parametrize(
        "key", sorted(EXPECTED_FRESHNESS_GATED_SLOW_BMS1_KEYS)
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_slow_bms1_temperature_is_removed_after_its_freshness_window(
        self, key
    ):
        """Drop both the value and provenance only after 900 seconds."""
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                key: 26.1,
                f"{key}_at": now
                - timedelta(seconds=MQTT_SLOW_BMS1_VALUE_MAX_AGE_SECONDS + 1),
                "work_mode_raw": "02",
            }
        )
        bundle = {
            key: 26.1,
            "work_mode_raw": "02",
            "mqtt_live": {
                key: {"value": 26.1, "source": "mqtt"},
                "work_mode_raw": {"value": "02", "source": "mqtt"},
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert key not in merged
        assert key not in merged["mqtt_live"]
        assert merged["work_mode_raw"] == "02"
        assert merged["mqtt_live"]["work_mode_raw"]["value"] == "02"

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_value_is_reapplied_after_previous_overlay_is_cleared(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                "ac_output_energy_in": 12.75,
                "ac_output_energy_in_at": now,
            }
        )
        bundle = {
            "ac_output_energy_in": 12.5,
            "mqtt_live": {
                "ac_output_energy_in": {
                    "value": 12.5,
                    "source": "mqtt",
                    "age_seconds": 300.0,
                }
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert merged["ac_output_energy_in"] == 12.75
        assert merged["mqtt_live"]["ac_output_energy_in"] == {
            "value": 12.75,
            "source": "mqtt",
            "age_seconds": 0.0,
        }

    @pytest.mark.parametrize("key", sorted(EXPECTED_FRESHNESS_GATED_POWER_KEYS))
    @freeze_time("2026-01-01 12:00:00")
    def test_expired_power_overlay_and_metadata_are_removed(self, key):
        now = dt_util.utcnow()
        stale_timestamp = now - timedelta(seconds=121)
        if key == "pv_power_mqtt":
            live = {
                "pv1_power_mqtt": 20.0,
                "pv1_power_mqtt_at": stale_timestamp,
                "pv2_power_mqtt": 22.0,
                "pv2_power_mqtt_at": stale_timestamp,
            }
        else:
            live = {key: 42.0, f"{key}_at": stale_timestamp}
        live["work_mode_raw"] = "02"
        coordinator = _make_coordinator(live)
        bundle = {
            key: 42.0,
            "work_mode_raw": "02",
            "mqtt_live": {
                key: {"value": 42.0, "source": "mqtt"},
                "work_mode_raw": {"value": "02", "source": "mqtt"},
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert key not in merged
        assert key not in merged["mqtt_live"]
        assert merged["work_mode_raw"] == "02"
        assert merged["mqtt_live"]["work_mode_raw"]["value"] == "02"

    @pytest.mark.parametrize(
        "key",
        sorted(EXPECTED_FRESHNESS_GATED_DAILY_ENERGY_KEYS),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_expired_legacy_daily_overlay_without_fallback_is_removed(self, key):
        now = dt_util.utcnow()
        day_key = "20260101"
        coordinator = _make_coordinator(
            {
                key: 4.2,
                f"{key}_at": now - timedelta(seconds=901),
                f"{key}_day_key": day_key,
            }
        )
        bundle = {
            "trend_day_key": day_key,
            "daily_energy": {
                "solar_energy_generated_today": 8.0,
                key: 4.2,
            },
            "mqtt_live": {
                key: {
                    "value": 4.2,
                    "source": "mqtt",
                    "day_key": day_key,
                    "age_seconds": 0.0,
                }
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert key not in merged["daily_energy"]
        assert merged["daily_energy"]["solar_energy_generated_today"] == 8.0
        assert "mqtt_live" not in merged

    @pytest.mark.parametrize(
        "key",
        sorted(EXPECTED_FRESHNESS_GATED_DAILY_ENERGY_KEYS),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_expired_daily_overlay_restores_latest_rest_fallback(self, key):
        now = dt_util.utcnow()
        day_key = "20260101"
        coordinator = _make_coordinator(
            {
                key: 4.2,
                f"{key}_at": now,
                f"{key}_day_key": day_key,
            }
        )

        enriched = coordinator._apply_mqtt_live_values_to_bundle(
            SYSTEM_ID,
            {
                "trend_day_key": day_key,
                "daily_energy": {
                    "solar_energy_generated_today": 8.0,
                    key: 3.5,
                },
            },
        )
        assert enriched["daily_energy"][key] == 4.2
        assert enriched["mqtt_live"][key]["fallback_value"] == 3.5
        assert enriched["mqtt_live"][key]["fallback_day_key"] == day_key

        with freeze_time("2026-01-01 12:15:01"):
            merged = coordinator._apply_mqtt_live_values_to_bundle(
                SYSTEM_ID,
                enriched,
            )

        assert merged["daily_energy"][key] == 3.5
        assert merged["daily_energy"]["solar_energy_generated_today"] == 8.0
        assert "mqtt_live" not in merged

    @pytest.mark.parametrize(
        "key",
        sorted(EXPECTED_FRESHNESS_GATED_DAILY_ENERGY_KEYS),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_previous_day_rest_fallback_is_not_restored(self, key):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                key: 4.2,
                f"{key}_at": now - timedelta(seconds=901),
                f"{key}_day_key": "20260101",
            }
        )
        bundle = {
            "trend_day_key": "20260101",
            "daily_energy": {key: 4.2},
            "mqtt_live": {
                key: {
                    "value": 4.2,
                    "source": "mqtt",
                    "fallback_value": 3.5,
                    "fallback_day_key": "20251231",
                }
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert key not in merged.get("daily_energy", {})
        assert "mqtt_live" not in merged

    @freeze_time("2026-01-01 12:00:00")
    def test_clean_rest_daily_value_survives_stale_mqtt_cache(self):
        now = dt_util.utcnow()
        day_key = "20260101"
        coordinator = _make_coordinator(
            {
                "battery_energy_charged_today": 4.2,
                "battery_energy_charged_today_at": now - timedelta(seconds=901),
                "battery_energy_charged_today_day_key": day_key,
            }
        )
        bundle = {
            "trend_day_key": day_key,
            "daily_energy": {"battery_energy_charged_today": 3.5},
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        # Without prior mqtt_live provenance this is a REST/trend value, not
        # an old overlay, and must remain available as the fallback.
        assert merged["daily_energy"]["battery_energy_charged_today"] == 3.5
        assert "mqtt_live" not in merged

    @freeze_time("2026-01-01 12:00:00")
    def test_runtime_update_removes_expired_overlays_across_value_groups(self):
        now = dt_util.utcnow()
        stale_energy_timestamp = now - timedelta(seconds=901)
        stale_power_timestamp = now - timedelta(seconds=121)
        coordinator = _make_coordinator(
            {
                "ac_output_energy_in": 12.5,
                "ac_output_energy_in_at": stale_energy_timestamp,
                "grid_power_mqtt": 250.0,
                "grid_power_mqtt_at": stale_power_timestamp,
                "battery_energy_charged_today": 4.2,
                "battery_energy_charged_today_at": stale_energy_timestamp,
                "battery_energy_charged_today_day_key": "20260101",
                "work_mode_raw": "02",
            }
        )
        coordinator.mqtt_system = JackeryMqttSystem(
            system_id=SYSTEM_ID,
            device_serial="SN1",
        )
        coordinator._mqtt_state = {"connected": True}
        coordinator.data = {
            "systems": {
                SYSTEM_ID: {
                    "system": {"systemNo": "SN1"},
                    "trend_day_key": "20260101",
                    "ac_output_energy_in": 12.5,
                    "grid_power_mqtt": 250.0,
                    "work_mode_raw": "02",
                    "daily_energy": {
                        "solar_energy_generated_today": 8.0,
                        "battery_energy_charged_today": 4.2,
                    },
                    "mqtt_live": {
                        "ac_output_energy_in": {"source": "mqtt"},
                        "grid_power_mqtt": {"source": "mqtt"},
                        "battery_energy_charged_today": {
                            "source": "mqtt",
                            "fallback_value": 3.5,
                            "fallback_day_key": "20260101",
                        },
                        "work_mode_raw": {"source": "mqtt"},
                    },
                }
            }
        }
        listener_calls = []
        coordinator.async_update_listeners = lambda: listener_calls.append(True)

        coordinator._publish_runtime_update()

        merged = coordinator.data["systems"][SYSTEM_ID]
        assert "ac_output_energy_in" not in merged
        assert "grid_power_mqtt" not in merged
        assert merged["daily_energy"]["battery_energy_charged_today"] == 3.5
        assert merged["daily_energy"]["solar_energy_generated_today"] == 8.0
        assert merged["work_mode_raw"] == "02"
        assert set(merged["mqtt_live"]) == {"work_mode_raw"}
        assert listener_calls == [True]


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
            # Both values belong to the same report, so this test isolates
            # the general freshness boundary from sample-coherence gating.
            "battery_power_mqtt_at": boundary_timestamp,
            "ac_main_power_mqtt": 42.0,
            "ac_main_power_mqtt_at": boundary_timestamp,
        }
        coordinator = _make_coordinator(live)
        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
        assert merged["ac_main_power_mqtt"] == -42.0

    @freeze_time("2026-01-01 12:00:00")
    def test_battery_power_freshness_boundary_still_flips_sign(self):
        """A coherent battery sample remains usable at the age boundary."""
        now = dt_util.utcnow()
        boundary_timestamp = now - timedelta(seconds=120)
        live = {
            "battery_power_mqtt": 1390.0,
            "battery_power_mqtt_at": boundary_timestamp,
            "ac_main_power_mqtt": 1469.0,
            "ac_main_power_mqtt_at": boundary_timestamp,
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
                {"ac_main": 120.0, "battery": -20.0, "eps": 20.0},
                120.0,
                0.0,
                -120.0,
                None,
                "battery_eps_minimum_balance_inconclusive",
                id="missing-pv-zero-minimum-balance-remains-unsigned",
            ),
            pytest.param(
                {"ac_main": 120.0, "battery": -21.0, "eps": 20.0},
                120.0,
                1.0,
                -119.0,
                None,
                "battery_eps_minimum_balance_within_margin",
                id="missing-pv-one-watt-residual-remains-unsigned",
            ),
            pytest.param(
                {
                    "ac_main": 120.0,
                    "battery": -(AC_MAIN_MINIMUM_BALANCE_MARGIN_W + 20.0),
                    "eps": 20.0,
                },
                120.0,
                AC_MAIN_MINIMUM_BALANCE_MARGIN_W,
                AC_MAIN_MINIMUM_BALANCE_MARGIN_W - 120.0,
                None,
                "battery_eps_minimum_balance_within_margin",
                id="missing-pv-margin-is-inclusive-and-remains-unsigned",
            ),
            pytest.param(
                {
                    "ac_main": 120.0,
                    "battery": -(AC_MAIN_MINIMUM_BALANCE_MARGIN_W + 21.0),
                    "eps": 20.0,
                },
                120.0,
                AC_MAIN_MINIMUM_BALANCE_MARGIN_W + 1.0,
                AC_MAIN_MINIMUM_BALANCE_MARGIN_W - 119.0,
                1.0,
                "battery_eps_minimum_balance_positive",
                id="missing-pv-above-margin-proves-positive-flow",
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
            skew_seconds=inputs.get("skew_seconds"),
        )
        coordinator = _make_coordinator(live)

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == expected_value
        # Balance inputs may select only the sign; they must never replace the
        # raw meter's magnitude, even if their numerical difference is large.
        assert abs(merged["ac_main_power_mqtt"]) == abs(raw_magnitude)
        metadata = merged["mqtt_live"]["ac_main_power_mqtt"]
        stale = inputs.get("stale", frozenset())
        skew_overrides = inputs.get("skew_seconds", {})
        sample_skew_seconds = {
            name: (
                None
                if inputs.get(input_name) is None
                else (
                    121.0
                    if live_key in stale
                    else float(skew_overrides.get(live_key, 0.0))
                )
            )
            for name, input_name, live_key in (
                ("pv1", "pv1", "pv1_power_mqtt"),
                ("pv2", "pv2", "pv2_power_mqtt"),
                ("battery", "battery", "battery_power_mqtt"),
                ("eps", "eps", "eps_load_power_mqtt"),
            )
        }
        assert metadata == {
            "value": expected_value,
            "source": "mqtt",
            "raw_magnitude": raw_magnitude,
            "balance_candidate": expected_candidate,
            "balance_delta": expected_delta,
            "sign_indicator": expected_indicator,
            "sign_source": expected_source,
            "minimum_balance_margin_w": AC_MAIN_MINIMUM_BALANCE_MARGIN_W,
            "sample_max_skew_seconds": AC_MAIN_SAMPLE_MAX_SKEW_SECONDS,
            "sample_skew_seconds": sample_skew_seconds,
        }


class TestAcMainPowerSampleCoherence:
    """Prevent valid-but-older values from deciding a newer AC-main sign."""

    @pytest.mark.parametrize(
        ("skew_seconds", "expected_source"),
        (
            pytest.param(
                AC_MAIN_SAMPLE_MAX_SKEW_SECONDS,
                "pv_battery_eps_balance_positive",
                id="maximum-skew-is-inclusive",
            ),
            pytest.param(
                AC_MAIN_SAMPLE_MAX_SKEW_SECONDS + 0.001,
                "pv_battery_fallback_negative",
                id="eps-just-outside-maximum-skew-is-excluded",
            ),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_eps_skew_boundary_changes_the_selected_cohort(
        self,
        skew_seconds,
        expected_source,
    ):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=363.0,
                pv1=0.0,
                pv2=0.0,
                battery=234.0,
                eps=-632.0,
                skew_seconds={"eps_load_power_mqtt": skew_seconds},
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        # EPS remains fresh enough to publish, but only a contemporaneous
        # value may participate in AC-main direction reconstruction.
        assert merged["eps_load_power_mqtt"] == -632.0
        assert merged["ac_main_power_mqtt"] == (
            363.0
            if skew_seconds <= AC_MAIN_SAMPLE_MAX_SKEW_SECONDS
            else -363.0
        )
        metadata = merged["mqtt_live"]["ac_main_power_mqtt"]
        assert metadata["sign_source"] == expected_source
        assert metadata["sample_skew_seconds"]["eps"] == skew_seconds
        assert (
            metadata["sample_max_skew_seconds"]
            == AC_MAIN_SAMPLE_MAX_SKEW_SECONDS
        )

    @freeze_time("2026-01-01 12:00:00")
    def test_incoherent_pv_uses_contemporaneous_battery_eps_fallback(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=120.0,
                pv1=500.0,
                pv2=500.0,
                battery=-100.0,
                eps=20.0,
                skew_seconds={
                    "pv2_power_mqtt": AC_MAIN_SAMPLE_MAX_SKEW_SECONDS + 0.001
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        # The still-fresh PV pair remains available to the combined PV sensor
        # even though its skew excludes it from this sign decision.
        assert merged["pv_power_mqtt"] == 1000.0
        assert merged["ac_main_power_mqtt"] == 120.0
        assert (
            merged["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "battery_eps_minimum_balance_positive"
        )

    @freeze_time("2026-01-01 12:00:00")
    def test_incoherent_battery_leaves_ac_main_unsigned(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=-363.0,
                battery=234.0,
                skew_seconds={
                    "battery_power_mqtt": AC_MAIN_SAMPLE_MAX_SKEW_SECONDS
                    + 0.001
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["battery_power_mqtt"] == 234.0
        assert merged["ac_main_power_mqtt"] == 363.0
        assert (
            merged["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "unsigned_fallback"
        )


class TestAcMainPowerStandbyStability:
    """Keep observed negative standby consumption stable across split replies."""

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_low_power_inputs_outside_strict_skew_remain_negative(self):
        """Reproduce the observed +16 W fallback after split PCS/EMS replies."""
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=16.0,
                pv1=0.0,
                pv2=0.0,
                battery=0.0,
                eps=0.0,
                skew_seconds={
                    "battery_power_mqtt": 60.0,
                    "eps_load_power_mqtt": 60.0,
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == -16.0
        metadata = merged["mqtt_live"]["ac_main_power_mqtt"]
        assert metadata["sign_indicator"] == -1.0
        assert (
            metadata["sign_source"]
            == "internal_consumption_idle_fresh_fallback"
        )

    @freeze_time("2026-01-01 12:00:00")
    def test_split_poll_sequence_never_flips_standby_positive(self):
        """Exercise coherent, PCS-first, and completed EMS/PCS bundle states."""
        now = dt_util.utcnow()
        previous_poll = now - timedelta(seconds=60)
        live = _ac_main_live_values(
            previous_poll,
            ac_main=16.0,
            pv1=0.0,
            pv2=0.0,
            battery=0.0,
            eps=0.0,
        )
        coordinator = _make_coordinator(live)

        coherent_previous = coordinator._apply_mqtt_live_values_to_bundle(
            SYSTEM_ID,
            {},
        )

        # A new PCS reply updates PV and AC Main before the corresponding EMS
        # reply. The retained EMS samples are fresh but intentionally outside
        # the strict two-second dynamic-balance cohort.
        for key in (
            "ac_main_power_mqtt_at",
            "pv1_power_mqtt_at",
            "pv2_power_mqtt_at",
        ):
            live[key] = now
        pcs_first = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        for key in ("battery_power_mqtt_at", "eps_load_power_mqtt_at"):
            live[key] = now
        completed_poll = coordinator._apply_mqtt_live_values_to_bundle(
            SYSTEM_ID,
            {},
        )

        assert coherent_previous["ac_main_power_mqtt"] == -16.0
        assert pcs_first["ac_main_power_mqtt"] == -16.0
        assert completed_poll["ac_main_power_mqtt"] == -16.0
        assert (
            pcs_first["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "internal_consumption_idle_fresh_fallback"
        )

    @freeze_time("2026-01-01 12:00:00")
    def test_fresh_low_power_fallback_threshold_is_inclusive(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=AC_MAIN_IDLE_POWER_THRESHOLD_W,
                pv1=AC_MAIN_IDLE_POWER_THRESHOLD_W / 2,
                pv2=AC_MAIN_IDLE_POWER_THRESHOLD_W / 2,
                battery=AC_MAIN_IDLE_POWER_THRESHOLD_W,
                eps=AC_MAIN_IDLE_POWER_THRESHOLD_W,
                skew_seconds={
                    "pv1_power_mqtt": 60.0,
                    "pv2_power_mqtt": 60.0,
                    "battery_power_mqtt": 60.0,
                    "eps_load_power_mqtt": 60.0,
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == -AC_MAIN_IDLE_POWER_THRESHOLD_W
        assert (
            merged["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "internal_consumption_idle_fresh_fallback"
        )

    @pytest.mark.parametrize(
        "overrides",
        (
            pytest.param({"ac_main": 50.01}, id="ac-main-above-threshold"),
            pytest.param({"pv1": 25.01, "pv2": 25.0}, id="combined-pv-above-threshold"),
            pytest.param({"battery": 50.01}, id="battery-above-threshold"),
            pytest.param({"eps": 50.01}, id="eps-above-threshold"),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_low_power_fallback_does_not_cross_any_threshold(self, overrides):
        now = dt_util.utcnow()
        inputs = {
            "ac_main": AC_MAIN_IDLE_POWER_THRESHOLD_W,
            "pv1": AC_MAIN_IDLE_POWER_THRESHOLD_W / 2,
            "pv2": AC_MAIN_IDLE_POWER_THRESHOLD_W / 2,
            "battery": AC_MAIN_IDLE_POWER_THRESHOLD_W,
            "eps": AC_MAIN_IDLE_POWER_THRESHOLD_W,
        }
        inputs.update(overrides)
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                **inputs,
                skew_seconds={
                    "pv1_power_mqtt": 60.0,
                    "pv2_power_mqtt": 60.0,
                    "battery_power_mqtt": 60.0,
                    "eps_load_power_mqtt": 60.0,
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == abs(inputs["ac_main"])
        assert (
            merged["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "unsigned_fallback"
        )

    @pytest.mark.parametrize(
        "stale_key",
        (
            "pv1_power_mqtt",
            "pv2_power_mqtt",
            "battery_power_mqtt",
            "eps_load_power_mqtt",
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_stale_input_cannot_establish_standby_fallback(self, stale_key):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            _ac_main_live_values(
                now,
                ac_main=16.0,
                pv1=0.0,
                pv2=0.0,
                battery=0.0,
                eps=0.0,
                stale=frozenset({stale_key}),
                skew_seconds={
                    "pv1_power_mqtt": 60.0,
                    "pv2_power_mqtt": 60.0,
                    "battery_power_mqtt": 60.0,
                    "eps_load_power_mqtt": 60.0,
                },
            )
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == 16.0
        assert (
            merged["mqtt_live"]["ac_main_power_mqtt"]["sign_source"]
            == "unsigned_fallback"
        )


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
