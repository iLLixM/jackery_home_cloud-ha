"""Regression tests for stable metric-sensor registry identities."""

from __future__ import annotations

from datetime import UTC, datetime

from freezegun import freeze_time
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jackery_home_cloud.const import DOMAIN
from custom_components.jackery_home_cloud.entity_identity import (
    ENTITY_MIGRATION_TEMP_MARKER,
    sensor_unique_id,
)
from custom_components.jackery_home_cloud.entity_migration import (
    async_reconcile_sensor_entity_registry,
)


def _entry(hass, *, title: str = "Jackery") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=title)
    entry.add_to_hass(hass)
    return entry


def _device(hass, entry: MockConfigEntry, system_id: str):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"system_{system_id}")},
        name=f"System {system_id}",
    )


def _registry_entry(
    hass,
    entry: MockConfigEntry,
    unique_id: str,
    *,
    object_id: str,
    device_id: str | None,
    platform: str = DOMAIN,
    disabled_by: er.RegistryEntryDisabler | None = None,
):
    return er.async_get(hass).async_get_or_create(
        domain="sensor",
        platform=platform,
        unique_id=unique_id,
        config_entry=entry,
        device_id=device_id,
        suggested_object_id=object_id,
        disabled_by=disabled_by,
        original_name=object_id.replace("_", " ").title(),
    )


def _systems(*system_ids: str, source: str | None = None) -> dict:
    return {
        system_id: {
            "monitor": {
                "energyFlowChartVO": {
                    "energyFlowCTVO": {"deviceNo": source},
                    "emsGwVO": {"deviceNo": source},
                    "pvInfo": {"deviceNo": source},
                    "acInfo": {"deviceNo": source},
                    "otherLoadVO": {"deviceNo": source},
                }
            }
        }
        for system_id in system_ids
    }


def _active_entries(hass, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def test_legacy_only_is_migrated_without_changing_entity_or_registry_id(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    legacy = _registry_entry(
        hass,
        entry,
        "ems_TEST_battery_soc",
        object_id="old_battery_soc",
        device_id=device.id,
    )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="ems_TEST")
    )

    canonical = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, sensor_unique_id("SYS1", "battery_soc")
    )
    assert canonical == legacy.entity_id
    migrated = er.async_get(hass).async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.id == legacy.id
    assert migrated.unique_id == "system_SYS1_battery_soc"
    assert result.migrated == 1
    assert result.duplicates_removed == 0


def test_canonical_only_is_unchanged_and_second_run_is_idempotent(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    canonical = _registry_entry(
        hass,
        entry,
        "system_SYS1_battery_soc",
        object_id="battery_soc",
        device_id=device.id,
    )

    first = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1")
    )
    second = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1")
    )

    assert er.async_get(hass).async_get(canonical.entity_id) == canonical
    assert first.migrated == first.duplicates_removed == first.ambiguous == 0
    assert second.migrated == second.duplicates_removed == second.ambiguous == 0
    assert first.already_canonical == second.already_canonical == 1


def test_older_legacy_survives_existing_canonical_conflict(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    with freeze_time("2026-01-01 00:00:00"):
        legacy = _registry_entry(
            hass,
            entry,
            "ems_TEST_grid_power",
            object_id="established_grid_power",
            device_id=device.id,
        )
    with freeze_time("2026-02-01 00:00:00"):
        canonical_duplicate = _registry_entry(
            hass,
            entry,
            "system_SYS1_grid_power",
            object_id="duplicate_grid_power",
            device_id=device.id,
        )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="ems_TEST")
    )

    entries = _active_entries(hass, entry)
    assert len(entries) == 1
    assert entries[0].entity_id == legacy.entity_id
    assert entries[0].id == legacy.id
    assert entries[0].unique_id == "system_SYS1_grid_power"
    assert er.async_get(hass).async_get(canonical_duplicate.entity_id) is None
    assert result.migrated == 1
    assert result.duplicates_removed == 1
    assert not any(
        deleted.unique_id == "system_SYS1_grid_power"
        for deleted in er.async_get(hass).deleted_entities.values()
    )


