"""Unit tests for mqtt_registry.py."""

from __future__ import annotations

import pytest

from custom_components.jackery_home_cloud.mqtt_registry import (
    build_default_subscriptions,
)


class TestBuildDefaultSubscriptions:
    def test_returns_lwt_and_data_topics_with_expected_qos(self):
        subs = build_default_subscriptions("SN12345")
        assert subs == (
            ("v1/iot_gw/gw_lwt/SN12345", 0),
            ("v1/iot_gw/gw/data/SN12345", 1),
        )

    def test_strips_whitespace_from_device_serial(self):
        subs = build_default_subscriptions("  SN12345  ")
        assert subs[0][0] == "v1/iot_gw/gw_lwt/SN12345"
        assert subs[1][0] == "v1/iot_gw/gw/data/SN12345"

    @pytest.mark.parametrize("empty_serial", ["", "   "])
    def test_empty_serial_returns_empty_tuple(self, empty_serial):
        assert build_default_subscriptions(empty_serial) == ()

    def test_none_serial_is_stringified_not_treated_as_empty(self):
        # Documents current (surprising) behavior: str(None) == "None" is
        # truthy, so a None serial is NOT short-circuited to an empty
        # tuple - it produces topics containing the literal string "None".
        subs = build_default_subscriptions(None)
        assert subs == (
            ("v1/iot_gw/gw_lwt/None", 0),
            ("v1/iot_gw/gw/data/None", 1),
        )

    def test_result_is_immutable_tuple_of_tuples(self):
        subs = build_default_subscriptions("SN1")
        assert isinstance(subs, tuple)
        assert all(isinstance(item, tuple) for item in subs)
