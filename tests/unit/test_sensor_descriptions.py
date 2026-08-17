"""Entity-description-level tests for sensor.py (Family F: unique_id
stability).

These import `sensor.py` directly and inspect `SYSTEM_SENSOR_DESCRIPTIONS`
as plain data - no `hass` fixture, coordinator, or config entry needed,
since `value_fn`/`unique_id_fn` are pure callables over a plain dict
bundle. This directly targets the historical regression from PR #8: a
`key` rename on a shipped entity silently changes its `unique_id`
(`f"{unique_source}_{description.key}"` in `JackeryMetricSensor.__init__`),
orphaning the old entity and losing history/automations for every
upgrading user.
"""

from __future__ import annotations

from typing import Any

from custom_components.jackery_home_cloud.diagnostics import _PROTOCOL_VALIDATION_METERS
from custom_components.jackery_home_cloud.sensor import (
    SYSTEM_SENSOR_DESCRIPTIONS,
    _mqtt_or_rest,
)

# Pinned snapshot of every shipped sensor `key`. This is the "vocabulary"
# behind `unique_id = f"{unique_source}_{key}"` for every simple
# (non-per-source) sensor. Any diff here (add/remove/rename) is a
# deliberate change that must be reviewed for unique_id/migration impact -
# do not "fix" this test by blindly re-pinning without checking whether the
# change breaks upgrades for already-shipped entities.
EXPECTED_SENSOR_KEYS = frozenset(
    {
        "total_charge_amount",
        "co2_saved",
        "grid_power",
        "ac_main_power",
        "battery_soc",
        "battery_power",
        "battery_power_bms1",
        "battery_energy_remaining",
        "pv_power",
        "eps_load_power",
        "eps_load_power_inverted",
        "other_load_power",
        "solar_energy_generated_today",
        "battery_energy_charged_today",
        "battery_energy_discharged_today",
        "battery_energy_charged_total",
        "battery_energy_discharged_total",
        "ac_output_energy_in",
        "ac_output_energy_out",
        "grid_energy_exported_today",
        "grid_energy_imported_today",
        "pv1_energy_today",
        "pv2_energy_today",
        "pv1_energy_total",
        "pv2_energy_total",
        "pv_energy_total",
        "on_grid_energy_exported_today",
        "battery_count",
        "total_battery_capacity",
        "device_connection",
        "mqtt_connection_status",
        "mqtt_message_count",
        "mqtt_last_topic",
        "mqtt_last_message_at",
    }
)

# Sensors whose `unique_id_fn` is the trivial `f"system_{system_id}"`
# regardless of bundle content - these are the entities where a `key`
# rename is *guaranteed* to change unique_id for every user (no
# source-device fallback to consider).
SIMPLE_UNIQUE_ID_KEYS = frozenset(
    {
        "total_charge_amount",
        "co2_saved",
        "ac_main_power",
        "battery_power",
        "battery_power_bms1",
        "ac_output_energy_in",
        "ac_output_energy_out",
    }
)


def test_all_sensor_keys_are_unique():
    keys = [d.key for d in SYSTEM_SENSOR_DESCRIPTIONS]
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"Duplicate sensor keys found: {duplicates}"


def test_sensor_key_snapshot_matches_pinned_set():
    keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS}
    added = keys - EXPECTED_SENSOR_KEYS
    removed = EXPECTED_SENSOR_KEYS - keys
    assert not added and not removed, (
        f"sensor.py's SYSTEM_SENSOR_DESCRIPTIONS keys changed - added={added or None}, "
        f"removed={removed or None}. If this is a deliberate rename of a "
        f"*shipped* entity, its unique_id changes and existing installs "
        f"will get an orphaned + a duplicate entity (see CONTRIBUTING.md #6). "
        f"Update EXPECTED_SENSOR_KEYS only after confirming that impact is "
        f"intended/acceptable."
    )


def test_simple_unique_id_fn_produces_system_scoped_id():
    """For keys with a trivial unique_id_fn, unique_id must be exactly
    f"system_{system_id}_{key}" regardless of bundle contents.
    """
    descriptions_by_key = {d.key: d for d in SYSTEM_SENSOR_DESCRIPTIONS}
    for key in SIMPLE_UNIQUE_ID_KEYS:
        description = descriptions_by_key[key]
        unique_source = description.unique_id_fn("sys123", {})
        assert unique_source == "system_sys123"
        unique_id = f"{unique_source}_{description.key}"
        assert unique_id == f"system_sys123_{key}"


def test_battery_power_and_bms1_are_distinct_entities():
    """Regression guard for the specific PR #8 finding: the aggregated
    EMS battery power and the BMS1-specific battery power must remain two
    separate sensor keys (and therefore two separate unique_ids), not
    accidentally merged back into one.
    """
    descriptions_by_key = {d.key: d for d in SYSTEM_SENSOR_DESCRIPTIONS}
    assert "battery_power" in descriptions_by_key
    assert "battery_power_bms1" in descriptions_by_key

    battery_power = descriptions_by_key["battery_power"]
    battery_power_bms1 = descriptions_by_key["battery_power_bms1"]
    assert battery_power.value_fn({"battery_power_mqtt": 123}) == 123
    assert battery_power_bms1.value_fn({"battery_power_bms1_mqtt": 456}) == 456
    # Cross-reading the wrong bundle key must not accidentally match.
    assert battery_power.value_fn({"battery_power_bms1_mqtt": 456}) is None
    assert battery_power_bms1.value_fn({"battery_power_mqtt": 123}) is None


