"""Tests for MQTT-backed entity setup gating across number/select/switch/
button/sensor (backlog discussion #6, item 17, "Entity behavior" section):

    MQTT-only entities exist only for the primary system, secondary
    systems retain REST entities.

Each of number.py/select.py/switch.py/button.py's `async_setup_entry`
never reads `hass` in its body (verified by grep - zero `hass.` usages),
only `entry.options`/`entry.runtime_data` and the coordinator passed
through `runtime_data.coordinator`. So `hass` is passed as `None` below,
and `entry`/`coordinator` are lightweight `SimpleNamespace`/fake objects
- no real Home Assistant runtime needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.jackery_home_cloud import button, number, select, switch
from custom_components.jackery_home_cloud.const import (
    CONF_ENABLE_MQTT,
    MQTT_EMS_AUTO_STANDBY_METER_ID,
    MQTT_EMS_FEED_POWER_LIMIT_METER_ID,
    MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
    MQTT_EMS_REBOOT_METER_ID,
)
from custom_components.jackery_home_cloud.sensor import (
    SYSTEM_SENSOR_DESCRIPTIONS,
    JackeryScheduleSensor,
    _build_entities,
)

PRIMARY = "sys-primary"
SECONDARY = "sys-secondary"
EXPERIMENTAL_POWER_SENSOR_KEYS = {
    "pcs_apparent_power",
    "pcs_active_power",
    "ems_other_load_power_l1",
    "ems_on_grid_power",
}
OPTIONAL_POWER_DIAGNOSTIC_SENSOR_KEYS = EXPERIMENTAL_POWER_SENSOR_KEYS | {
    "pcs_active_power_l1",
}
PV_INPUT_SENSOR_KEYS = {"pv1_power", "pv2_power"}


class _FakeCoordinator:
    def __init__(
        self,
        data: dict,
        mqtt_system_id: str | None,
        *,
        model_confirmed: bool = False,
        unsupported_meter_ids: frozenset[str] = frozenset(),
    ):
        self.data = data
        self._mqtt_system_id = mqtt_system_id
        # Mirrors JackeryHomeCloudCoordinator.is_model_confirmed()/
        # supports_meter() (discussion #6, item 9, "Device capability and
        # model detection"). Defaults match today's real-world default: no
        # `factoryModel` in the fixture bundles below -> unconfirmed model
        # -> every meter "supported" (see coordinator.py's fallback
        # policy), which is exactly what the existing entity-count
        # assertions below already pin.
        self._model_confirmed = model_confirmed
        self._unsupported_meter_ids = unsupported_meter_ids

    def is_mqtt_system(self, system_id: str) -> bool:
        return self._mqtt_system_id is not None and str(system_id) == self._mqtt_system_id

    def is_model_confirmed(self, system_id: str) -> bool:
        return self.is_mqtt_system(system_id) and self._model_confirmed

    def supports_meter(self, system_id: str, meter_id: str) -> bool:
        if not self.is_mqtt_system(system_id):
            return False
        return meter_id not in self._unsupported_meter_ids


def _bundle(serial: str) -> dict:
    return {"main_device_serial": serial}


def _entry(*, enable_mqtt: bool, mqtt_client, coordinator) -> SimpleNamespace:
    return SimpleNamespace(
        options={CONF_ENABLE_MQTT: enable_mqtt},
        runtime_data=SimpleNamespace(mqtt_client=mqtt_client, coordinator=coordinator),
    )


async def _collect(async_setup_entry, entry) -> list:
    collected: list = []
    await async_setup_entry(None, entry, collected.extend)
    return collected


MQTT_PLATFORMS = [
    ("number", number.async_setup_entry, 3),
    ("select", select.async_setup_entry, 2),
    ("switch", switch.async_setup_entry, 3),
    ("button", button.async_setup_entry, 1),
]


class TestMqttPlatformsGating:
    async def test_mqtt_disabled_creates_no_entities(self):
        for _, setup_fn, _count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator({"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY)
            entry = _entry(enable_mqtt=False, mqtt_client=object(), coordinator=coordinator)
            assert await _collect(setup_fn, entry) == []

    async def test_no_mqtt_client_creates_no_entities(self):
        for _, setup_fn, _count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator({"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY)
            entry = _entry(enable_mqtt=True, mqtt_client=None, coordinator=coordinator)
            assert await _collect(setup_fn, entry) == []

    async def test_primary_system_with_resolvable_serial_creates_expected_entity_count(self):
        for name, setup_fn, expected_count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator({"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY)
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            entities = await _collect(setup_fn, entry)
            assert len(entities) == expected_count, f"{name}: expected {expected_count}, got {len(entities)}"

    async def test_primary_system_without_resolvable_serial_creates_no_entities(self):
        for _, setup_fn, _count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator({"systems": {PRIMARY: {}}}, mqtt_system_id=PRIMARY)
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            assert await _collect(setup_fn, entry) == []

    async def test_secondary_system_creates_no_mqtt_entities_even_with_resolvable_serial(self):
        for _, setup_fn, _count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator(
                {"systems": {SECONDARY: _bundle("SN2")}}, mqtt_system_id=PRIMARY
            )
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            assert await _collect(setup_fn, entry) == []

    async def test_mixed_primary_and_secondary_only_primary_gets_entities(self):
        for name, setup_fn, expected_count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator(
                {"systems": {PRIMARY: _bundle("SN1"), SECONDARY: _bundle("SN2")}},
                mqtt_system_id=PRIMARY,
            )
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            entities = await _collect(setup_fn, entry)
            assert len(entities) == expected_count, f"{name}: expected {expected_count}, got {len(entities)}"


class TestSensorBuildEntitiesGating:
    def test_requires_mqtt_sensors_only_created_for_primary_system(self):
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1"), SECONDARY: _bundle("SN2")}},
            mqtt_system_id=PRIMARY,
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        requires_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if d.requires_mqtt}
        primary_requires_mqtt = [
            e for e in entities
            if getattr(e, "_system_id", None) == PRIMARY and getattr(e, "entity_description", None) is not None
            and e.entity_description.key in requires_mqtt_keys
        ]
        secondary_requires_mqtt = [
            e for e in entities
            if getattr(e, "_system_id", None) == SECONDARY and getattr(e, "entity_description", None) is not None
            and e.entity_description.key in requires_mqtt_keys
        ]
        assert len(primary_requires_mqtt) == len(requires_mqtt_keys)
        assert secondary_requires_mqtt == []

    def test_pv_input_sensors_are_enabled_only_for_confirmed_mqtt_primary(self):
        coordinator = _FakeCoordinator(
            {
                "systems": {
                    PRIMARY: _bundle("SN1"),
                    SECONDARY: _bundle("SN2"),
                }
            },
            mqtt_system_id=PRIMARY,
            model_confirmed=True,
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)
        pv_entities = [
            entity
            for entity in entities
            if getattr(entity, "entity_description", None) is not None
            and entity.entity_description.key in PV_INPUT_SENSOR_KEYS
        ]

        assert {entity.entity_description.key for entity in pv_entities} == (
            PV_INPUT_SENSOR_KEYS
        )
        assert {entity._system_id for entity in pv_entities} == {PRIMARY}
        assert all(entity.entity_registry_enabled_default for entity in pv_entities)

    def test_rest_sensors_created_for_every_selected_system(self):
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1"), SECONDARY: _bundle("SN2")}},
            mqtt_system_id=PRIMARY,
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        non_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if not d.requires_mqtt}
        for system_id in (PRIMARY, SECONDARY):
            keys_for_system = {
                e.entity_description.key
                for e in entities
                if getattr(e, "_system_id", None) == system_id and getattr(e, "entity_description", None) is not None
            }
            assert non_mqtt_keys <= keys_for_system

    def test_schedule_sensor_only_created_for_primary_system(self):
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1"), SECONDARY: _bundle("SN2")}},
            mqtt_system_id=PRIMARY,
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        schedule_entities = [e for e in entities if isinstance(e, JackeryScheduleSensor)]
        assert len(schedule_entities) == 1
        assert schedule_entities[0]._system_id == PRIMARY

    def test_mqtt_disabled_at_entry_level_creates_no_requires_mqtt_sensors(self):
        coordinator = _FakeCoordinator({"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY)

        entities = _build_entities(coordinator, mqtt_enabled=False)

        requires_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if d.requires_mqtt}
        present_keys = {
            e.entity_description.key for e in entities if getattr(e, "entity_description", None) is not None
        }
        assert not (present_keys & requires_mqtt_keys)


# One representative gated meter id per platform, matched to the class that
# disappears when that meter isn't in a confirmed model's capability set
# (discussion #6, item 9, "Device capability and model detection").
PLATFORM_GATED_METER = {
    "number": MQTT_EMS_FEED_POWER_LIMIT_METER_ID,
    "select": MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
    "switch": MQTT_EMS_AUTO_STANDBY_METER_ID,
    "button": MQTT_EMS_REBOOT_METER_ID,
}


class TestCapabilityGating:
    async def test_confirmed_model_with_full_capability_set_creates_all_entities_enabled(self):
        """A confirmed model whose capability set covers every meter behaves
        exactly like today (no entity dropped, none disabled-by-default)."""
        for name, setup_fn, expected_count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator(
                {"systems": {PRIMARY: _bundle("SN1")}},
                mqtt_system_id=PRIMARY,
                model_confirmed=True,
            )
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            entities = await _collect(setup_fn, entry)
            assert len(entities) == expected_count, f"{name}: expected {expected_count}, got {len(entities)}"
            for entity in entities:
                assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True

    async def test_confirmed_model_missing_one_meter_drops_exactly_that_entity(self):
        for name, setup_fn, expected_count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator(
                {"systems": {PRIMARY: _bundle("SN1")}},
                mqtt_system_id=PRIMARY,
                model_confirmed=True,
                unsupported_meter_ids=frozenset({PLATFORM_GATED_METER[name]}),
            )
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            entities = await _collect(setup_fn, entry)
            assert len(entities) == expected_count - 1, (
                f"{name}: expected {expected_count - 1}, got {len(entities)}"
            )

    async def test_unconfirmed_model_creates_all_entities_disabled_by_default(self):
        """Unconfirmed models are not reduced (no regression for users on a
        model that happens to work but isn't listed yet) - they only get
        entity_registry_enabled_default=False so a user must opt in."""
        for name, setup_fn, expected_count in MQTT_PLATFORMS:
            coordinator = _FakeCoordinator(
                {"systems": {PRIMARY: _bundle("SN1")}},
                mqtt_system_id=PRIMARY,
                model_confirmed=False,
            )
            entry = _entry(enable_mqtt=True, mqtt_client=object(), coordinator=coordinator)
            entities = await _collect(setup_fn, entry)
            assert len(entities) == expected_count, f"{name}: expected {expected_count}, got {len(entities)}"
            for entity in entities:
                assert entity._attr_entity_registry_enabled_default is False


class TestSensorCapabilityGating:
    def test_confirmed_model_keeps_optional_power_diagnostics_disabled(self):
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY, model_confirmed=True
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        requires_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if d.requires_mqtt}
        for entity in entities:
            description = getattr(entity, "entity_description", None)
            if description is not None and description.key in requires_mqtt_keys:
                if description.key in OPTIONAL_POWER_DIAGNOSTIC_SENSOR_KEYS:
                    assert entity.entity_registry_enabled_default is False
                else:
                    # Preserve the original capability-gating assertion: a
                    # confirmed model must not dynamically disable ordinary
                    # MQTT sensors. Individual descriptions may still be
                    # intentionally optional for unrelated product reasons.
                    assert (
                        getattr(
                            entity,
                            "_attr_entity_registry_enabled_default",
                            True,
                        )
                        is True
                    )

    def test_unconfirmed_model_requires_mqtt_sensors_and_schedule_sensor_are_disabled_by_default(self):
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY, model_confirmed=False
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        requires_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if d.requires_mqtt}
        requires_mqtt_entities = [
            e
            for e in entities
            if getattr(e, "entity_description", None) is not None
            and e.entity_description.key in requires_mqtt_keys
        ]
        assert requires_mqtt_entities
        for entity in requires_mqtt_entities:
            assert entity._attr_entity_registry_enabled_default is False

        pv_entities = [
            entity
            for entity in requires_mqtt_entities
            if entity.entity_description.key in PV_INPUT_SENSOR_KEYS
        ]
        assert {entity.entity_description.key for entity in pv_entities} == (
            PV_INPUT_SENSOR_KEYS
        )

        schedule_entities = [e for e in entities if isinstance(e, JackeryScheduleSensor)]
        assert len(schedule_entities) == 1
        assert schedule_entities[0]._attr_entity_registry_enabled_default is False

    def test_unconfirmed_model_rest_only_sensors_stay_enabled_by_default(self):
        """Only requires_mqtt sensors are affected - plain REST sensors have
        no capability question (they don't depend on MQTT meter support)."""
        coordinator = _FakeCoordinator(
            {"systems": {PRIMARY: _bundle("SN1")}}, mqtt_system_id=PRIMARY, model_confirmed=False
        )

        entities = _build_entities(coordinator, mqtt_enabled=True)

        non_mqtt_keys = {d.key for d in SYSTEM_SENSOR_DESCRIPTIONS if not d.requires_mqtt}
        for entity in entities:
            description = getattr(entity, "entity_description", None)
            if description is not None and description.key in non_mqtt_keys:
                assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True
