"""Entity-registry reconciliation for legacy Jackery sensor identities."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .entity_identity import (
    ENTITY_MIGRATION_TEMP_MARKER,
    LEGACY_SOURCE_SCOPED_SENSOR_KEYS,
    sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)

_LEGACY_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "grid_power": (
        "monitor",
        "energyFlowChartVO",
        "energyFlowCTVO",
        "deviceNo",
    ),
    "battery_soc": (
        "monitor",
        "energyFlowChartVO",
        "emsGwVO",
        "deviceNo",
    ),
    "battery_energy_remaining": (
        "monitor",
        "energyFlowChartVO",
        "emsGwVO",
        "deviceNo",
    ),
    "pv_power": (
        "monitor",
        "energyFlowChartVO",
        "pvInfo",
        "deviceNo",
    ),
    "eps_load_power": (
        "monitor",
        "energyFlowChartVO",
        "acInfo",
        "deviceNo",
    ),
    "eps_load_power_inverted": (
        "monitor",
        "energyFlowChartVO",
        "acInfo",
        "deviceNo",
    ),
    "other_load_power": (
        "monitor",
        "energyFlowChartVO",
        "otherLoadVO",
        "deviceNo",
    ),
}


@dataclass(frozen=True, slots=True)
class EntityRegistryReconciliationResult:
    """Summary of one entity-registry reconciliation pass."""

    migrated: int = 0
    duplicates_removed: int = 0
    already_canonical: int = 0
    ambiguous: int = 0


@callback
def async_reconcile_sensor_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    systems: Mapping[str, Any],
) -> EntityRegistryReconciliationResult:
    """Reconcile legacy source-scoped sensors to stable system identities.

    The operation is intentionally checked on every setup rather than being
    gated only by a config-entry version. That makes partial upgrades and
    interrupted duplicate cleanup self-healing while keeping ambiguous
    multi-system states untouched.
    """
    system_bundles = {
        str(system_id): bundle
        for system_id, bundle in systems.items()
        if isinstance(bundle, Mapping)
    }
    if not system_bundles:
        return EntityRegistryReconciliationResult()

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    registry_entries = [
        registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if registry_entry.domain == "sensor"
        and registry_entry.platform == DOMAIN
    ]

    device_system_ids: dict[str, set[str]] = defaultdict(set)
    for system_id in system_bundles:
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"system_{system_id}")}
        )
        if device is not None:
            device_system_ids[device.id].add(system_id)

    # Older registries may not expose the system identifier on the device
    # anymore. Any already-canonical entity remains a safe secondary mapping
    # from that HA device back to its Jackery system.
    for registry_entry in registry_entries:
        if registry_entry.device_id is None:
            continue
        matching_system_ids = [
            system_id
            for system_id in system_bundles
            if registry_entry.unique_id.startswith(f"system_{system_id}_")
        ]
        if matching_system_ids:
            # Prefer the longest exact prefix so a hypothetical system "A"
            # cannot also claim an entity belonging to system "A_B".
            device_system_ids[registry_entry.device_id].add(
                max(matching_system_ids, key=len)
            )

    current_legacy_ids: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for system_id, bundle in system_bundles.items():
        for key, path in _LEGACY_SOURCE_PATHS.items():
            source = _safe_get(bundle, *path)
            if source is None:
                continue
            source_text = str(source).strip()
            if source_text:
                current_legacy_ids[f"{source_text}_{key}"].add(
                    (system_id, key)
                )

    groups: dict[tuple[str, str], list[er.RegistryEntry]] = defaultdict(list)
    ambiguous = 0
    for registry_entry in registry_entries:
        direct_identity = _direct_identity(
            registry_entry.unique_id,
            system_bundles,
        )
        if direct_identity is not None:
            groups[direct_identity].append(registry_entry)
            continue

        key = _legacy_sensor_key(registry_entry.unique_id)
        if key is None:
            continue

        candidate_system_ids: set[str] = set()
        if registry_entry.device_id is not None:
            candidate_system_ids.update(
                device_system_ids.get(registry_entry.device_id, set())
            )
        candidate_system_ids.update(
            system_id
            for system_id, candidate_key in current_legacy_ids.get(
                registry_entry.unique_id, set()
            )
            if candidate_key == key
        )
        if not candidate_system_ids and len(system_bundles) == 1:
            candidate_system_ids.add(next(iter(system_bundles)))

        if len(candidate_system_ids) != 1:
            ambiguous += 1
            _LOGGER.warning(
                "Could not safely reconcile Jackery sensor identity: "
                "key=%s candidate_entity=%s candidate_system_ids=%s",
                key,
                registry_entry.entity_id,
                sorted(candidate_system_ids),
            )
            continue

        groups[(candidate_system_ids.pop(), key)].append(registry_entry)

    migrated = 0
    duplicates_removed = 0
    already_canonical = 0
    for (system_id, key), candidates in groups.items():
        canonical_unique_id = sensor_unique_id(system_id, key)
        if (
            len(candidates) == 1
            and candidates[0].unique_id == canonical_unique_id
        ):
            already_canonical += 1
            continue

        try:
            group_migrated, group_removed = _reconcile_group(
                registry,
                system_id=system_id,
                key=key,
                candidates=candidates,
            )
        except (KeyError, ValueError) as err:
            _LOGGER.warning(
                "Failed to reconcile Jackery sensor identity for system_id=%s "
                "key=%s candidates=%s: %s",
                system_id,
                key,
                sorted(candidate.entity_id for candidate in candidates),
                err,
            )
            ambiguous += 1
            continue

        migrated += group_migrated
        duplicates_removed += group_removed

    result = EntityRegistryReconciliationResult(
        migrated=migrated,
        duplicates_removed=duplicates_removed,
        already_canonical=already_canonical,
        ambiguous=ambiguous,
    )
    if migrated or duplicates_removed or ambiguous:
        _LOGGER.info(
            "Jackery entity registry reconciliation completed: migrated=%d "
            "duplicates_removed=%d already_canonical=%d ambiguous=%d",
            result.migrated,
            result.duplicates_removed,
            result.already_canonical,
            result.ambiguous,
        )
    return result


@callback
def _reconcile_group(
    registry: er.EntityRegistry,
    *,
    system_id: str,
    key: str,
    candidates: list[er.RegistryEntry],
) -> tuple[int, int]:
    """Reconcile one unambiguous logical sensor group."""
    canonical_unique_id = sensor_unique_id(system_id, key)
    survivor = min(candidates, key=_survivor_sort_key)
    survivor_requires_migration = survivor.unique_id != canonical_unique_id
    canonical_entry = next(
        (
            candidate
            for candidate in candidates
            if candidate.unique_id == canonical_unique_id
        ),
        None,
    )
    moved_canonical: er.RegistryEntry | None = None

    if survivor.unique_id != canonical_unique_id:
        if canonical_entry is not None:
            temporary_unique_id = _available_temporary_unique_id(
                registry,
                canonical_unique_id,
                canonical_entry,
            )
            moved_canonical = registry.async_update_entity(
                canonical_entry.entity_id,
                new_unique_id=temporary_unique_id,
            )
        try:
            survivor = registry.async_update_entity(
                survivor.entity_id,
                new_unique_id=canonical_unique_id,
            )
        except (KeyError, ValueError):
            # Keep the registry usable if the second step fails. A process
            # interruption cannot be caught, so temporary IDs are also
            # recognized as candidates during the next setup pass.
            if moved_canonical is not None:
                registry.async_update_entity(
                    moved_canonical.entity_id,
                    new_unique_id=canonical_unique_id,
                )
            raise

    removed_entity_ids: list[str] = []
    for candidate in candidates:
        if candidate.entity_id == survivor.entity_id:
            continue
        registry.async_remove(candidate.entity_id)
        removed_entity_ids.append(candidate.entity_id)

    _LOGGER.info(
        "Reconciled Jackery sensor entity: system_id=%s key=%s "
        "survivor=%s removed=%s",
        system_id,
        key,
        survivor.entity_id,
        sorted(removed_entity_ids),
    )
    return (1 if survivor_requires_migration else 0, len(removed_entity_ids))


def _direct_identity(
    unique_id: str,
    systems: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Return an identity encoded by a canonical or temporary unique ID."""
    for system_id in systems:
        for key in LEGACY_SOURCE_SCOPED_SENSOR_KEYS:
            canonical = sensor_unique_id(system_id, key)
            if unique_id == canonical or unique_id.startswith(
                f"{canonical}{ENTITY_MIGRATION_TEMP_MARKER}"
            ):
                return system_id, key
    return None