def test_ac_output_energy_in_sensor_is_mqtt_only_cumulative_energy_counter():
    descriptions = {d.key: d for d in SYSTEM_SENSOR_DESCRIPTIONS}

    energy_in_description = descriptions["ac_output_energy_in"]
    assert energy_in_description.requires_mqtt is True
    assert energy_in_description.translation_key == "ac_output_energy_in"
    assert energy_in_description.native_unit_of_measurement == "kWh"
    assert energy_in_description.device_class == "energy"
    assert energy_in_description.state_class == "total_increasing"
    assert energy_in_description.value_fn({"ac_output_energy_in": "12.345"}) == 12.345


def test_ac_output_energy_out_sensor_is_mqtt_only_cumulative_energy_counter():
    descriptions = {d.key: d for d in SYSTEM_SENSOR_DESCRIPTIONS}

    energy_out_description = descriptions["ac_output_energy_out"]
    assert energy_out_description.requires_mqtt is True
    assert energy_out_description.translation_key == "ac_output_energy_out"
    assert energy_out_description.native_unit_of_measurement == "kWh"
    assert energy_out_description.device_class == "energy"
    assert energy_out_description.state_class == "total_increasing"
    assert energy_out_description.value_fn({"ac_output_energy_out": "12.345"}) == 12.345


class TestMqttOrRestPrecedence:
    """discussion #6 Phase 3, item 10 ("Validate MQTT values against
    REST") regression-coverage gap: _mqtt_or_rest itself had no direct
    test coverage before this."""

    def test_prefers_mqtt_when_present(self):
        assert _mqtt_or_rest({"grid_power_mqtt": 100.0}, "grid_power_mqtt", 200.0) == 100.0

    def test_falls_back_to_rest_when_mqtt_key_absent(self):
        assert _mqtt_or_rest({}, "grid_power_mqtt", 200.0) == 200.0

    def test_returns_none_when_both_absent(self):
        assert _mqtt_or_rest({}, "grid_power_mqtt", None) is None

    def test_falls_back_to_rest_when_mqtt_value_is_not_coercible(self):
        assert _mqtt_or_rest({"grid_power_mqtt": None}, "grid_power_mqtt", 200.0) == 200.0


class TestInvertedAcSocketPower:
    def test_negative_feed_in_becomes_positive_generation(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]

        assert description.value_fn({"eps_load_power_mqtt": -450.0}) == 450.0

    def test_positive_consumption_becomes_negative(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]

        assert description.value_fn({"eps_load_power_mqtt": 275.0}) == -275.0

    def test_uses_rest_fallback_when_mqtt_value_is_absent(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]
        bundle = _nest(
            ("monitor", "energyFlowChartVO", "acInfo", "epsLoadPower"),
            -125.0,
        )

        assert description.value_fn(bundle) == 125.0

    def test_missing_source_value_remains_unknown(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]

        assert description.value_fn({}) is None

    def test_zero_is_normalized_to_positive_zero(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]

        assert str(description.value_fn({"eps_load_power_mqtt": 0.0})) == "0.0"

    def test_entity_is_optional_and_disabled_by_default(self):
        description = {
            item.key: item for item in SYSTEM_SENSOR_DESCRIPTIONS
        }["eps_load_power_inverted"]

        assert description.entity_registry_enabled_default is False
        assert description.translation_key == "eps_load_power_inverted"
        assert description.device_class == "power"
        assert description.state_class == "measurement"


def _nest(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {}
    cursor = bundle
    for key in path[:-1]:
        cursor[key] = {}
        cursor = cursor[key]
    cursor[path[-1]] = value
    return bundle


class TestSixMeterValueFnWiring:
    """Locks in which mqtt_key/REST path each of the 6 protocol-validation-
    scope sensors reads, so a future edit can't silently swap two meters -
    the same failure mode CONTRIBUTING.md #6 warns about for bundle-key
    renames, applied here to a lambda's captured constants instead of a
    coordinator bundle write."""

    def test_each_meter_prefers_its_own_mqtt_key_and_falls_back_to_its_own_rest_path(self):
        descriptions_by_key = {d.key: d for d in SYSTEM_SENSOR_DESCRIPTIONS}
        for sensor_key, mqtt_key, rest_path in _PROTOCOL_VALIDATION_METERS:
            description = descriptions_by_key[sensor_key]

            mqtt_bundle = {mqtt_key: 111.0, **_nest(rest_path, 222.0)}
            assert description.value_fn(mqtt_bundle) == 111.0, sensor_key

            rest_only_bundle = _nest(rest_path, 222.0)
            assert description.value_fn(rest_only_bundle) == 222.0, sensor_key
