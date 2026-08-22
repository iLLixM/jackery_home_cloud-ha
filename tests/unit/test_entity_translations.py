"""Regression tests for Home Assistant entity and service localization."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.jackery_home_cloud.button import JackeryRebootButton
from custom_components.jackery_home_cloud.number import (
    JackeryChargeLimitSocNumber,
    JackeryDischargeLimitSocNumber,
    JackeryFeedPowerLimitNumber,
)
from custom_components.jackery_home_cloud.select import (
    MODE_OPTIONS,
    JackeryOutputPowerLimitSelect,
    JackeryWorkModeSelect,
)
from custom_components.jackery_home_cloud.sensor import (
    SYSTEM_SENSOR_DESCRIPTIONS,
    JackeryMetricSensor,
    JackeryScheduleSensor,
)
from custom_components.jackery_home_cloud.switch import (
    JackeryAcOutputSwitch,
    JackeryAutoStandbySwitch,
    JackeryStandbySwitch,
)

INTEGRATION_DIR = (
    Path(__file__).parents[2] / "custom_components" / "jackery_home_cloud"
)
LANGUAGES = ("en", "de", "fr")

ENTITY_CLASSES = {
    "switch": {
        "ac_output": JackeryAcOutputSwitch,
        "standby": JackeryStandbySwitch,
        "auto_standby": JackeryAutoStandbySwitch,
    },
    "select": {
        "work_mode": JackeryWorkModeSelect,
        "output_power_limit": JackeryOutputPowerLimitSelect,
    },
    "number": {
        "discharge_limit_soc": JackeryDischargeLimitSocNumber,
        "charge_limit_soc": JackeryChargeLimitSocNumber,
        "feed_power_limit": JackeryFeedPowerLimitNumber,
    },
    "button": {"reboot_device": JackeryRebootButton},
}

EXPECTED_SERVICE_FIELDS = {
    "set_charge_window": {"slot", "start", "end"},
    "set_discharge_window": {"slot", "start", "end"},
    "clear_charge_window": {"slot"},
    "clear_discharge_window": {"slot"},
}


def _translations() -> dict[str, dict]:
    return {
        language: json.loads(
            (INTEGRATION_DIR / "translations" / f"{language}.json").read_text(
                encoding="utf-8"
            )
        )
        for language in LANGUAGES
    }


def _schema(value):
    """Return nested dictionary keys while ignoring translated text."""
    if isinstance(value, dict):
        return {key: _schema(child) for key, child in value.items()}
    return type(value).__name__


def test_every_metric_sensor_uses_its_stable_key_for_translation():
    assert all(
        description.translation_key == description.key
        for description in SYSTEM_SENSOR_DESCRIPTIONS
    )


def test_metric_sensor_does_not_override_translated_name():
    description = SYSTEM_SENSOR_DESCRIPTIONS[0]
    coordinator = SimpleNamespace(data={"systems": {"sys1": {"monitor": {}}}})

    entity = JackeryMetricSensor(coordinator, "sys1", description)

    assert getattr(entity, "_attr_name", None) is None
    assert entity.entity_description.translation_key == description.key


def test_all_non_metric_entities_use_translation_keys_without_names():
    coordinator = SimpleNamespace(data={"systems": {}})
    for domain, classes in ENTITY_CLASSES.items():
        for translation_key, entity_class in classes.items():
            entity = entity_class(
                coordinator=coordinator,
                system_id="sys1",
                bundle={"system": {}},
                mqtt_client=object(),
                device_sn="SN1",
            )
            assert entity.has_entity_name is True, (domain, translation_key)
            assert entity.translation_key == translation_key
            assert getattr(entity, "_attr_name", None) is None

    schedule = JackeryScheduleSensor(coordinator=coordinator, system_id="sys1")
    assert schedule.has_entity_name is True
    assert schedule.translation_key == "charge_discharge_schedule"
    assert getattr(schedule, "_attr_name", None) is None


def test_entity_translation_schema_matches_python_and_all_languages():
    translations = _translations()
    expected_sensor_keys = {
        description.translation_key for description in SYSTEM_SENSOR_DESCRIPTIONS
    } | {"charge_discharge_schedule"}
    expected_domain_keys = {
        "sensor": expected_sensor_keys,
        **{
            domain: set(classes)
            for domain, classes in ENTITY_CLASSES.items()
        },
    }

    for language, translation in translations.items():
        assert set(translation["entity"]) == set(expected_domain_keys), language
        for domain, expected_keys in expected_domain_keys.items():
            assert set(translation["entity"][domain]) == expected_keys, (
                language,
                domain,
            )

    english_schema = _schema(translations["en"]["entity"])
    assert _schema(translations["de"]["entity"]) == english_schema
    assert _schema(translations["fr"]["entity"]) == english_schema


def test_service_translation_schema_is_complete_and_identical():
    translations = _translations()
    for language, translation in translations.items():
        assert set(translation["services"]) == set(EXPECTED_SERVICE_FIELDS), language
        for service, expected_fields in EXPECTED_SERVICE_FIELDS.items():
            service_translation = translation["services"][service]
            assert set(service_translation) == {"name", "description", "fields"}
            assert set(service_translation["fields"]) == expected_fields

    english_schema = _schema(translations["en"]["services"])
    assert _schema(translations["de"]["services"]) == english_schema
    assert _schema(translations["fr"]["services"]) == english_schema


def test_automation_visible_work_mode_options_remain_backward_compatible():
    assert tuple(MODE_OPTIONS) == (
        "Self-consumption",
        "Battery priority",
        "Time of use",
        "Intelligent mode",
    )


def test_services_yaml_contains_only_stable_schema_and_selectors():
    services_yaml = (INTEGRATION_DIR / "services.yaml").read_text(encoding="utf-8")
    assert "  name:" not in services_yaml
    assert "  description:" not in services_yaml
