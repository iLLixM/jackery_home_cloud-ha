"""Regression tests for the two rename-related failure modes documented in
CONTRIBUTING.md section 6 ("Renaming: the rule that matters most here"):

1. A `const.py` constant is imported by another module (`from .const import
   X`) but no longer defined in `const.py` -> ImportError at integration
   load, breaking every platform since `coordinator.py` is imported by all
   of them.
2. A "bundle" dict key is read by an entity platform module
   (`bundle.get("some_key")`) but never actually produced anywhere in
   `coordinator.py` -> no crash, but the entity silently reads `None`
   forever.

Both checks are static (AST-based): they do not import Home Assistant or
run the integration, so they also work as a fast pre-commit-style guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "jackery_home_cloud"
)

PLATFORM_FILES = (
    "coordinator.py",
    "number.py",
    "select.py",
    "sensor.py",
    "switch.py",
    "button.py",
)

BUNDLE_VAR_NAMES = {"bundle", "merged", "live"}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _const_defined_names() -> set[str]:
    """Every name assigned at module level in const.py."""
    defined: set[str] = set()
    tree = _parse(COMPONENT_DIR / "const.py")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    return defined


def _const_imports(tree: ast.Module) -> list[str]:
    """Every name imported via `from .const import ...` (level-1 relative)."""
    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "const"
        ):
            names.extend(alias.name for alias in node.names)
    return names


class TestConstImportCompleteness:
    """Guards regression #1: every `from .const import X` must resolve."""

    @pytest.mark.parametrize("filename", PLATFORM_FILES)
    def test_all_const_imports_are_defined(self, filename):
        defined = _const_defined_names()
        tree = _parse(COMPONENT_DIR / filename)
        imported = _const_imports(tree)

        missing = [name for name in imported if name not in defined]
        assert not missing, (
            f"{filename} imports the following names from const.py that no "
            f"longer exist there: {missing}. This would raise ImportError "
            f"at integration load for every platform."
        )

    def test_api_modules_const_imports_are_defined(self):
        """api/client.py and api/auth.py import from ..const (level 2)."""
        defined = _const_defined_names()
        for filename in ("client.py", "auth.py"):
            path = COMPONENT_DIR / "api" / filename
            tree = _parse(path)
            imported = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.level == 2
                and node.module == "const"
                for alias in node.names
            ]
            missing = [name for name in imported if name not in defined]
            assert not missing, (
                f"api/{filename} imports the following names from const.py "
                f"that no longer exist there: {missing}."
            )


def _string_dict_keys_written(tree: ast.Module) -> set[str]:
    """Collect string keys that are plausibly *produced* into a bundle-like
    dict in coordinator.py:

    - `merged["key"] = ...` / `bundle["key"] = ...` / `live["key"] = ...`
    - any dict literal key, e.g. `{"key": value, ...}` (covers the common
      pattern of building a partial `live` dict as a literal before
      merging it).
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in BUNDLE_VAR_NAMES
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _bundle_get_keys_read(tree: ast.Module) -> set[str]:
    """Collect string keys read off a variable named `bundle` via
    `.get("key")` or `["key"]`, anywhere in a platform module (typically
    inside `value_fn=lambda bundle: ...`).
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bundle"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "bundle"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


# Keys that are legitimately read from the bundle but produced somewhere
# other than a literal/subscript assignment pattern the static scan can
# see (e.g. copied wholesale from the REST API response, or produced via
# a helper/loop the AST scan intentionally doesn't attempt to model).
# Keep this list short and documented - it is the "known gap" of this
# static check, not a general escape hatch.
KNOWN_NON_LITERAL_PRODUCERS = {
    "system",  # top-level REST system bundle, not a coordinator-authored key
    "monitor",  # nested REST payload sub-dict, not a coordinator-authored key
    "main_device_firmware",
    "battery_count",
    "total_battery_capacity_kwh",
    "daily_energy",
    # Experimental power meters are produced by the
    # experimental_power_values loop in coordinator ingestion. The loop uses
    # bundle_key as a variable, so this intentionally simple AST scan cannot
    # connect its literal tuple entries to updated.update(). PCS active power
    # L1 is validated and produced explicitly, so it does not belong here.
    "pcs_apparent_power_mqtt",
    "pcs_active_power_mqtt",
    "ems_other_load_power_l1_mqtt",
    "ems_on_grid_power_mqtt",
}


class TestBundleKeyProducerConsumerConsistency:
    """Guards regression #2: every `bundle.get("key")` consumer in a
    platform file must correspond to a key actually produced in
    coordinator.py (or be explicitly documented as a known static-analysis
    gap above).
    """

    @pytest.mark.parametrize(
        "filename", ("sensor.py", "number.py", "select.py", "switch.py", "button.py")
    )
    def test_bundle_keys_read_are_produced_by_coordinator(self, filename):
        coordinator_tree = _parse(COMPONENT_DIR / "coordinator.py")
        produced = _string_dict_keys_written(coordinator_tree) | KNOWN_NON_LITERAL_PRODUCERS

        consumer_tree = _parse(COMPONENT_DIR / filename)
        consumed = _bundle_get_keys_read(consumer_tree)

        missing = sorted(consumed - produced)
        assert not missing, (
            f"{filename} reads the following bundle key(s) via "
            f"bundle.get(...) that coordinator.py never appears to "
            f"produce: {missing}. Either coordinator.py no longer writes "
            f"this key (silent None regression - see CONTRIBUTING.md #6), "
            f"or it is a legitimate static-analysis gap that should be "
            f"added to KNOWN_NON_LITERAL_PRODUCERS with a comment."
        )
