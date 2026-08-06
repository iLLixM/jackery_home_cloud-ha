"""Tests for MODEL_CAPABILITIES (const.py) and JackeryHomeCloudCoordinator.
detected_model/is_model_confirmed/supports_meter (discussion #6, item 9,
"Device capability and model detection"):

    Create only those MQTT entities and controls that the connected
    device actually supports.

Only JAKS-IN1K5-BA2K-EUA1 has ever been confirmed against real hardware
(see README.md/CONTRIBUTING.md #1); an unmapped model is "unconfirmed",
not "unsupported" - supports_meter() returns True for every meter_id on an
unconfirmed model (see const.py's MODEL_CAPABILITIES docstring), and
is_model_confirmed() is the separate signal platforms use to create
entities disabled-by-default instead of dropping them.

`detected_model`/`is_model_confirmed`/`supports_meter` only read
`self.data`/`self.mqtt_system` (via `is_mqtt_system`) - no `hass` needed,
so the coordinator is built via `object.__new__`, same pattern as
test_coordinator_mqtt_system_selection.py/test_coordinator_bundle_merge.py.
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.const import MODEL_CAPABILITIES
from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)

SYSTEM_ID = "sys1"
CONFIRMED_MODEL = "JAKS-IN1K5-BA2K-EUA1"
UNKNOWN_MODEL = "SOME-FUTURE-MODEL"


class TestModelCapabilitiesShape:
    def test_no_model_maps_to_an_empty_capability_set(self):
        for model, meters in MODEL_CAPABILITIES.items():
            assert meters, f"{model} has an empty capability set"

    def test_confirmed_model_is_present(self):
        assert CONFIRMED_MODEL in MODEL_CAPABILITIES


def _make_coordinator(
    *,
    system: dict | None = None,
    mqtt_system_id: str | None = SYSTEM_ID,
) -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system = (
        JackeryMqttSystem(system_id=mqtt_system_id, device_serial="SN1")
        if mqtt_system_id is not None
        else None
    )
    coordinator.data = {
        "systems": {SYSTEM_ID: {"system": system if system is not None else {}}}
    }
    return coordinator


class TestDetectedModel:
    def test_reads_factory_model(self):
        coordinator = _make_coordinator(system={"factoryModel": CONFIRMED_MODEL})
        assert coordinator.detected_model(SYSTEM_ID) == CONFIRMED_MODEL

    def test_falls_back_to_series_then_model(self):
        coordinator = _make_coordinator(system={"series": "X"})
        assert coordinator.detected_model(SYSTEM_ID) == "X"

        coordinator = _make_coordinator(system={"model": "Y"})
        assert coordinator.detected_model(SYSTEM_ID) == "Y"

    def test_returns_none_when_no_model_field_present(self):
        coordinator = _make_coordinator(system={})
        assert coordinator.detected_model(SYSTEM_ID) is None

    def test_returns_none_for_unknown_system(self):
        coordinator = _make_coordinator()
        assert coordinator.detected_model("missing") is None


class TestIsModelConfirmed:
    def test_true_for_confirmed_model(self):
        coordinator = _make_coordinator(system={"factoryModel": CONFIRMED_MODEL})
        assert coordinator.is_model_confirmed(SYSTEM_ID) is True

    def test_false_for_unknown_model(self):
        coordinator = _make_coordinator(system={"factoryModel": UNKNOWN_MODEL})
        assert coordinator.is_model_confirmed(SYSTEM_ID) is False

    def test_false_when_no_model_detected(self):
        coordinator = _make_coordinator(system={})
        assert coordinator.is_model_confirmed(SYSTEM_ID) is False


class TestSupportsMeter:
    def test_confirmed_model_supports_its_listed_meters(self):
        coordinator = _make_coordinator(system={"factoryModel": CONFIRMED_MODEL})
        for meter_id in MODEL_CAPABILITIES[CONFIRMED_MODEL]:
            assert coordinator.supports_meter(SYSTEM_ID, meter_id) is True

    def test_confirmed_model_rejects_an_unlisted_meter(self):
        coordinator = _make_coordinator(system={"factoryModel": CONFIRMED_MODEL})
        assert coordinator.supports_meter(SYSTEM_ID, "not-a-real-meter-id") is False

    def test_unconfirmed_model_supports_every_meter(self):
        coordinator = _make_coordinator(system={"factoryModel": UNKNOWN_MODEL})
        assert coordinator.supports_meter(SYSTEM_ID, "anything-at-all") is True

    def test_no_detected_model_supports_every_meter(self):
        coordinator = _make_coordinator(system={})
        assert coordinator.supports_meter(SYSTEM_ID, "anything-at-all") is True

    def test_false_for_non_mqtt_system(self):
        coordinator = _make_coordinator(
            system={"factoryModel": CONFIRMED_MODEL}, mqtt_system_id="other-system"
        )
        assert coordinator.supports_meter(SYSTEM_ID, "anything-at-all") is False
