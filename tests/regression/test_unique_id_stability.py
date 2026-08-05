"""AST-based unique_id snapshot for select.py/switch.py/button.py
(Family F: unique_id stability).

Unlike sensor.py (data-driven descriptions) and number.py (class
attributes), these three platforms hardcode their unique_id suffix as a
literal inside an f-string assignment in `__init__`
(`self._attr_unique_id = f"system_{system_id}_<suffix>"`). That can't be
inspected without instantiating a real entity (coordinator + hass), so
this uses the same static-AST approach as
tests/regression/test_rename_safety.py to extract the literal suffix per
class and pin it as a snapshot.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPONENT_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "jackery_home_cloud"
)

# Pinned snapshot: filename -> {class_name: unique_id_suffix}. Renaming any
# of these suffixes changes unique_id for a *shipped* entity and orphans
# it for upgrading users (see CONTRIBUTING.md #6).
EXPECTED_UNIQUE_ID_SUFFIXES = {
    "select.py": {
        "JackeryWorkModeSelect": "work_mode",
        "JackeryOutputPowerLimitSelect": "output_power_limit",
    },
    "switch.py": {
        "JackeryAcOutputSwitch": "ac_output",
        "JackeryStandbySwitch": "standby",
        "JackeryAutoStandbySwitch": "auto_standby",
    },
    "button.py": {
        "JackeryRebootButton": "reboot_device",
    },
}


def _extract_suffix_from_joinedstr(joined: ast.JoinedStr) -> str | None:
    """Given `f"system_{system_id}_<suffix>"`, return "<suffix>" - i.e. the
    literal text following the last interpolated (FormattedValue) part.
    """
    last_formatted_index = None
    for index, part in enumerate(joined.values):
        if isinstance(part, ast.FormattedValue):
            last_formatted_index = index

    if last_formatted_index is None:
        return None

    tail_parts = joined.values[last_formatted_index + 1 :]
    tail = "".join(
        part.value for part in tail_parts if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    return tail.lstrip("_") or None


def _find_unique_id_suffixes(path: Path) -> dict[str, str]:
    """Find, per class in the file, the literal suffix assigned to
    `self._attr_unique_id` inside `__init__` as an f-string.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: dict[str, str] = {}

    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        for member in class_node.body:
            if not (isinstance(member, ast.FunctionDef) and member.name == "__init__"):
                continue
            for stmt in ast.walk(member):
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_attr_unique_id"
                        and isinstance(stmt.value, ast.JoinedStr)
                    ):
                        suffix = _extract_suffix_from_joinedstr(stmt.value)
                        if suffix:
                            result[class_node.name] = suffix
    return result


def test_select_switch_button_unique_id_suffixes_match_snapshot():
    mismatches = {}
    for filename, expected in EXPECTED_UNIQUE_ID_SUFFIXES.items():
        actual = _find_unique_id_suffixes(COMPONENT_DIR / filename)
        if actual != expected:
            mismatches[filename] = {"expected": expected, "actual": actual}

    assert not mismatches, (
        f"unique_id suffix snapshot mismatch: {mismatches}. If this is a "
        f"deliberate rename of a *shipped* entity's unique_id suffix, "
        f"existing installs will get an orphaned + a duplicate entity "
        f"(CONTRIBUTING.md #6). Update EXPECTED_UNIQUE_ID_SUFFIXES only "
        f"after confirming that impact is intended/acceptable."
    )


def test_every_expected_class_still_exists_and_was_detected():
    """Guards against the extraction itself silently finding nothing (e.g.
    if the source switches from an f-string to `.format()`/`%` and the
    snapshot test above would trivially "pass" with an empty actual dict).
    """
    for filename, expected in EXPECTED_UNIQUE_ID_SUFFIXES.items():
        actual = _find_unique_id_suffixes(COMPONENT_DIR / filename)
        assert actual, f"No unique_id suffixes detected at all in {filename} - extraction may be broken."
        assert set(actual) == set(expected), (
            f"{filename}: detected classes {sorted(actual)} do not match "
            f"expected classes {sorted(expected)}."
        )
