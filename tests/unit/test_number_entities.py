"""Class-attribute-level tests for number.py MQTT-backed controls
(Family F: unique_id stability).

`_bundle_key` / `_unique_id_suffix` / `_meter_id` are plain class
attributes on `_JackeryMqttNumberEntity` subclasses, so they can be
inspected directly without instantiating the entity (no coordinator, no
hass fixture, no config entry needed).
"""

from __future__ import annotations

from custom_components.jackery_home_cloud.number import (
    JackeryChargeLimitSocNumber,
    JackeryDischargeLimitSocNumber,
    JackeryFeedPowerLimitNumber,
)

NUMBER_ENTITY_CLASSES = (
    JackeryDischargeLimitSocNumber,
    JackeryChargeLimitSocNumber,
    JackeryFeedPowerLimitNumber,
)

# Pinned snapshot: (unique_id_suffix -> bundle_key). unique_id is built as
# f"system_{system_id}_{_unique_id_suffix}" - renaming _unique_id_suffix on
# a shipped entity breaks upgrades exactly like the sensor.py `key` rename
# in PR #8. bundle_key must match whatever coordinator.py actually
# populates (see tests/regression/test_rename_safety.py for the
# producer/consumer cross-check on bundle_key specifically).
EXPECTED_NUMBER_ENTITIES = {
    "discharge_limit_soc": "discharge_limit_soc_mqtt",
    "charge_limit_soc": "charge_limit_soc_mqtt",
    "feed_power_limit": "feed_power_limit_mqtt",
}


def test_unique_id_suffixes_are_unique_across_number_entities():
    suffixes = [cls._unique_id_suffix for cls in NUMBER_ENTITY_CLASSES]
    duplicates = {s for s in suffixes if suffixes.count(s) > 1}
    assert not duplicates, f"Duplicate _unique_id_suffix values: {duplicates}"


def test_bundle_keys_are_unique_across_number_entities():
    bundle_keys = [cls._bundle_key for cls in NUMBER_ENTITY_CLASSES]
    duplicates = {k for k in bundle_keys if bundle_keys.count(k) > 1}
    assert not duplicates, f"Duplicate _bundle_key values: {duplicates}"


def test_number_entity_snapshot_matches_pinned_set():
    actual = {cls._unique_id_suffix: cls._bundle_key for cls in NUMBER_ENTITY_CLASSES}
    assert actual == EXPECTED_NUMBER_ENTITIES, (
        f"number.py entity unique_id_suffix/bundle_key mapping changed: "
        f"{actual}. If this is a deliberate rename of a *shipped* entity's "
        f"_unique_id_suffix, existing installs will get an orphaned + a "
        f"duplicate entity. Update EXPECTED_NUMBER_ENTITIES only after "
        f"confirming that impact is intended/acceptable."
    )


def test_no_entity_class_left_with_blank_defaults():
    """Every concrete subclass must override the base class's blank
    defaults - an empty _bundle_key/_unique_id_suffix would silently read
    None forever / collide unique_ids across systems.
    """
    for cls in NUMBER_ENTITY_CLASSES:
        assert cls._bundle_key, f"{cls.__name__}._bundle_key must not be empty"
        assert cls._unique_id_suffix, f"{cls.__name__}._unique_id_suffix must not be empty"
        assert cls._meter_id, f"{cls.__name__}._meter_id must not be empty"
