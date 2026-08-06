"""Unit tests for mqtt_parser.py.

Covers: payload normalization, non-JSON payload handling, nested key
search for gw_sn/dev_sn/method, and the EMS meter value extraction rules
(cmd filtering, dev_sn prefix matching, meter_list lookup, raw vs float
extraction).
"""

from __future__ import annotations

import json

import pytest

from custom_components.jackery_home_cloud.mqtt_parser import (
    extract_ems_meter_raw_value,
    extract_ems_meter_value,
    parse_mqtt_payload,
)


class TestParseMqttPayload:
    def test_valid_json_payload_extracts_top_level_fields(self):
        payload = json.dumps(
            {"gw_sn": "GW123", "dev_sn": "ems_ABC", "method": "report"}
        ).encode("utf-8")
        result = parse_mqtt_payload("v1/iot_gw/gw/data/GW123", payload)
        assert result["gw_sn"] == "GW123"
        assert result["dev_sn"] == "ems_ABC"
        assert result["method"] == "report"
        assert result["topic"] == "v1/iot_gw/gw/data/GW123"
        assert result["payload_json"] == json.loads(payload)

    def test_nested_fields_are_found_recursively(self):
        payload = json.dumps(
            {"info": {"nested": {"gw_sn": "GW999"}}}
        ).encode("utf-8")
        result = parse_mqtt_payload("topic", payload)
        assert result["gw_sn"] == "GW999"

    def test_invalid_json_payload_yields_none_fields(self):
        payload = b"not-json-at-all {{{"
        result = parse_mqtt_payload("topic", payload)
        assert result["payload_json"] is None
        assert result["gw_sn"] is None
        assert result["dev_sn"] is None
        assert result["method"] is None
        assert result["payload_text"] == "not-json-at-all {{{"

    def test_non_utf8_bytes_do_not_raise(self):
        payload = b"\xff\xfe\x00invalid"
        result = parse_mqtt_payload("topic", payload)
        assert result["payload_json"] is None

    def test_empty_payload(self):
        result = parse_mqtt_payload("topic", b"")
        assert result["payload_json"] is None
        assert result["payload_text"] == ""


def _make_payload(cmd: str, dev_sn: str, meter_list: list) -> dict:
    return {
        "cmd": cmd,
        "info": {"dev_list": [{"dev_sn": dev_sn, "meter_list": meter_list}]},
    }


class TestExtractEmsMeterValue:
    @pytest.mark.parametrize("cmd", ["data_report", "data_get", "data_set"])
    def test_accepted_cmds_return_value(self, cmd):
        payload = _make_payload(cmd, "ems_SN1", [["123", "45.6"]])
        value = extract_ems_meter_value(payload, device_serial="SN1", meter_id="123")
        assert value == 45.6

    def test_unsupported_cmd_returns_none(self):
        payload = _make_payload("other_cmd", "ems_SN1", [["123", "45.6"]])
        value = extract_ems_meter_value(payload, device_serial="SN1", meter_id="123")
        assert value is None

    def test_dev_sn_prefix_must_match(self):
        payload = _make_payload("data_report", "pcs_SN1", [["123", "45.6"]])
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") is None
        assert (
            extract_ems_meter_value(
                payload, device_serial="SN1", meter_id="123", dev_sn_prefix="pcs"
            )
            == 45.6
        )

    def test_meter_id_not_found_returns_none(self):
        payload = _make_payload("data_report", "ems_SN1", [["999", "1.0"]])
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") is None

    def test_device_serial_not_found_returns_none(self):
        payload = _make_payload("data_report", "ems_SN1", [["123", "45.6"]])
        assert extract_ems_meter_value(payload, device_serial="OTHER", meter_id="123") is None

    def test_non_numeric_value_returns_none_for_float_extraction(self):
        payload = _make_payload("data_report", "ems_SN1", [["123", "not-a-number"]])
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") is None

    def test_raw_extraction_preserves_exact_string(self):
        payload = _make_payload("data_report", "ems_SN1", [["123", "0007"]])
        assert (
            extract_ems_meter_raw_value(payload, device_serial="SN1", meter_id="123")
            == "0007"
        )

    def test_malformed_payload_shapes_return_none(self):
        assert extract_ems_meter_value("not-a-mapping", device_serial="SN1", meter_id="123") is None
        assert extract_ems_meter_value({}, device_serial="SN1", meter_id="123") is None
        assert (
            extract_ems_meter_value(
                {"cmd": "data_report", "info": "not-a-mapping"},
                device_serial="SN1",
                meter_id="123",
            )
            is None
        )
        assert (
            extract_ems_meter_value(
                {"cmd": "data_report", "info": {"dev_list": "not-a-list"}},
                device_serial="SN1",
                meter_id="123",
            )
            is None
        )

    def test_meter_list_wrong_shape_returns_none(self):
        payload = _make_payload("data_report", "ems_SN1", [["only-one-element"]])
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") is None

    def test_meter_list_item_not_a_list_at_all_is_skipped(self):
        """Unlike a too-short list (covered above), a meter_list entry
        that isn't a list at all (e.g. an unexpected dict shape from a
        firmware variant) must be skipped rather than raising."""
        payload = _make_payload(
            "data_report", "ems_SN1", [{"unexpected": "dict-shape"}, ["123", "45.6"]]
        )
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") == 45.6

    def test_meter_id_matched_as_string_regardless_of_int_type(self):
        payload = _make_payload("data_report", "ems_SN1", [[123, "45.6"]])
        assert extract_ems_meter_value(payload, device_serial="SN1", meter_id="123") == 45.6
