"""Tests for JackeryHomeCloudCoordinator._ingest_mqtt_live_values
(backlog discussion #6, item 17):

  "Multi-system safeguard" section: secondary MQTT payloads are ignored.
  "Parser behavior" section: raw schedule values, missing leading zeros.

`_ingest_mqtt_live_values` only reads `self.data` (for
`_resolve_system_id_from_gw_sn`) and `self.mqtt_system` (via
`is_mqtt_system`), and writes `self._mqtt_live_values` and
`self._mqtt_update_events` (via `_notify_mqtt_update` - see discussion #6,
item 6, "Event-driven write verification") - no `hass` needed. Built via
`object.__new__`, same pattern as test_coordinator_bundle_merge.py.
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.const import (
    MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID,
    MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID,
    MQTT_EMS_CHARGE_WINDOW_METER_IDS,
    MQTT_EMS_DISCHARGE_WINDOW_METER_IDS,
    MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID,
)
from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)
from custom_components.jackery_home_cloud.sensor import _schedule_windows

PRIMARY_SYSTEM = "sys-primary"
PRIMARY_SERIAL = "SN-PRIMARY"
SECONDARY_SYSTEM = "sys-secondary"
SECONDARY_SERIAL = "SN-SECONDARY"


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
    zero-padded raw values produced by _ingest_mqtt_live_values above."""

    def test_eight_digit_raw_value_is_sliced_into_hh_mm_window(self):
        bundle = {"charge_window_0": "06150715"}
        assert _schedule_windows(bundle, "charge_window_") == ["06:15-07:15"]

    def test_wrong_length_raw_value_is_silently_dropped(self):
        bundle = {"charge_window_0": "615715"}  # 6 digits, never zero-padded correctly
        assert _schedule_windows(bundle, "charge_window_") == []

    def test_zero_slot_is_omitted(self):
        bundle = {"charge_window_0": "0", "charge_window_1": "08000900"}
        assert _schedule_windows(bundle, "charge_window_") == ["08:00-09:00"]

    def test_missing_slots_are_skipped_not_errored(self):
        bundle = {"charge_window_3": "10001100"}
        assert _schedule_windows(bundle, "charge_window_") == ["10:00-11:00"]

    def test_charge_and_discharge_prefixes_are_independent(self):
        bundle = {"charge_window_0": "06000700", "discharge_window_0": "18001900"}
        assert _schedule_windows(bundle, "charge_window_") == ["06:00-07:00"]
        assert _schedule_windows(bundle, "discharge_window_") == ["18:00-19:00"]


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

    def test_pv1_energy_total_has_its_own_independent_guard(self):
        coordinator = _make_coordinator()
        coordinator._mqtt_live_values[PRIMARY_SYSTEM] = {"pv1_energy_total": 5.0}
        message = _data_report(PRIMARY_SERIAL, [[MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID, "1.0"]])

        coordinator._ingest_mqtt_live_values(message)

        assert coordinator._mqtt_live_values[PRIMARY_SYSTEM]["pv1_energy_total"] == 5.0
