"""Tests for the pure daily-energy/trend reconciliation helper functions
in coordinator.py (module-level, no coordinator instance needed):
`_coerce_float`, `_prefer_value`, `_first_cluster_system`, `_trend_list`,
`_sum_positive`, `_sum_negative_as_positive`, `_sum_bms_values`,
`_values_are_consistent`, `_source_priority`, `_system_battery_capacity_kwh`.

These back `_build_daily_energy_summary`/`_resolve_battery_daily_value`,
the multi-source (BMS total / per-BMS list / cluster trend) reconciliation
engine for daily battery energy. That reconciliation logic itself (source
scoring + plausibility checks + midnight-reset handling) is substantial
enough to warrant its own dedicated effort beyond this pass; these tests
cover the well-defined building blocks it's composed from, which were
previously completely untested.
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.coordinator import (
    _coerce_float,
    _first_cluster_system,
    _prefer_value,
    _source_priority,
    _sum_bms_values,
    _sum_negative_as_positive,
    _sum_positive,
    _system_battery_capacity_kwh,
    _trend_list,
    _values_are_consistent,
)


class TestCoerceFloat:
    def test_numeric_string(self):
        assert _coerce_float("12.5") == 12.5

    def test_int(self):
        assert _coerce_float(7) == 7.0

    def test_none_returns_none(self):
        assert _coerce_float(None) is None

    def test_invalid_string_returns_none(self):
        assert _coerce_float("not-a-number") is None


class TestPreferValue:
    def test_returns_first_non_none(self):
        assert _prefer_value(None, None, 3.0, 4.0) == 3.0

    def test_all_none_returns_none(self):
        assert _prefer_value(None, None) is None

    def test_no_args_returns_none(self):
        assert _prefer_value() is None


class TestFirstClusterSystem:
    def test_returns_first_item_from_primary_key(self):
        payload = {"trendClusterSystemList": [{"id": "a"}, {"id": "b"}]}
        assert _first_cluster_system(payload) == {"id": "a"}

    def test_falls_back_to_real_trend_key(self):
        payload = {"realTrendClusterSystemList": [{"id": "fallback"}]}
        assert _first_cluster_system(payload) == {"id": "fallback"}

    def test_missing_both_keys_returns_empty_dict(self):
        assert _first_cluster_system({}) == {}

    def test_non_mapping_items_are_skipped(self):
        payload = {"trendClusterSystemList": ["not-a-mapping", {"id": "real"}]}
        assert _first_cluster_system(payload) == {"id": "real"}


class TestTrendList:
    def test_filters_to_mapping_rows_only(self):
        trend_system = {"trendList": [{"a": 1}, "not-a-mapping", {"b": 2}]}
        assert _trend_list(trend_system) == [{"a": 1}, {"b": 2}]

    def test_non_list_returns_empty(self):
        assert _trend_list({"trendList": "not-a-list"}) == []

    def test_missing_key_returns_empty(self):
        assert _trend_list({}) == []


class TestSumPositive:
    def test_sums_only_positive_values(self):
        rows = [{"x": 1.5}, {"x": -2.0}, {"x": 3.0}]
        assert _sum_positive(rows, "x") == 4.5

    def test_ignores_missing_or_non_numeric(self):
        rows = [{"x": "n/a"}, {"y": 1}, {"x": 2.0}]
        assert _sum_positive(rows, "x") == 2.0

    def test_none_when_no_values_seen_at_all(self):
        assert _sum_positive([], "x") is None
        assert _sum_positive([{"y": 1}], "x") is None

    def test_zero_when_only_negative_values_seen(self):
        # seen=True but total stays 0 - distinguishes "saw data, all
        # negative" from "never saw the key at all" (which is None).
        assert _sum_positive([{"x": -5.0}], "x") == 0.0


class TestSumNegativeAsPositive:
    def test_sums_absolute_value_of_negatives_only(self):
        rows = [{"x": -1.5}, {"x": 2.0}, {"x": -3.0}]
        assert _sum_negative_as_positive(rows, "x") == 4.5

    def test_none_when_no_values_seen(self):
        assert _sum_negative_as_positive([], "x") is None


class TestSumBmsValues:
    def test_sums_across_bms_list(self):
        rows = [{"charge": 1.0}, {"charge": 2.5}]
        assert _sum_bms_values(rows, "charge") == 3.5

    def test_non_list_input_returns_none(self):
        assert _sum_bms_values("not-a-list", "charge") is None
        assert _sum_bms_values(None, "charge") is None

    def test_non_mapping_rows_are_skipped(self):
        rows = ["not-a-mapping", {"charge": 1.0}]
        assert _sum_bms_values(rows, "charge") == 1.0


class TestValuesAreConsistent:
    def test_close_values_within_relative_tolerance_are_consistent(self):
        # tolerance = max(0.05, min(10,10.5)*0.10) = max(0.05, 1.0) = 1.0
        assert _values_are_consistent(10.0, 10.5) is True

    def test_far_apart_values_are_not_consistent(self):
        assert _values_are_consistent(10.0, 15.0) is False

    def test_small_values_use_the_minimum_absolute_tolerance(self):
        # tolerance = max(0.05, min(0.1,0.16)*0.10) = max(0.05, 0.01) = 0.05
        assert _values_are_consistent(0.10, 0.16) is False  # diff 0.06 > 0.05
        assert _values_are_consistent(0.10, 0.12) is True  # diff 0.02 <= 0.05

    def test_either_value_none_is_never_consistent(self):
        assert _values_are_consistent(None, 1.0) is False
        assert _values_are_consistent(1.0, None) is False


class TestSourcePriority:
    def test_known_sources_ranked_in_documented_order(self):
        assert _source_priority("bms_total_div1000") == 0
        assert _source_priority("bms_list_sum") == 1
        assert _source_priority("cluster_reference") == 2
        assert _source_priority("bms_total_direct") == 3

    def test_unknown_source_gets_lowest_priority(self):
        assert _source_priority("something_unexpected") == 99


class TestSystemBatteryCapacityKwh:
    def test_reads_nested_monitor_field(self):
        monitor = {"systemVO": {"batteryCapacity": "5.12"}}
        assert _system_battery_capacity_kwh(monitor) == 5.12

    def test_missing_field_returns_none(self):
        assert _system_battery_capacity_kwh({}) is None
