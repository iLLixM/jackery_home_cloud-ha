"""Tests for JackeryHomeCloudCoordinator._resolve_mqtt_system / is_mqtt_system
(backlog discussion #6, item 17, "Multi-system safeguard" section):

    the primary system is frozen during runtime, changed REST ordering
    does not switch it, ... deterministic fallback selection works

`_resolve_mqtt_system` reads no `self` state at all (only calls
`self._extract_system_device_sn(bundle)`, itself a function of its
`bundle` argument only), so it is constructed via `object.__new__` purely
for stylistic consistency with the rest of the coordinator test suite -
no attributes need to be seeded for it. `is_mqtt_system` reads exactly
one attribute (`self.mqtt_system_id`).
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
)


def _make_coordinator(mqtt_system_id: str | None = None) -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system_id = mqtt_system_id
    return coordinator


class TestResolveMqttSystem:
    def test_first_system_with_resolvable_serial_is_selected(self):
        coordinator = _make_coordinator()
        bundles = {
            "sys1": {"main_device_serial": "SN1"},
            "sys2": {"main_device_serial": "SN2"},
        }

        system_id, device_sn = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert system_id == "sys1"
        assert device_sn == "SN1"

    def test_falls_back_deterministically_to_next_system_with_resolvable_serial(self):
        coordinator = _make_coordinator()
        bundles = {
            "sys1": {},  # no resolvable serial
            "sys2": {"main_device_serial": "SN2"},
        }

        system_id, device_sn = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert system_id == "sys2"
        assert device_sn == "SN2"

    def test_no_system_has_a_resolvable_serial_returns_none(self):
        coordinator = _make_coordinator()
        bundles = {"sys1": {}, "sys2": {}}

        system_id, device_sn = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert system_id is None
        assert device_sn == ""

    def test_empty_selected_system_ids_returns_none(self):
        coordinator = _make_coordinator()

        system_id, device_sn = coordinator._resolve_mqtt_system([], {"sys1": {"main_device_serial": "SN1"}})

        assert system_id is None
        assert device_sn == ""

    def test_selection_order_is_what_matters_not_dict_order(self):
        """Regression pin for "changed REST ordering does not switch it":
        the resolution itself only depends on the order of
        `selected_system_ids`, never on the iteration order of the
        `bundles_by_id` mapping - a dict built in a different order (as
        REST responses may return systems in a different order between
        polls) must not change the outcome.
        """
        coordinator = _make_coordinator()
        bundles_forward = {
            "sys1": {"main_device_serial": "SN1"},
            "sys2": {"main_device_serial": "SN2"},
        }
        bundles_reversed = {
            "sys2": {"main_device_serial": "SN2"},
            "sys1": {"main_device_serial": "SN1"},
        }

        result_forward = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles_forward)
        result_reversed = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles_reversed)

        assert result_forward == result_reversed == ("sys1", "SN1")

    def test_bundle_missing_from_mapping_is_skipped(self):
        coordinator = _make_coordinator()
        bundles = {"sys2": {"main_device_serial": "SN2"}}  # sys1 absent entirely

        system_id, device_sn = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert system_id == "sys2"
        assert device_sn == "SN2"

    def test_non_mapping_bundle_is_skipped(self):
        coordinator = _make_coordinator()
        bundles = {"sys1": "not-a-mapping", "sys2": {"main_device_serial": "SN2"}}

        system_id, device_sn = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert system_id == "sys2"
        assert device_sn == "SN2"


class TestIsMqttSystem:
    def test_true_when_system_id_matches(self):
        coordinator = _make_coordinator(mqtt_system_id="sys1")
        assert coordinator.is_mqtt_system("sys1") is True

    def test_false_when_system_id_does_not_match(self):
        coordinator = _make_coordinator(mqtt_system_id="sys1")
        assert coordinator.is_mqtt_system("sys2") is False

    def test_false_when_no_primary_system_resolved_yet(self):
        coordinator = _make_coordinator(mqtt_system_id=None)
        assert coordinator.is_mqtt_system("sys1") is False

    def test_compares_via_str_coercion(self):
        coordinator = _make_coordinator(mqtt_system_id="123")
        assert coordinator.is_mqtt_system(123) is True
