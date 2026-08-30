"""Tests for JackeryHomeCloudCoordinator._ingest_mqtt_live_values
(backlog discussion #6, item 17):

  "Multi-system safeguard" section: secondary MQTT payloads are ignored.
  "Parser behavior" section: raw schedule values, missing leading zeros.
  "Energy pipeline" section: AC-output totals pass from one MQTT report
  through ingestion and bundle merging to their final sensor values.

`_ingest_mqtt_live_values` only reads `self.data` (for
`_resolve_system_id_from_gw_sn`) and `self.mqtt_system` (via
`is_mqtt_system`), and writes `self._mqtt_live_values` and
`self._mqtt_update_events` (via `_notify_mqtt_update` - see discussion #6,
item 6, "Event-driven write verification") - no `hass` needed. Built via
`object.__new__`, same pattern as test_coordinator_bundle_merge.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
import pytest

from custom_components.jackery_home_cloud.const import (
    MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID,
    MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID,
    MQTT_EMS_BATTERY_POWER_METER_ID,
    MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID,
    MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID,
    MQTT_EMS_CHARGE_WINDOW_METER_IDS,
    MQTT_EMS_DISCHARGE_WINDOW_METER_IDS,
    MQTT_EMS_EPS_LOAD_POWER_METER_ID,
    MQTT_EMS_ON_GRID_POWER_METER_ID,
    MQTT_EMS_OTHER_LOAD_POWER_L1_METER_ID,
    MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID,
    MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
    MQTT_PCS_ACTIVE_POWER_METER_ID,
    MQTT_PCS_APPARENT_POWER_METER_ID,
    MQTT_PCS_PV1_POWER_METER_ID,
    MQTT_PCS_PV2_POWER_METER_ID,
)
from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
    _FAST_EMS_METER_IDS,
    _FAST_PCS_METER_IDS,
    _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH,
    _TOTALS_EMS_METER_IDS,
    _validate_and_pad_schedule_raw,
)
from custom_components.jackery_home_cloud.sensor import (
    SYSTEM_SENSOR_DESCRIPTIONS,
    JackeryMetricSensor,
    _schedule_windows,
)

PRIMARY_SYSTEM = "sys-primary"
PRIMARY_SERIAL = "SN-PRIMARY"
SECONDARY_SYSTEM = "sys-secondary"
SECONDARY_SERIAL = "SN-SECONDARY"
AC_OUTPUT_ENERGY_METERS = (
    ("ac_output_energy_in", MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID),
    ("ac_output_energy_out", MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID),
)
def _make_coordinator() -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system = JackeryMqttSystem(system_id=PRIMARY_SYSTEM, device_serial=PRIMARY_SERIAL)
    coordinator.data = {
        "systems": {
            PRIMARY_SYSTEM: {"system": {"systemNo": PRIMARY_SERIAL}},
            SECONDARY_SYSTEM: {"system": {"systemNo": SECONDARY_SERIAL}},
        }
    }
    coordinator._mqtt_live_values = {}
    coordinator._mqtt_update_events = {}
    return coordinator


def _data_report(gw_sn: str, meter_list: list[list[str]]) -> dict:
    return {
        "payload_json": {
            "cmd": "data_report",
            "gw_sn": gw_sn,
            "info": {
                "dev_list": [
                    {"dev_sn": f"ems_{gw_sn}", "meter_list": meter_list}
                ]
            },
        }
    }


def _power_data_report(gw_sn: str) -> dict:
    """Build one report containing every AC-main sign input."""
    return {
        "payload_json": {
            "cmd": "data_report",
            "gw_sn": gw_sn,
            "info": {
                "dev_list": [
                    {
                        "dev_sn": f"ems_{gw_sn}",
                        "meter_list": [
                            [MQTT_EMS_BATTERY_POWER_METER_ID, "-234"],
                            [MQTT_EMS_EPS_LOAD_POWER_METER_ID, "-632"],
                        ],
                    },
                    {
                        "dev_sn": f"pcs_{gw_sn}",
                        "meter_list": [
                            [MQTT_PCS_PV1_POWER_METER_ID, "0"],
                            [MQTT_PCS_PV2_POWER_METER_ID, "0"],
                            [MQTT_PCS_ACTIVE_POWER_L1_METER_ID, "363"],
                        ],
                    },
                ]
            },
        }
    }


def _power_validation_data_report(gw_sn: str) -> dict:
    """Build the combined validated/experimental MQTT trace response."""
    return {
        "payload_json": {
            "cmd": "data_get",
            "gw_sn": gw_sn,
            "info": {
                "dev_list": [
                    {
                        "dev_sn": f"ems_{gw_sn}",
                        "meter_list": [
                            [MQTT_EMS_OTHER_LOAD_POWER_L1_METER_ID, "140"],
                            [MQTT_EMS_ON_GRID_POWER_METER_ID, "0.000000"],
                        ],
                    },
                    {
                        "dev_sn": f"pcs_{gw_sn}",
                        "meter_list": [
                            [MQTT_PCS_ACTIVE_POWER_L1_METER_ID, "-8.000000"],
                            [MQTT_PCS_APPARENT_POWER_METER_ID, "4.000000"],
                            [MQTT_PCS_ACTIVE_POWER_METER_ID, "6.000000"],
                        ],
                    },
                ],
                "result": "0",
                "reason": "ok",
            },
        }
    }


def test_ac_output_energy_meters_are_in_slow_totals_poll_group():
    assert MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID in _TOTALS_EMS_METER_IDS
    assert MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID in _TOTALS_EMS_METER_IDS


class TestFastPowerPolling:
    def test_meter_ids_are_in_the_correct_fast_device_groups(self):
        assert {
            MQTT_EMS_OTHER_LOAD_POWER_L1_METER_ID,
            MQTT_EMS_ON_GRID_POWER_METER_ID,
        } <= set(_FAST_EMS_METER_IDS)
        assert {
            MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
            MQTT_PCS_APPARENT_POWER_METER_ID,
            MQTT_PCS_ACTIVE_POWER_METER_ID,
        } <= set(_FAST_PCS_METER_IDS)
        assert "50416641" not in _FAST_PCS_METER_IDS

    async def test_fast_request_places_all_meters_under_the_correct_devices(self):
        coordinator = _make_coordinator()
        mqtt_client = SimpleNamespace(async_publish_json=AsyncMock())
        coordinator.config_entry = SimpleNamespace(
            runtime_data=SimpleNamespace(mqtt_client=mqtt_client)
        )

        await coordinator.async_request_fast_live_meter_values()

        mqtt_client.async_publish_json.assert_awaited_once()
        _, payload = mqtt_client.async_publish_json.await_args.args
        assert mqtt_client.async_publish_json.await_args.kwargs == {"qos": 1}
        meter_lists = {
            device["dev_sn"]: device["meter_list"]
            for device in payload["info"]["dev_list"]
        }
        assert meter_lists[f"ems_{PRIMARY_SERIAL}"] == list(_FAST_EMS_METER_IDS)
        assert meter_lists[f"pcs_{PRIMARY_SERIAL}"] == list(_FAST_PCS_METER_IDS)


class TestValidatedPcsActivePowerL1Pipeline:
    @pytest.mark.parametrize("raw_value", ("1403", "-1403", "5", "-5", "0"))
    @freeze_time("2026-01-01 12:00:00")
    def test_l1_is_ingested_unchanged_without_duplicate_ac_main_cache(
        self, raw_value
    ):
        coordinator = _make_coordinator()
        coordinator._ingest_mqtt_live_values(
            {
                "payload_json": {
                    "cmd": "data_get",
                    "gw_sn": PRIMARY_SERIAL,
                    "info": {
                        "dev_list": [
                            {
                                "dev_sn": f"pcs_{PRIMARY_SERIAL}",
                                "meter_list": [
                                    [MQTT_PCS_ACTIVE_POWER_L1_METER_ID, raw_value]
                                ],
                            }
                        ]
                    },
                }
            }
        )

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["pcs_active_power_l1_mqtt"] == float(raw_value)
        assert live["pcs_active_power_l1_mqtt_at"] == datetime(
            2026, 1, 1, 12, 0, tzinfo=UTC
        )
        assert "ac_main_power_mqtt" not in live


class TestPowerValidationPipeline:
    @freeze_time("2026-01-01 12:00:00")
    def test_trace_response_reaches_validated_and_experimental_raw_sensors(self):
        """Preserve the observed values, especially their differing signs."""
        coordinator = _make_coordinator()
        coordinator._ingest_mqtt_live_values(
            _power_validation_data_report(PRIMARY_SERIAL)
        )

        expected_values = {
            "pcs_active_power_l1_mqtt": -8.0,
            "pcs_apparent_power_mqtt": 4.0,
            "pcs_active_power_mqtt": 6.0,
            "ems_other_load_power_l1_mqtt": 140.0,
            "ems_on_grid_power_mqtt": 0.0,
        }
        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert {key: live[key] for key in expected_values} == expected_values
        assert {
            live[f"{key}_at"] for key in expected_values
        } == {datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}

        source_bundle = coordinator.data["systems"][PRIMARY_SYSTEM]
        merged = coordinator._apply_mqtt_live_values_to_bundle(
            PRIMARY_SYSTEM,
            source_bundle,
        )
        coordinator.data["systems"][PRIMARY_SYSTEM] = merged
        assert merged["ac_main_power_mqtt"] == -8.0
        assert merged["mqtt_live"]["ac_main_power_mqtt"] == {
            "value": -8.0,
            "source": "mqtt",
            "meter_id": MQTT_PCS_ACTIVE_POWER_L1_METER_ID,
        }

        descriptions = {
            description.key: description
            for description in SYSTEM_SENSOR_DESCRIPTIONS
        }
        ac_main_sensor = JackeryMetricSensor(
            coordinator=coordinator,
            system_id=PRIMARY_SYSTEM,
            description=descriptions["ac_main_power"],
        )
        assert ac_main_sensor.native_value == -8.0

        sensor_to_bundle_key = {
            "pcs_active_power_l1": "pcs_active_power_l1_mqtt",
            "pcs_apparent_power": "pcs_apparent_power_mqtt",
            "pcs_active_power": "pcs_active_power_mqtt",
            "ems_other_load_power_l1": "ems_other_load_power_l1_mqtt",
            "ems_on_grid_power": "ems_on_grid_power_mqtt",
        }
        for sensor_key, bundle_key in sensor_to_bundle_key.items():
            expected_value = expected_values[bundle_key]
            assert merged[bundle_key] == expected_value
            assert merged["mqtt_live"][bundle_key] == {
                "value": expected_value,
                "source": "mqtt",
            }
            sensor = JackeryMetricSensor(
                coordinator=coordinator,
                system_id=PRIMARY_SYSTEM,
                description=descriptions[sensor_key],
            )
            assert sensor.native_value == expected_value


class TestPvInputPowerPipeline:
    """Exercise both physical PV inputs from MQTT payload to HA sensors."""

    @freeze_time("2026-01-01 12:00:00")
    def test_single_pcs_report_reaches_pv1_pv2_and_aggregate_sensors(self):
        coordinator = _make_coordinator()
        message = {
            "payload_json": {
                "cmd": "data_get",
                "gw_sn": PRIMARY_SERIAL,
                "info": {
                    "dev_list": [
                        {
                            "dev_sn": f"pcs_{PRIMARY_SERIAL}",
                            "meter_list": [
                                [MQTT_PCS_PV1_POWER_METER_ID, "321"],
                                [MQTT_PCS_PV2_POWER_METER_ID, "179"],
                            ],
                        }
                    ],
                    "result": "0",
                    "reason": "ok",
                },
            }
        }

        coordinator._ingest_mqtt_live_values(message)
        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["pv1_power_mqtt"] == 321.0
        assert live["pv2_power_mqtt"] == 179.0

        source_bundle = coordinator.data["systems"][PRIMARY_SYSTEM]
        merged = coordinator._apply_mqtt_live_values_to_bundle(
            PRIMARY_SYSTEM,
            source_bundle,
        )
        coordinator.data["systems"][PRIMARY_SYSTEM] = merged

        expected_values = {
            "pv1_power": 321.0,
            "pv2_power": 179.0,
            "pv_power": 500.0,
        }
        descriptions = {
            description.key: description
            for description in SYSTEM_SENSOR_DESCRIPTIONS
        }
        for sensor_key, expected_value in expected_values.items():
            sensor = JackeryMetricSensor(
                coordinator=coordinator,
                system_id=PRIMARY_SYSTEM,
                description=descriptions[sensor_key],
            )
            assert sensor.native_value == expected_value


class TestMqttReportTimestampCoherence:
    def test_all_values_from_one_report_share_one_reception_timestamp(self):
        """Clock movement while parsing must not split one observation batch.

        The deliberately advancing clock would assign different timestamps if
        ingestion called ``utcnow`` once per meter. A single report must remain
        coherent so its values can safely contribute to AC-main sign inference.
        """
        coordinator = _make_coordinator()
        message = _power_data_report(PRIMARY_SERIAL)
        first_instant = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        instants = iter(
            first_instant + timedelta(seconds=index) for index in range(50)
        )

        with patch(
            "custom_components.jackery_home_cloud.coordinator.dt_util.utcnow",
            side_effect=lambda: next(instants),
        ):
            coordinator._ingest_mqtt_live_values(message)

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        timestamps = {
            live[f"{key}_at"]
            for key in (
                "battery_power_mqtt",
                "eps_load_power_mqtt",
                "pv1_power_mqtt",
                "pv2_power_mqtt",
                "pcs_active_power_l1_mqtt",
            )
        }
        assert timestamps == {first_instant}


class TestSecondarySystemIgnored:
    def test_primary_system_payload_is_ingested(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {}).get("charge_window_0") == "06150715"

    def test_secondary_system_payload_is_ignored(self):
        coordinator = _make_coordinator()
        message = _data_report(SECONDARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert SECONDARY_SYSTEM not in coordinator._mqtt_live_values

    def test_unknown_gw_sn_is_ignored(self):
        coordinator = _make_coordinator()
        message = _data_report("SN-UNKNOWN", [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values == {}

    def test_no_mqtt_system_resolved_yet_ignores_everything(self):
        coordinator = _make_coordinator()
        coordinator.mqtt_system = None
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values == {}


class TestScheduleRawValueZeroPadding:
    def test_seven_digit_raw_value_is_zero_padded_to_eight(self):
        """Real device case documented in CONTRIBUTING.md: meter_id
        "23146497" reporting raw "6150715" (7 digits, missing leading
        zero) must be stored as "06150715".
        """
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["charge_window_0"] == "06150715"

    def test_eight_digit_raw_value_is_unchanged(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "06150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["charge_window_0"] == "06150715"

    def test_empty_slot_zero_is_not_zero_padded(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "0"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["charge_window_0"] == "0"

    def test_charge_and_discharge_windows_are_independent(self):
        coordinator = _make_coordinator()
        message = _data_report(
            PRIMARY_SERIAL,
            [
                [MQTT_EMS_CHARGE_WINDOW_METER_IDS[2], "615"],
                [MQTT_EMS_DISCHARGE_WINDOW_METER_IDS[2], "1830"],
            ],
        )

        coordinator._ingest_mqtt_live_values(message)

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["charge_window_2"] == "00000615"
        assert live["discharge_window_2"] == "00001830"

    def test_timestamp_is_recorded_alongside_the_value(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["charge_window_0_at"] is not None


class TestScheduleWindowsDisplay:
    """sensor.py's _schedule_windows is the "consumer" half of the same
    validated raw values produced by _ingest_mqtt_live_values above. It no
    longer re-validates length/content itself - coordinator.py's
    _validate_and_pad_schedule_raw() (see TestValidateAndPadScheduleRaw and
    TestScheduleRawValueValidation below) is the single source of truth for
    that, applied at ingestion time.
    """

    def test_eight_digit_raw_value_is_sliced_into_slot_dict(self):
        bundle = {"charge_window_0": "06150715"}
        assert _schedule_windows(bundle, "charge_window_") == [
            {"slot": 0, "start": "06:15", "end": "07:15"}
        ]

    def test_zero_slot_is_omitted(self):
        bundle = {"charge_window_0": "0", "charge_window_1": "08000900"}
        assert _schedule_windows(bundle, "charge_window_") == [
            {"slot": 1, "start": "08:00", "end": "09:00"}
        ]

    def test_missing_slots_are_skipped_not_errored(self):
        bundle = {"charge_window_3": "10001100"}
        assert _schedule_windows(bundle, "charge_window_") == [
            {"slot": 3, "start": "10:00", "end": "11:00"}
        ]

    def test_charge_and_discharge_prefixes_are_independent(self):
        bundle = {"charge_window_0": "06000700", "discharge_window_0": "18001900"}
        assert _schedule_windows(bundle, "charge_window_") == [
            {"slot": 0, "start": "06:00", "end": "07:00"}
        ]
        assert _schedule_windows(bundle, "discharge_window_") == [
            {"slot": 0, "start": "18:00", "end": "19:00"}
        ]


class TestValidateAndPadScheduleRaw:
    """Pure boundary-case tests for coordinator.py's schedule raw-value
    validator, independent of the MQTT ingestion plumbing."""

    def test_empty_slot_sentinel_passes_unchanged(self):
        assert _validate_and_pad_schedule_raw("0") == "0"

    def test_short_value_is_zero_padded(self):
        assert _validate_and_pad_schedule_raw("6150715") == "06150715"

    def test_valid_boundaries_are_accepted(self):
        assert _validate_and_pad_schedule_raw("00000001") == "00000001"
        assert _validate_and_pad_schedule_raw("23002359") == "23002359"

    def test_non_digit_value_is_rejected(self):
        assert _validate_and_pad_schedule_raw("6a50715") is None

    def test_too_long_value_is_rejected(self):
        assert _validate_and_pad_schedule_raw("061507150") is None

    def test_start_hour_out_of_range_is_rejected(self):
        assert _validate_and_pad_schedule_raw("25150715") is None

    def test_end_hour_out_of_range_is_rejected(self):
        assert _validate_and_pad_schedule_raw("06152400") is None

    def test_start_minute_out_of_range_is_rejected(self):
        assert _validate_and_pad_schedule_raw("06990715") is None

    def test_end_minute_out_of_range_is_rejected(self):
        assert _validate_and_pad_schedule_raw("06150799") is None

    def test_overnight_spanning_window_is_rejected(self):
        assert _validate_and_pad_schedule_raw("22000600") is None

    def test_zero_duration_window_is_rejected(self):
        assert _validate_and_pad_schedule_raw("06000600") is None


class TestScheduleRawValueValidation:
    """Ingestion-level validation (coordinator.py): an invalid raw schedule
    value is logged and ignored, never accepted with a garbage/normalized
    value, and never overwrites a previously cached valid value for the
    same slot."""

    def test_non_digit_raw_value_is_rejected_and_logged(self, caplog):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "6a50715"]])

        with caplog.at_level(logging.WARNING):
            coordinator._ingest_mqtt_live_values(message)

        assert "charge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})
        assert "invalid" in caplog.text.lower()

    def test_too_long_raw_value_is_rejected_and_logged(self, caplog):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "061507150"]])

        with caplog.at_level(logging.WARNING):
            coordinator._ingest_mqtt_live_values(message)

        assert "charge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})
        assert caplog.records

    def test_hour_out_of_range_is_rejected(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "25150715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert "charge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})

    def test_minute_out_of_range_is_rejected(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "06990715"]])

        coordinator._ingest_mqtt_live_values(message)

        assert "charge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})

    def test_overnight_spanning_raw_value_is_rejected(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "22000600"]])

        coordinator._ingest_mqtt_live_values(message)

        assert "charge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})

    def test_discharge_window_validation_is_independent_and_logged(self, caplog):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_DISCHARGE_WINDOW_METER_IDS[0], "22000600"]])

        with caplog.at_level(logging.WARNING):
            coordinator._ingest_mqtt_live_values(message)

        assert "discharge_window_0" not in coordinator._mqtt_live_values.get(PRIMARY_SYSTEM, {})
        assert "discharge window" in caplog.text.lower()

    def test_previous_value_is_preserved_when_new_value_is_invalid(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {
            "charge_window_0": "06000700",
            "charge_window_0_at": "sentinel-timestamp",
        }
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "not-digits"]])

        coordinator._ingest_mqtt_live_values(message)

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["charge_window_0"] == "06000700"
        assert live["charge_window_0_at"] == "sentinel-timestamp"

    def test_empty_slot_sentinel_still_ingested(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_CHARGE_WINDOW_METER_IDS[0], "0"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["charge_window_0"] == "0"


class TestCumulativeEnergyTotalsRejectDecreases:
    """Cumulative energy counters (battery charged/discharged total,
    PV1/PV2 energy total) reported over MQTT are guarded against
    spurious decreases (e.g. a device reboot/counter glitch) via a small
    tolerance - a decrease larger than the tolerance is ignored and the
    previously accepted value is kept, so history/statistics never see a
    cumulative total go backwards.
    """

    def test_increasing_value_is_accepted(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"battery_energy_charged_total": 10.0}
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID, "10.5"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["battery_energy_charged_total"] == 10.5

    def test_decrease_within_tolerance_is_still_accepted(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"battery_energy_charged_total": 10.0}
        # tolerance is 0.01 kWh for these totals - a 0.005 dip is noise, not a reset.
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID, "9.995"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["battery_energy_charged_total"] == 9.995

    def test_decrease_beyond_tolerance_is_rejected_and_previous_value_kept(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"battery_energy_charged_total": 10.0}
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID, "3.0"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["battery_energy_charged_total"] == 10.0

    def test_first_ever_value_with_no_previous_is_always_accepted(self):
        coordinator = _make_coordinator()
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID, "0.5"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["battery_energy_discharged_total"] == 0.5

    def test_ac_output_energy_in_is_ingested(self):
        coordinator = _make_coordinator()
        message = _data_report(
            PRIMARY_SERIAL,
            [[MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID, "4.125"]],
        )

        coordinator._ingest_mqtt_live_values(message)

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["ac_output_energy_in"] == 4.125
        assert live["ac_output_energy_in_source"] == "mqtt"

    def test_ac_output_energy_out_is_ingested(self):
        coordinator = _make_coordinator()
        message = _data_report(
            PRIMARY_SERIAL,
            [[MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID, "7.875"]],
        )

        coordinator._ingest_mqtt_live_values(message)

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["ac_output_energy_out"] == 7.875
        assert live["ac_output_energy_out_source"] == "mqtt"

    def test_decreasing_ac_output_energy_in_value_is_rejected(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"ac_output_energy_in": 10.0}
        message = _data_report(
            PRIMARY_SERIAL,
            [[MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID, "3.0"]],
        )

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["ac_output_energy_in"] == 10.0

    def test_decreasing_ac_output_energy_out_value_is_rejected(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"ac_output_energy_out": 10.0}
        message = _data_report(
            PRIMARY_SERIAL,
            [[MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID, "3.0"]],
        )

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["ac_output_energy_out"] == 10.0

    def test_pv1_energy_total_has_its_own_independent_guard(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"pv1_energy_total": 5.0}
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID, "1.0"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["pv1_energy_total"] == 5.0


class TestAcOutputEnergyToleranceBoundaries:
    """Pin the monotonicity guard at and immediately beyond its boundary."""

    @pytest.mark.parametrize(("key", "meter_id"), AC_OUTPUT_ENERGY_METERS)
    def test_decrease_exactly_at_tolerance_is_accepted(self, key, meter_id):
        coordinator = _make_coordinator()
        previous_value = 10.0
        previous_timestamp = "previous-timestamp"
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {
            key: previous_value,
            f"{key}_at": previous_timestamp,
        }
        incoming_value = (
            previous_value - _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH
        )

        coordinator._ingest_mqtt_live_values(
            _data_report(PRIMARY_SERIAL, [[meter_id, str(incoming_value)]])
        )

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live[key] == pytest.approx(incoming_value)
        assert live[f"{key}_at"] != previous_timestamp
        assert live[f"{key}_source"] == "mqtt"

    @pytest.mark.parametrize(("key", "meter_id"), AC_OUTPUT_ENERGY_METERS)
    def test_decrease_just_beyond_tolerance_preserves_value_and_timestamp(
        self,
        key,
        meter_id,
    ):
        coordinator = _make_coordinator()
        previous_value = 10.0
        previous_timestamp = "previous-timestamp"
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {
            key: previous_value,
            f"{key}_at": previous_timestamp,
            f"{key}_source": "previous-source",
        }
        incoming_value = (
            previous_value
            - _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH
            - 0.001
        )

        coordinator._ingest_mqtt_live_values(
            _data_report(PRIMARY_SERIAL, [[meter_id, str(incoming_value)]])
        )

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live[key] == previous_value
        assert live[f"{key}_at"] == previous_timestamp
        assert live[f"{key}_source"] == "previous-source"

    @pytest.mark.parametrize(("key", "meter_id"), AC_OUTPUT_ENERGY_METERS)
    def test_increasing_value_is_accepted_after_rejected_decrease(self, key, meter_id):
        coordinator = _make_coordinator()
        previous_value = 10.0
        previous_timestamp = "previous-timestamp"
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {
            key: previous_value,
            f"{key}_at": previous_timestamp,
        }

        rejected_value = (
            previous_value
            - _MQTT_TOTAL_ALLOWED_DECREASE_TOLERANCE_KWH
            - 0.001
        )
        coordinator._ingest_mqtt_live_values(
            _data_report(PRIMARY_SERIAL, [[meter_id, str(rejected_value)]])
        )
        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM][key] == previous_value

        coordinator._ingest_mqtt_live_values(
            _data_report(PRIMARY_SERIAL, [[meter_id, "10.5"]])
        )

        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live[key] == 10.5
        assert live[f"{key}_at"] != previous_timestamp
        assert live[f"{key}_source"] == "mqtt"


class TestAcOutputEnergyPipeline:
    """Exercise the complete MQTT-report-to-sensor data path.

    The focused tests above intentionally isolate ingestion. This regression
    test connects all production stages so a mismatch between meter IDs,
    live-cache keys, bundle keys, metadata, or sensor value callbacks cannot
    pass unnoticed.
    """

    @freeze_time("2026-01-01 12:00:00")
    def test_single_report_reaches_both_energy_sensor_values(self):
        coordinator = _make_coordinator()
        message = _data_report(
            PRIMARY_SERIAL,
            [
                [MQTT_EMS_AC_OUTPUT_ENERGY_IN_METER_ID, "4.125"],
                [MQTT_EMS_AC_OUTPUT_ENERGY_OUT_METER_ID, "7.875"],
            ],
        )

        # Stage 1: parse the actual MQTT report and populate the live cache.
        coordinator._ingest_mqtt_live_values(message)
        live = coordinator._mqtt_live_values[PRIMARY_SYSTEM]
        assert live["ac_output_energy_in"] == 4.125
        assert live["ac_output_energy_out"] == 7.875

        # Stage 2: merge fresh cached values into the coordinator bundle.
        source_bundle = coordinator.data["systems"][PRIMARY_SYSTEM]
        merged = coordinator._apply_mqtt_live_values_to_bundle(
            PRIMARY_SYSTEM,
            source_bundle,
        )
        coordinator.data["systems"][PRIMARY_SYSTEM] = merged

        expected_values = {
            "ac_output_energy_in": 4.125,
            "ac_output_energy_out": 7.875,
        }
        for key, expected_value in expected_values.items():
            assert merged[key] == expected_value
            assert merged["mqtt_live"][key] == {
                "value": expected_value,
                "source": "mqtt",
                "age_seconds": 0.0,
            }

        # Stage 3: expose the merged values through the shipped sensor
        # descriptions and JackeryMetricSensor.native_value property.
        descriptions = {
            description.key: description
            for description in SYSTEM_SENSOR_DESCRIPTIONS
        }
        for key, expected_value in expected_values.items():
            sensor = JackeryMetricSensor(
                coordinator=coordinator,
                system_id=PRIMARY_SYSTEM,
                description=descriptions[key],
            )
            assert sensor.native_value == expected_value
