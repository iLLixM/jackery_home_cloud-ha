"""Tests for the pure/hass-independent helpers in config_flow.py.

These are the functions that never touch `self.hass` (verified by
reading config_flow.py in full): schema builders, label/option
formatting, and `_prepare_credential_input`. They can be unit tested
directly, unlike the `async_step_*` methods which need a real
`hass`/`MockConfigEntry` (tracked separately as Tier 2 work pending
`tests/conftest.py`). This gets config_flow.py off 0% coverage without
waiting on that.
"""

from __future__ import annotations

import voluptuous as vol
import pytest

from custom_components.jackery_home_cloud.config_flow import (
    JackeryHomeCloudConfigFlow,
    _combined_system_options,
    _credential_schema,
    _mqtt_config_requires_crypto_key,
    _mqtt_system_schema,
    _reconfigure_credential_schema,
    _system_label,
    _system_options,
    _systems_mqtt_schema,
    _systems_schema,
)
from custom_components.jackery_home_cloud.const import (
    CONF_ACCOUNT,
    CONF_ENABLE_MQTT,
    CONF_MQTT_SYSTEM_ID,
    CONF_PASSWORD,
    CONF_PHONE_UID,
    CONF_SELECTED_SYSTEMS,
)


class TestMqttConfigRequiresCryptoKey:
    def test_none_requires_crypto_key(self):
        assert _mqtt_config_requires_crypto_key(None) is True

    def test_non_mapping_requires_crypto_key(self):
        assert _mqtt_config_requires_crypto_key("not-a-mapping") is True

    def test_plaintext_password_does_not_require_crypto_key(self):
        assert _mqtt_config_requires_crypto_key({"_password_is_plaintext": True}) is False

    def test_legacy_encoded_password_requires_crypto_key(self):
        assert _mqtt_config_requires_crypto_key({"_password_is_plaintext": False}) is True

    def test_missing_flag_defaults_to_requiring_crypto_key(self):
        assert _mqtt_config_requires_crypto_key({}) is True


class TestSystemLabel:
    def test_uses_name_and_system_no(self):
        system = {"id": "1", "name": "Home", "systemNo": "SN123"}
        assert _system_label(system) == "Home (SN123)"

    def test_falls_back_to_system_no_when_name_missing(self):
        system = {"id": "1", "systemNo": "SN123"}
        assert _system_label(system) == "SN123 (SN123)"

    def test_falls_back_to_id_when_name_and_system_no_missing(self):
        system = {"id": "42"}
        assert _system_label(system) == "42 (42)"


class TestSystemOptions:
    def test_builds_value_label_pairs(self):
        systems = [{"id": "1", "name": "Home", "systemNo": "SN1"}]
        assert _system_options(systems) == [{"value": "1", "label": "Home (SN1)"}]

    def test_filters_out_systems_without_id(self):
        systems = [{"id": "1", "name": "Home"}, {"name": "No id"}]
        options = _system_options(systems)
        assert len(options) == 1
        assert options[0]["value"] == "1"


