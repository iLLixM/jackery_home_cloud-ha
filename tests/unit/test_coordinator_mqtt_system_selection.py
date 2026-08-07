"""Tests for JackeryHomeCloudCoordinator._resolve_mqtt_system / is_mqtt_system
(discussion #6, item 1, "Explicit selection of the MQTT-enabled system"):

    Let the user choose which Jackery system receives MQTT telemetry and
    controls. Store the selected system_id in the config entry.

`_resolve_mqtt_system` now looks up a single explicitly configured id
(CONF_MQTT_SYSTEM_ID, read from `self.config_entry.options`) instead of
iterating candidates - it no longer falls back to "the next system with a
resolvable serial" the way the old implicit heuristic did. It is constructed
via `object.__new__` with a minimal `config_entry` stand-in (a
`SimpleNamespace` exposing `.options`, the only attribute this method
reads), matching the rest of this test suite's pattern for pure
coordinator-method tests that don't need a real `hass`/`ConfigEntry`.
`is_mqtt_system` is unaffected by this change - it still reads exactly one
attribute (`self.mqtt_system`, a `JackeryMqttSystem | None` - see discussion
#6 item 2, "Structured MQTT system runtime model").
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.jackery_home_cloud.const import (
    CONF_ENABLE_MQTT,
    CONF_MQTT_SYSTEM_ID,
    CONF_MQTT_SYSTEM_SELECTION_PENDING,
)
from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)


def _make_coordinator(
    *, mqtt_system_id: str | None = None, configured_mqtt_system_id: str | None = None
) -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system = (
        JackeryMqttSystem(system_id=mqtt_system_id, device_serial="irrelevant")
        if mqtt_system_id is not None
        else None
    )
    coordinator.config_entry = SimpleNamespace(
        options={CONF_MQTT_SYSTEM_ID: configured_mqtt_system_id}
    )
    return coordinator


class TestResolveMqttSystem:
    def test_returns_configured_system_when_present_and_resolvable(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles = {
            "sys1": {"main_device_serial": "SN1"},
            "sys2": {"main_device_serial": "SN2"},
        }

        resolved = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert resolved == JackeryMqttSystem(system_id="sys1", device_serial="SN1")

    def test_ignores_other_systems_even_if_they_have_resolvable_serials(self):
        """Regression pin: the configured system is the only candidate - a
        second, non-configured system with a resolvable serial must NOT be
        picked just because the configured one fails. This is the key
        behavior difference from the old "first resolvable wins" heuristic."""
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles = {
            "sys1": {},  # no resolvable serial
            "sys2": {"main_device_serial": "SN2"},
        }

        resolved = coordinator._resolve_mqtt_system(["sys1", "sys2"], bundles)

        assert resolved is None

    def test_returns_none_when_no_system_configured(self):
        coordinator = _make_coordinator(configured_mqtt_system_id=None)
        bundles = {"sys1": {"main_device_serial": "SN1"}}

        resolved = coordinator._resolve_mqtt_system(["sys1"], bundles)

        assert resolved is None

    def test_returns_none_when_configured_system_is_empty_string(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="")
        bundles = {"sys1": {"main_device_serial": "SN1"}}

        resolved = coordinator._resolve_mqtt_system(["sys1"], bundles)

        assert resolved is None

    def test_returns_none_when_configured_system_not_in_selected_ids_this_cycle(self):
        """Covers both a system deselected via reconfigure and one dropped
        by the account since the id was configured."""
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles = {"sys2": {"main_device_serial": "SN2"}}

        resolved = coordinator._resolve_mqtt_system(["sys2"], bundles)

        assert resolved is None

    def test_returns_none_when_configured_system_bundle_missing(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles: dict = {}

        resolved = coordinator._resolve_mqtt_system(["sys1"], bundles)

        assert resolved is None

    def test_returns_none_when_configured_system_bundle_is_not_a_mapping(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles = {"sys1": "not-a-mapping"}

        resolved = coordinator._resolve_mqtt_system(["sys1"], bundles)

        assert resolved is None

    def test_returns_none_when_configured_system_bundle_has_no_serial(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")
        bundles = {"sys1": {}}

        resolved = coordinator._resolve_mqtt_system(["sys1"], bundles)

        assert resolved is None

    def test_empty_selected_system_ids_returns_none(self):
        coordinator = _make_coordinator(configured_mqtt_system_id="sys1")

        resolved = coordinator._resolve_mqtt_system([], {"sys1": {"main_device_serial": "SN1"}})

        assert resolved is None

    def test_compares_configured_id_via_str_coercion(self):
        coordinator = _make_coordinator(configured_mqtt_system_id=123)
        bundles = {"123": {"main_device_serial": "SN1"}}

        resolved = coordinator._resolve_mqtt_system(["123"], bundles)

        assert resolved == JackeryMqttSystem(system_id="123", device_serial="SN1")


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


def _make_pending_coordinator(*, options: dict) -> tuple[JackeryHomeCloudCoordinator, Mock]:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.config_entry = SimpleNamespace(options=dict(options))
    update_entry = Mock()
    coordinator.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update_entry))
    return coordinator, update_entry


class TestResolvePendingMqttSystemSelection:
    """Tests for _resolve_pending_mqtt_system_selection (discussion #6,
    reviewer follow-up on item 1): async_migrate_entry() defers
    CONF_MQTT_SYSTEM_ID for entries migrated with more than one selected
    system rather than guessing selected[0], since it has no live API data
    to know which one actually exposes a resolvable MQTT serial. This
    method runs on the coordinator's first (and any subsequent, until
    resolved) refresh to replicate the old "first resolvable wins"
    heuristic exactly once, using real bundle data.
    """

    def test_persists_first_selected_system_with_resolvable_serial(self):
        coordinator, update_entry = _make_pending_coordinator(
            options={CONF_MQTT_SYSTEM_ID: None, CONF_MQTT_SYSTEM_SELECTION_PENDING: True}
        )
        bundles = {
            "sys1": {},  # no resolvable serial
            "sys2": {"main_device_serial": "SN2"},
        }

        coordinator._resolve_pending_mqtt_system_selection(["sys1", "sys2"], bundles)

        update_entry.assert_called_once()
        _, kwargs = update_entry.call_args
        assert kwargs["options"][CONF_MQTT_SYSTEM_ID] == "sys2"
        assert CONF_MQTT_SYSTEM_SELECTION_PENDING not in kwargs["options"]

    def test_follows_selected_system_order_not_bundle_dict_order(self):
        coordinator, update_entry = _make_pending_coordinator(
            options={CONF_MQTT_SYSTEM_ID: None, CONF_MQTT_SYSTEM_SELECTION_PENDING: True}
        )
        bundles = {
            "sys1": {"main_device_serial": "SN1"},
            "sys2": {"main_device_serial": "SN2"},
        }

        coordinator._resolve_pending_mqtt_system_selection(["sys2", "sys1"], bundles)

        _, kwargs = update_entry.call_args
        assert kwargs["options"][CONF_MQTT_SYSTEM_ID] == "sys2"

    def test_leaves_pending_marker_when_no_system_resolvable_yet(self):
        """A transient gap (e.g. a REST hiccup) must not persist a guess -
        retry on the next refresh instead."""
        coordinator, update_entry = _make_pending_coordinator(
            options={CONF_MQTT_SYSTEM_ID: None, CONF_MQTT_SYSTEM_SELECTION_PENDING: True}
        )
        bundles = {"sys1": {}, "sys2": {}}

        coordinator._resolve_pending_mqtt_system_selection(["sys1", "sys2"], bundles)

        update_entry.assert_not_called()

    def test_preserves_unrelated_options(self):
        coordinator, update_entry = _make_pending_coordinator(
            options={
                CONF_MQTT_SYSTEM_ID: None,
                CONF_MQTT_SYSTEM_SELECTION_PENDING: True,
                CONF_ENABLE_MQTT: True,
            }
        )
        bundles = {"sys1": {"main_device_serial": "SN1"}}

        coordinator._resolve_pending_mqtt_system_selection(["sys1"], bundles)

        _, kwargs = update_entry.call_args
        assert kwargs["options"][CONF_ENABLE_MQTT] is True
