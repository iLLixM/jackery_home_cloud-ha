"""Stable entity identity helpers for Jackery Home Cloud."""

from __future__ import annotations


# These released sensors previously used an optional REST ``deviceNo`` as
# their unique-ID prefix. The keys are retained here only so setup-time
# reconciliation can identify and migrate those legacy registry entries.
LEGACY_SOURCE_SCOPED_SENSOR_KEYS: frozenset[str] = frozenset(
    {
        "grid_power",
        "battery_soc",
        "battery_energy_remaining",
        "pv_power",
        "eps_load_power",
        "eps_load_power_inverted",
        "other_load_power",
    }
)

ENTITY_MIGRATION_TEMP_MARKER = "__duplicate_cleanup_"


def sensor_unique_id(system_id: str, key: str) -> str:
    """Return the canonical system-scoped unique ID for a metric sensor."""
    return f"system_{system_id}_{key}"