class TestCredentialSchemas:
    def test_credential_schema_accepts_valid_input(self):
        schema = _credential_schema()
        result = schema({CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"})
        assert result[CONF_ACCOUNT] == "user@example.com"

    def test_credential_schema_requires_password(self):
        schema = _credential_schema()
        with pytest.raises(vol.Invalid):
            schema({CONF_ACCOUNT: "user@example.com"})

    def test_credential_schema_applies_account_default(self):
        schema = _credential_schema({CONF_ACCOUNT: "saved@example.com"})
        result = schema({CONF_PASSWORD: "pw"})
        assert result[CONF_ACCOUNT] == "saved@example.com"

    def test_reconfigure_schema_has_optional_phone_uid(self):
        schema = _reconfigure_credential_schema()
        result = schema({CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"})
        assert result[CONF_PHONE_UID] == ""

    def test_reconfigure_schema_applies_phone_uid_default(self):
        schema = _reconfigure_credential_schema({CONF_PHONE_UID: "ha-existing"})
        result = schema({CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"})
        assert result[CONF_PHONE_UID] == "ha-existing"


class TestSystemsMqttSchema:
    def test_builds_select_options_from_systems(self):
        systems = [{"systemId": "1", "systemName": "Home", "systemNo": "SN1"}]
        schema = _systems_mqtt_schema(systems)
        result = schema({CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True})
        assert result[CONF_SELECTED_SYSTEMS] == ["1"]
        assert result[CONF_ENABLE_MQTT] is True

    def test_skips_systems_without_resolvable_id(self):
        systems = [{"systemName": "No id"}, {"systemId": "2", "systemName": "Valid"}]
        schema = _systems_mqtt_schema(systems)
        result = schema({CONF_SELECTED_SYSTEMS: ["2"], CONF_ENABLE_MQTT: False})
        assert result[CONF_SELECTED_SYSTEMS] == ["2"]

    def test_label_appends_system_no_when_not_already_in_name(self):
        systems = [{"systemId": "1", "systemName": "Home", "systemNo": "SN1"}]
        schema = _systems_mqtt_schema(systems)
        select_selector = schema.schema[CONF_SELECTED_SYSTEMS]
        options = select_selector.config["options"]
        assert options[0]["label"] == "Home (SN1)"

    def test_label_does_not_duplicate_system_no_already_in_name(self):
        systems = [{"systemId": "1", "systemName": "Home SN1", "systemNo": "SN1"}]
        schema = _systems_mqtt_schema(systems)
        select_selector = schema.schema[CONF_SELECTED_SYSTEMS]
        options = select_selector.config["options"]
        assert options[0]["label"] == "Home SN1"

    def test_defaults_carry_over_selected_systems_and_toggles(self):
        systems = [{"systemId": "1", "systemName": "Home"}]
        defaults = {CONF_SELECTED_SYSTEMS: ["1"], CONF_ENABLE_MQTT: True}
        schema = _systems_mqtt_schema(systems, defaults)
        result = schema({})
        assert result[CONF_SELECTED_SYSTEMS] == ["1"]
        assert result[CONF_ENABLE_MQTT] is True


class TestCombinedSystemOptions:
    """Mirrors TestSystemsMqttSchema's label-format assertions, but against
    the extracted helper directly - a regression pin proving the extraction
    didn't change _systems_mqtt_schema's output (see next class)."""

    def test_builds_value_label_pairs(self):
        systems = [{"systemId": "1", "systemName": "Home", "systemNo": "SN1"}]
        assert _combined_system_options(systems) == [{"value": "1", "label": "Home (SN1)"}]

    def test_skips_systems_without_resolvable_id(self):
        systems = [{"systemName": "No id"}, {"systemId": "2", "systemName": "Valid"}]
        options = _combined_system_options(systems)
        assert len(options) == 1
        assert options[0]["value"] == "2"

    def test_label_does_not_duplicate_system_no_already_in_name(self):
        systems = [{"systemId": "1", "systemName": "Home SN1", "systemNo": "SN1"}]
        options = _combined_system_options(systems)
        assert options[0]["label"] == "Home SN1"

    def test_falls_back_to_id_or_system_no_for_name(self):
        systems = [{"systemId": "1", "systemNo": "SN1"}]
        options = _combined_system_options(systems)
        assert options[0]["label"] == "SN1"


class TestMqttSystemSchema:
    def test_accepts_a_value_from_the_provided_options(self):
        options = [{"value": "1", "label": "Home"}, {"value": "2", "label": "Cabin"}]
        schema = _mqtt_system_schema(options, default_system_id="1")
        result = schema({CONF_MQTT_SYSTEM_ID: "2"})
        assert result[CONF_MQTT_SYSTEM_ID] == "2"

    def test_rejects_a_value_outside_the_provided_options(self):
        options = [{"value": "1", "label": "Home"}]
        schema = _mqtt_system_schema(options, default_system_id="1")
        with pytest.raises(vol.Invalid):
            schema({CONF_MQTT_SYSTEM_ID: "not-an-option"})

    def test_default_carries_over(self):
        options = [{"value": "1", "label": "Home"}, {"value": "2", "label": "Cabin"}]
        schema = _mqtt_system_schema(options, default_system_id="2")
        result = schema({})
        assert result[CONF_MQTT_SYSTEM_ID] == "2"

    def test_selector_is_single_select_not_multiple(self):
        options = [{"value": "1", "label": "Home"}]
        schema = _mqtt_system_schema(options, default_system_id="1")
        select_selector = schema.schema[CONF_MQTT_SYSTEM_ID]
        assert select_selector.config["multiple"] is False


class TestSystemsSchema:
    def test_default_selection_uses_current_selection_when_still_available(self):
        systems = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
        schema = _systems_schema(systems, current_selection=["1"])
        result = schema({})
        assert result[CONF_SELECTED_SYSTEMS] == ["1"]

    def test_default_selection_falls_back_to_all_when_current_selection_unavailable(self):
        systems = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
        schema = _systems_schema(systems, current_selection=["stale-id"])
        result = schema({})
        assert sorted(result[CONF_SELECTED_SYSTEMS]) == ["1", "2"]

    def test_current_selection_is_filtered_to_available_ids_only(self):
        systems = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
        schema = _systems_schema(systems, current_selection=["1", "stale-id"])
        result = schema({})
        assert result[CONF_SELECTED_SYSTEMS] == ["1"]


class TestPrepareCredentialInput:
    def _flow(self) -> JackeryHomeCloudConfigFlow:
        return JackeryHomeCloudConfigFlow()

    def test_strips_account_whitespace(self):
        flow = self._flow()
        result = flow._prepare_credential_input(
            {CONF_ACCOUNT: "  user@example.com  ", CONF_PASSWORD: "pw"}
        )
        assert result[CONF_ACCOUNT] == "user@example.com"

    def test_derives_phone_uid_when_blank(self):
        flow = self._flow()
        result = flow._prepare_credential_input({CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"})
        assert result[CONF_PHONE_UID].startswith("ha-")

    def test_preserves_given_phone_uid(self):
        flow = self._flow()
        result = flow._prepare_credential_input(
            {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_PHONE_UID: "ha-existing"}
        )
        assert result[CONF_PHONE_UID] == "ha-existing"

    def test_defaults_enable_mqtt_when_absent(self):
        flow = self._flow()
        result = flow._prepare_credential_input({CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw"})
        assert CONF_ENABLE_MQTT in result

    def test_respects_explicit_enable_mqtt_value(self):
        flow = self._flow()
        result = flow._prepare_credential_input(
            {CONF_ACCOUNT: "user@example.com", CONF_PASSWORD: "pw", CONF_ENABLE_MQTT: True}
        )
        assert result[CONF_ENABLE_MQTT] is True