def test_older_canonical_survives_and_keeps_its_metadata(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    with freeze_time("2026-01-01 00:00:00"):
        canonical = _registry_entry(
            hass,
            entry,
            "system_SYS1_pv_power",
            object_id="established_pv_power",
            device_id=device.id,
        )
    er.async_get(hass).async_update_entity(
        canonical.entity_id,
        name="My PV",
        icon="mdi:solar-power",
    )
    with freeze_time("2026-02-01 00:00:00"):
        legacy = _registry_entry(
            hass,
            entry,
            "pcs_TEST_pv_power",
            object_id="duplicate_pv_power",
            device_id=device.id,
        )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="pcs_TEST")
    )

    survivor = er.async_get(hass).async_get(canonical.entity_id)
    assert survivor is not None
    assert survivor.name == "My PV"
    assert survivor.icon == "mdi:solar-power"
    assert er.async_get(hass).async_get(legacy.entity_id) is None
    assert result.migrated == 0
    assert result.duplicates_removed == 1


def test_n_way_duplicates_keep_the_oldest_entity(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    candidates = []
    for timestamp, prefix in (
        ("2026-01-01 00:00:00", "ems_OLDEST"),
        ("2026-02-01 00:00:00", "ems_NEWER"),
        ("2026-03-01 00:00:00", "system_SYS1"),
    ):
        with freeze_time(timestamp):
            candidates.append(
                _registry_entry(
                    hass,
                    entry,
                    f"{prefix}_other_load_power",
                    object_id=f"{prefix}_other_load_power",
                    device_id=device.id,
                )
            )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1")
    )

    entries = _active_entries(hass, entry)
    assert [(item.entity_id, item.unique_id) for item in entries] == [
        (candidates[0].entity_id, "system_SYS1_other_load_power")
    ]
    assert result.migrated == 1
    assert result.duplicates_removed == 2


def test_multi_system_entries_are_reconciled_without_cross_system_merge(hass):
    entry = _entry(hass)
    device_a = _device(hass, entry, "A")
    device_b = _device(hass, entry, "B")
    legacy_a = _registry_entry(
        hass,
        entry,
        "ems_A_battery_energy_remaining",
        object_id="a_energy",
        device_id=device_a.id,
    )
    legacy_b = _registry_entry(
        hass,
        entry,
        "ems_B_battery_energy_remaining",
        object_id="b_energy",
        device_id=device_b.id,
    )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("A", "B")
    )

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "system_A_battery_energy_remaining"
    ) == legacy_a.entity_id
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "system_B_battery_energy_remaining"
    ) == legacy_b.entity_id
    assert result.migrated == 2
    assert result.duplicates_removed == result.ambiguous == 0


def test_current_rest_sources_map_multi_system_legacy_entries_without_devices(hass):
    entry = _entry(hass)
    legacy_a = _registry_entry(
        hass,
        entry,
        "ems_A_battery_soc",
        object_id="a_soc",
        device_id=None,
    )
    legacy_b = _registry_entry(
        hass,
        entry,
        "ems_B_battery_soc",
        object_id="b_soc",
        device_id=None,
    )
    systems = {
        "A": _systems("A", source="ems_A")["A"],
        "B": _systems("B", source="ems_B")["B"],
    }

    result = async_reconcile_sensor_entity_registry(hass, entry, systems)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "system_A_battery_soc"
    ) == legacy_a.entity_id
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "system_B_battery_soc"
    ) == legacy_b.entity_id
    assert result.migrated == 2
    assert result.ambiguous == 0


def test_single_system_safely_adopts_unmapped_released_legacy_suffix(hass):
    entry = _entry(hass)
    legacy = _registry_entry(
        hass,
        entry,
        "historic_device_other_load_power",
        object_id="historic_home_load",
        device_id=None,
    )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("ONLY")
    )

    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "system_ONLY_other_load_power"
    ) == legacy.entity_id
    assert result.migrated == 1


def test_ambiguous_multi_system_legacy_entry_is_not_modified(hass):
    entry = _entry(hass)
    legacy = _registry_entry(
        hass,
        entry,
        "unknown_grid_power",
        object_id="unknown_grid_power",
        device_id=None,
    )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("A", "B")
    )

    assert er.async_get(hass).async_get(legacy.entity_id) == legacy
    assert result.ambiguous == 1
    assert result.migrated == result.duplicates_removed == 0