def _legacy_sensor_key(unique_id: str) -> str | None:
    """Return the released source-scoped sensor key encoded in a unique ID."""
    if unique_id.startswith("system_"):
        return None
    for key in sorted(LEGACY_SOURCE_SCOPED_SENSOR_KEYS, key=len, reverse=True):
        prefix, separator, suffix = unique_id.rpartition(f"_{key}")
        if separator and prefix and not suffix:
            return key
    return None


def _survivor_sort_key(registry_entry: er.RegistryEntry) -> tuple[float, int, str]:
    """Prefer the oldest entry, then a legacy entry, then entity_id."""
    created_at = registry_entry.created_at
    created_order = (
        created_at.timestamp()
        if isinstance(created_at, datetime)
        else float("inf")
    )
    canonical_rank = 1 if registry_entry.unique_id.startswith("system_") else 0
    return created_order, canonical_rank, registry_entry.entity_id


def _available_temporary_unique_id(
    registry: er.EntityRegistry,
    canonical_unique_id: str,
    registry_entry: er.RegistryEntry,
) -> str:
    """Return a deterministic unused temporary unique ID."""
    base = (
        f"{canonical_unique_id}{ENTITY_MIGRATION_TEMP_MARKER}"
        f"{registry_entry.id}"
    )
    candidate = base
    index = 1
    while registry.async_get_entity_id("sensor", DOMAIN, candidate) is not None:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _safe_get(data: Any, *path: str) -> Any:
    """Safely traverse a nested mapping."""
    current = data
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
