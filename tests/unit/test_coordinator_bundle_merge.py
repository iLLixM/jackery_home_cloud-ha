"""Tests for JackeryHomeCloudCoordinator._apply_mqtt_live_values_to_bundle
(Family C: coordinator ingestion/merge logic).

This targets MQTT bundle merging, including the direct PCS active-power-L1
source for AC main power and freshness gating for every sampled input.

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
    MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS,
    MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
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
    "pv1_power_mqtt",
    "pv2_power_mqtt",
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


class TestNoLiveValues:
    def test_missing_system_returns_bundle_unchanged(self):
        coordinator = _make_coordinator({})
        coordinator._mqtt_live_values = {}
        bundle = {"some_key": "value"}
        merged = coordinator._apply_mqtt_live_values_to_bundle("unknown_system", bundle)
        assert merged == bundle
        assert merged is not bundle  # must return a copy, not the original


class TestExistingMqttBundleValuesRemainMerged:
    """Guard unrelated MQTT bundle fields while AC-main sourcing is refactored.

    Both fields are merged close to the AC-main source alias.
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


class TestAcMainPowerDirectL1Source:
    """Use fresh PCS active-power L1 unchanged as AC-main MQTT power."""

    @pytest.mark.parametrize(
        "l1_value",
        (1500.0, -1500.0, 20.0, -20.0, 5.0, -5.0, 1.0, -1.0, 0.0),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_preserves_signed_l1_value_without_deadband(self, l1_value):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                "pcs_active_power_l1_mqtt": l1_value,
                "pcs_active_power_l1_mqtt_at": now,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["pcs_active_power_l1_mqtt"] == l1_value
        assert merged["ac_main_power_mqtt"] == l1_value
        assert merged["mqtt_live"]["pcs_active_power_l1_mqtt"] == {
            "value": l1_value,
            "source": "mqtt",
        }
        assert merged["mqtt_live"]["ac_main_power_mqtt"] == {
            "value": l1_value,
            "source": "mqtt",
            "meter_id": MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
        }

    @pytest.mark.parametrize("l1_value", (25.0, -25.0))
    @freeze_time("2026-01-01 12:00:00")
    def test_other_power_inputs_cannot_change_l1_value(self, l1_value):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                "pcs_active_power_l1_mqtt": l1_value,
                "pcs_active_power_l1_mqtt_at": now,
                "pv1_power_mqtt": 1500.0,
                "pv1_power_mqtt_at": now,
                "pv2_power_mqtt": 1200.0,
                "pv2_power_mqtt_at": now,
                "battery_power_mqtt": -900.0,
                "battery_power_mqtt_at": now,
                "eps_load_power_mqtt": -700.0,
                "eps_load_power_mqtt_at": now,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["ac_main_power_mqtt"] == l1_value

    @freeze_time("2026-01-01 12:00:00")
    def test_sequential_samples_have_no_direction_memory(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator({})
        values = (25.0, 5.0, -5.0, 0.0, 2.0, -2.0)
        results = []

        for value in values:
            coordinator._mqtt_live_values[SYSTEM_ID] = {
                "pcs_active_power_l1_mqtt": value,
                "pcs_active_power_l1_mqtt_at": now,
            }
            merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})
            results.append(merged["ac_main_power_mqtt"])

        assert tuple(results) == values
        assert not hasattr(coordinator, "_ac_main_l1_direction")

    @pytest.mark.parametrize(
        ("age_seconds", "expected_present"),
        (
            pytest.param(
                MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS,
                True,
                id="inclusive-freshness-boundary",
            ),
            pytest.param(
                MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS + 0.001,
                False,
                id="immediately-stale",
            ),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_l1_freshness_controls_both_bundle_values(
        self, age_seconds, expected_present
    ):
        timestamp = dt_util.utcnow() - timedelta(seconds=age_seconds)
        coordinator = _make_coordinator(
            {
                "pcs_active_power_l1_mqtt": -123.0,
                "pcs_active_power_l1_mqtt_at": timestamp,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert ("pcs_active_power_l1_mqtt" in merged) is expected_present
        assert ("ac_main_power_mqtt" in merged) is expected_present

    @freeze_time("2026-01-01 12:00:00")
    def test_stale_l1_removes_previous_ac_main_overlay_and_metadata(self):
        stale_timestamp = dt_util.utcnow() - timedelta(
            seconds=MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS + 1
        )
        coordinator = _make_coordinator(
            {
                "pcs_active_power_l1_mqtt": 500.0,
                "pcs_active_power_l1_mqtt_at": stale_timestamp,
            }
        )
        bundle = {
            "pcs_active_power_l1_mqtt": 500.0,
            "ac_main_power_mqtt": 500.0,
            "mqtt_live": {
                "pcs_active_power_l1_mqtt": {
                    "value": 500.0,
                    "source": "mqtt",
                },
                "ac_main_power_mqtt": {
                    "value": 500.0,
                    "source": "mqtt",
                    "meter_id": MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
                },
            },
        }

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, bundle)

        assert "pcs_active_power_l1_mqtt" not in merged
        assert "ac_main_power_mqtt" not in merged
        assert "mqtt_live" not in merged

    def test_missing_l1_does_not_publish_ac_main_overlay(self):
        coordinator = _make_coordinator({})

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert "ac_main_power_mqtt" not in merged


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


class TestPvInputPowerFreshnessGating:
    """Publish PV inputs independently while keeping the aggregate strict."""

    @freeze_time("2026-01-01 12:00:00")
    def test_both_fresh_inputs_publish_components_and_aggregate(self):
        now = dt_util.utcnow()
        coordinator = _make_coordinator(
            {
                "pv1_power_mqtt": 320.0,
                "pv1_power_mqtt_at": now,
                "pv2_power_mqtt": 180.0,
                "pv2_power_mqtt_at": now,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged["pv1_power_mqtt"] == 320.0
        assert merged["pv2_power_mqtt"] == 180.0
        assert merged["pv_power_mqtt"] == 500.0
        assert merged["mqtt_live"]["pv1_power_mqtt"] == {
            "value": 320.0,
            "source": "mqtt",
        }
        assert merged["mqtt_live"]["pv2_power_mqtt"] == {
            "value": 180.0,
            "source": "mqtt",
        }
        assert merged["mqtt_live"]["pv_power_mqtt"] == {
            "value": 500.0,
            "source": "mqtt",
            "pv1_power": 320.0,
            "pv2_power": 180.0,
        }

    @pytest.mark.parametrize(
        ("fresh_key", "stale_key", "expected_value"),
        (
            pytest.param(
                "pv1_power_mqtt",
                "pv2_power_mqtt",
                320.0,
                id="pv1-fresh-pv2-stale",
            ),
            pytest.param(
                "pv2_power_mqtt",
                "pv1_power_mqtt",
                180.0,
                id="pv2-fresh-pv1-stale",
            ),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_each_input_remains_independently_available(
        self, fresh_key, stale_key, expected_value
    ):
        now = dt_util.utcnow()
        values = {
            "pv1_power_mqtt": 320.0,
            "pv1_power_mqtt_at": now,
            "pv2_power_mqtt": 180.0,
            "pv2_power_mqtt_at": now,
        }
        values[f"{stale_key}_at"] = now - timedelta(seconds=121)
        coordinator = _make_coordinator(values)

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert merged[fresh_key] == expected_value
        assert stale_key not in merged
        assert "pv_power_mqtt" not in merged

    @pytest.mark.parametrize(
        ("age_seconds", "expected_present"),
        (
            pytest.param(120, True, id="inclusive-boundary"),
            pytest.param(121, False, id="expired"),
        ),
    )
    @freeze_time("2026-01-01 12:00:00")
    def test_component_freshness_boundary(self, age_seconds, expected_present):
        timestamp = dt_util.utcnow() - timedelta(seconds=age_seconds)
        coordinator = _make_coordinator(
            {
                "pv1_power_mqtt": 320.0,
                "pv1_power_mqtt_at": timestamp,
            }
        )

        merged = coordinator._apply_mqtt_live_values_to_bundle(SYSTEM_ID, {})

        assert ("pv1_power_mqtt" in merged) is expected_present


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