def test_other_config_entries_platforms_and_unaffected_sensors_remain_untouched(hass):
    entry = _entry(hass)
    other_entry = _entry(hass, title="Other")
    device = _device(hass, entry, "SYS1")
    unaffected = _registry_entry(
        hass,
        entry,
        "system_SYS1_ac_main_power",
        object_id="ac_main_power",
        device_id=device.id,
    )
    foreign_config = _registry_entry(
        hass,
        other_entry,
        "foreign_grid_power",
        object_id="foreign_grid_power",
        device_id=None,
    )
    foreign_platform = _registry_entry(
        hass,
        entry,
        "ems_TEST_grid_power",
        object_id="other_platform_grid_power",
        device_id=None,
        platform="other_integration",
    )

    async_reconcile_sensor_entity_registry(hass, entry, _systems("SYS1"))

    registry = er.async_get(hass)
    assert registry.async_get(unaffected.entity_id) == unaffected
    assert registry.async_get(foreign_config.entity_id) == foreign_config
    assert registry.async_get(foreign_platform.entity_id) == foreign_platform


def test_disabled_inverted_eps_sensor_keeps_survivor_configuration(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    legacy = _registry_entry(
        hass,
        entry,
        "ac_TEST_eps_load_power_inverted",
        object_id="inverted_socket",
        device_id=device.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )

    async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="ac_TEST")
    )

    survivor = er.async_get(hass).async_get(legacy.entity_id)
    assert survivor is not None
    assert survivor.unique_id == "system_SYS1_eps_load_power_inverted"
    assert survivor.disabled_by == er.RegistryEntryDisabler.INTEGRATION


def test_interrupted_temporary_identity_is_recovered_on_next_setup(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    temporary = _registry_entry(
        hass,
        entry,
        "system_SYS1_eps_load_power"
        f"{ENTITY_MIGRATION_TEMP_MARKER}interrupted",
        object_id="socket_power",
        device_id=device.id,
    )

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1")
    )

    recovered = er.async_get(hass).async_get(temporary.entity_id)
    assert recovered is not None
    assert recovered.unique_id == "system_SYS1_eps_load_power"
    assert result.migrated == 1


def test_failed_conflict_resolution_rolls_canonical_entry_back(hass, monkeypatch):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    with freeze_time("2026-01-01 00:00:00"):
        legacy = _registry_entry(
            hass,
            entry,
            "ems_TEST_battery_soc",
            object_id="legacy_soc",
            device_id=device.id,
        )
    with freeze_time("2026-02-01 00:00:00"):
        canonical = _registry_entry(
            hass,
            entry,
            "system_SYS1_battery_soc",
            object_id="canonical_soc",
            device_id=device.id,
        )

    original_update = er.EntityRegistry.async_update_entity

    def _fail_survivor_update(registry, entity_id, **kwargs):
        if (
            entity_id == legacy.entity_id
            and kwargs.get("new_unique_id") == "system_SYS1_battery_soc"
        ):
            raise ValueError("injected migration failure")
        return original_update(registry, entity_id, **kwargs)

    monkeypatch.setattr(er.EntityRegistry, "async_update_entity", _fail_survivor_update)

    result = async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="ems_TEST")
    )

    registry = er.async_get(hass)
    assert registry.async_get(legacy.entity_id).unique_id == "ems_TEST_battery_soc"
    assert registry.async_get(canonical.entity_id).unique_id == (
        "system_SYS1_battery_soc"
    )
    assert result.ambiguous == 1
    assert result.migrated == result.duplicates_removed == 0


def test_survivor_order_is_deterministic_for_equal_timestamps(hass):
    entry = _entry(hass)
    device = _device(hass, entry, "SYS1")
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    with freeze_time(timestamp):
        legacy = _registry_entry(
            hass,
            entry,
            "ems_TEST_grid_power",
            object_id="legacy_equal_time",
            device_id=device.id,
        )
        _registry_entry(
            hass,
            entry,
            "system_SYS1_grid_power",
            object_id="canonical_equal_time",
            device_id=device.id,
        )

    async_reconcile_sensor_entity_registry(
        hass, entry, _systems("SYS1", source="ems_TEST")
    )

    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "system_SYS1_grid_power"
    ) == legacy.entity_id
