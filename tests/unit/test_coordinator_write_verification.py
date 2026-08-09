"""Tests for JackeryHomeCloudCoordinator.async_set_meter_value
(backlog discussion #6, item 17, "Write verification" section):

    stale values are rejected, missing timestamps are rejected, fresh
    matching values succeed, retries use new request timestamps,
    concurrent writes to one meter are serialized, writes to different
    meters remain independent.

This is the method every writable MQTT entity (switch/select/number/
button) ultimately calls to publish a `data_set` command and confirm the
device applied it. It only reads/writes `self.config_entry.runtime_data.
mqtt_client`, `self.data`, `self._mqtt_live_values`, `self._meter_write_
locks`, `self._mqtt_update_events`, and `self.mqtt_system` (via
`is_mqtt_system`) - none of it needs a real `hass`, so the coordinator is
built via `object.__new__` with just those attributes seeded, same pattern
as test_coordinator_bundle_merge.py.

Verification is event-driven (discussion #6, item 6): every fake publisher
below mutates `coordinator.data`/`_mqtt_live_values` synchronously inside
`async_publish_json` (i.e. before it returns), so `async_set_meter_value`'s
immediate post-publish check already sees the new state without ever
needing to actually wait on `_mqtt_update_events` - `verify_delay_seconds=0`
just keeps that timeout budget at zero for attempts that don't confirm.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.jackery_home_cloud.coordinator import (
    JackeryHomeCloudCoordinator,
    JackeryMqttSystem,
)

SYSTEM_ID = "sys1"
METER_ID = "meter1"
BUNDLE_KEY = "work_mode_raw"
TIMESTAMP_KEY = "work_mode_raw_at"
EXPECTED = "02"


class _RecordingPublisher:
    """Fake mqtt_client.async_publish_json with scriptable side effects."""

    def __init__(self, on_call=None):
        self.calls = 0
        self.active = 0
        self.max_concurrent = 0
        self._on_call = on_call

    async def async_publish_json(self, topic, payload, qos=1):
        self.calls += 1
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            if self._on_call is not None:
                await self._on_call(self.calls)
        finally:
            self.active -= 1


def _make_coordinator(
    *,
    mqtt_system_id: str = SYSTEM_ID,
    mqtt_client=None,
    bundle: dict | None = None,
    live_values: dict | None = None,
) -> JackeryHomeCloudCoordinator:
    coordinator = object.__new__(JackeryHomeCloudCoordinator)
    coordinator.mqtt_system = (
        JackeryMqttSystem(system_id=mqtt_system_id, device_serial="irrelevant")
        if mqtt_system_id is not None
        else None
    )
    coordinator.config_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(mqtt_client=mqtt_client)
    )
    coordinator.data = {
        "systems": {SYSTEM_ID: bundle if bundle is not None else {"main_device_serial": "SN1"}}
    }
    coordinator._mqtt_live_values = {SYSTEM_ID: live_values if live_values is not None else {}}
    coordinator._meter_write_locks = {}
    coordinator._mqtt_update_events = {}
    coordinator._mqtt_state = {"publish_count": 0}
    coordinator._mqtt_write_state = {
        "last_confirmed_meter_id": None,
        "last_confirmed_bundle_key": None,
        "last_confirmed_value": None,
        "last_confirmed_at": None,
        "last_error_meter_id": None,
        "last_error_bundle_key": None,
        "last_error_message": None,
        "last_error_at": None,
    }
    return coordinator


async def _set_meter_value(coordinator, publisher, **overrides):
    kwargs = dict(
        system_id=SYSTEM_ID,
        meter_id=METER_ID,
        raw_value="2",
        bundle_key=BUNDLE_KEY,
        timestamp_key=TIMESTAMP_KEY,
        expected_bundle_value=EXPECTED,
        max_attempts=3,
        verify_delay_seconds=0,
    )
    kwargs.update(overrides)
    coordinator.config_entry.runtime_data.mqtt_client = publisher
    return await coordinator.async_set_meter_value(**kwargs)


class TestGuardClauses:
    async def test_no_mqtt_client_raises(self):
        coordinator = _make_coordinator(mqtt_client=None)
        with pytest.raises(HomeAssistantError, match="not available"):
            await coordinator.async_set_meter_value(
                system_id=SYSTEM_ID,
                meter_id=METER_ID,
                raw_value="2",
                bundle_key=BUNDLE_KEY,
                timestamp_key=TIMESTAMP_KEY,
                expected_bundle_value=EXPECTED,
                verify_delay_seconds=0,
            )

    async def test_secondary_system_write_rejected_without_publishing(self):
        publisher = _RecordingPublisher()
        coordinator = _make_coordinator(mqtt_system_id="other-system", mqtt_client=publisher)
        with pytest.raises(HomeAssistantError, match="primary Jackery system"):
            await _set_meter_value(coordinator, publisher)
        assert publisher.calls == 0


class TestFreshMatchingValueSucceeds:
    async def test_confirms_on_first_attempt(self):
        async def on_call(call_number):
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        await _set_meter_value(coordinator, publisher)

        assert publisher.calls == 1


class TestStaleAndMissingTimestamps:
    async def test_stale_value_is_rejected_then_retry_with_fresh_timestamp_succeeds(self):
        """Attempt 1: the bundle already happens to hold the target value
        (from before this write was even issued) with a stale timestamp -
        must NOT be confirmed. Attempt 2: the device "responds" with a
        genuinely fresh timestamp (>= that attempt's own cutoff) - must be
        confirmed. This also pins "retries use new request timestamps":
        the freshness bar is evaluated against each attempt's own cutoff,
        not a fixed one from the first attempt.
        """
        stale_bundle = {"main_device_serial": "SN1", BUNDLE_KEY: EXPECTED}
        stale_live = {TIMESTAMP_KEY: dt_util.utcnow() - timedelta(hours=1)}

        async def on_call(call_number):
            if call_number >= 2:
                coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher, bundle=stale_bundle, live_values=stale_live)

        await _set_meter_value(coordinator, publisher)

        assert publisher.calls == 2

    async def test_missing_timestamp_never_confirms_even_if_value_matches(self):
        bundle = {"main_device_serial": "SN1", BUNDLE_KEY: EXPECTED}
        publisher = _RecordingPublisher()  # never writes a timestamp key
        coordinator = _make_coordinator(mqtt_client=publisher, bundle=bundle, live_values={})

        with pytest.raises(HomeAssistantError, match="did not confirm"):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert publisher.calls == 2


class TestExhaustionPaths:
    async def test_value_never_matches_raises_did_not_confirm(self):
        bundle = {"main_device_serial": "SN1", BUNDLE_KEY: "WRONG"}
        publisher = _RecordingPublisher()
        coordinator = _make_coordinator(mqtt_client=publisher, bundle=bundle)

        with pytest.raises(HomeAssistantError, match="did not confirm meter meter1"):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert publisher.calls == 2

    async def test_publish_failure_every_attempt_raises_with_underlying_error(self):
        async def on_call(call_number):
            raise ConnectionError("broker unreachable")

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        with pytest.raises(HomeAssistantError, match="broker unreachable"):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert publisher.calls == 2


class TestFloatTolerance:
    async def test_value_within_tolerance_matches(self):
        async def on_call(call_number):
            coordinator.data["systems"][SYSTEM_ID]["soc_limit"] = 49.995  # within 0.01 of 50.0
            coordinator._mqtt_live_values[SYSTEM_ID]["soc_limit_at"] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        await _set_meter_value(
            coordinator,
            publisher,
            bundle_key="soc_limit",
            timestamp_key="soc_limit_at",
            expected_bundle_value=50.0,
        )

        assert publisher.calls == 1

    async def test_value_outside_tolerance_does_not_match(self):
        async def on_call(call_number):
            coordinator.data["systems"][SYSTEM_ID]["soc_limit"] = 49.98  # 0.02 away from 50.0
            coordinator._mqtt_live_values[SYSTEM_ID]["soc_limit_at"] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        with pytest.raises(HomeAssistantError, match="did not confirm"):
            await _set_meter_value(
                coordinator,
                publisher,
                bundle_key="soc_limit",
                timestamp_key="soc_limit_at",
                expected_bundle_value=50.0,
                max_attempts=1,
            )


class TestConcurrency:
    async def test_writes_to_same_meter_are_serialized(self):
        async def on_call(call_number):
            await asyncio.sleep(0.02)
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)
        coordinator.config_entry.runtime_data.mqtt_client = publisher

        await asyncio.gather(
            coordinator.async_set_meter_value(
                system_id=SYSTEM_ID,
                meter_id=METER_ID,
                raw_value="2",
                bundle_key=BUNDLE_KEY,
                timestamp_key=TIMESTAMP_KEY,
                expected_bundle_value=EXPECTED,
                verify_delay_seconds=0,
            ),
            coordinator.async_set_meter_value(
                system_id=SYSTEM_ID,
                meter_id=METER_ID,
                raw_value="2",
                bundle_key=BUNDLE_KEY,
                timestamp_key=TIMESTAMP_KEY,
                expected_bundle_value=EXPECTED,
                verify_delay_seconds=0,
            ),
        )

        assert publisher.calls == 2
        assert publisher.max_concurrent == 1  # never overlapped

    async def test_writes_to_different_meters_run_independently(self):
        async def on_call(call_number):
            await asyncio.sleep(0.02)
            coordinator.data["systems"][SYSTEM_ID]["meter_a"] = "A"
            coordinator.data["systems"][SYSTEM_ID]["meter_b"] = "B"
            coordinator._mqtt_live_values[SYSTEM_ID]["meter_a_at"] = dt_util.utcnow()
            coordinator._mqtt_live_values[SYSTEM_ID]["meter_b_at"] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)
        coordinator.config_entry.runtime_data.mqtt_client = publisher

        await asyncio.gather(
            coordinator.async_set_meter_value(
                system_id=SYSTEM_ID,
                meter_id="meter_a",
                raw_value="A",
                bundle_key="meter_a",
                timestamp_key="meter_a_at",
                expected_bundle_value="A",
                verify_delay_seconds=0,
            ),
            coordinator.async_set_meter_value(
                system_id=SYSTEM_ID,
                meter_id="meter_b",
                raw_value="B",
                bundle_key="meter_b",
                timestamp_key="meter_b_at",
                expected_bundle_value="B",
                verify_delay_seconds=0,
            ),
        )

        assert publisher.calls == 2
        assert publisher.max_concurrent == 2  # ran concurrently, not serialized


class TestEventDrivenWakeup:
    """Discussion #6, item 6, "Event-driven write verification": these are
    the only tests in this file where the fake publisher does NOT mutate
    state synchronously inside async_publish_json - the confirming update
    instead lands from a separate task after publish already returned, so
    these actually exercise the asyncio.Event wait path in
    async_set_meter_value rather than resolving on the pre-wait immediate
    check like every other test above.
    """

    async def test_wakes_promptly_on_matching_update_instead_of_sleeping_full_timeout(self):
        """The fix this refactor exists for: confirmation arrives as soon
        as the matching update lands, not after the full verify_delay_seconds
        timeout - even when that timeout is generous."""
        publisher = _RecordingPublisher()
        coordinator = _make_coordinator(mqtt_client=publisher)
        coordinator._mqtt_update_events[SYSTEM_ID] = asyncio.Event()

        async def _deliver_update_shortly():
            await asyncio.sleep(0.05)
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()
            coordinator._mqtt_update_events[SYSTEM_ID].set()

        deliver_task = asyncio.ensure_future(_deliver_update_shortly())
        loop = asyncio.get_running_loop()
        start = loop.time()
        await _set_meter_value(coordinator, publisher, verify_delay_seconds=5.0)
        elapsed = loop.time() - start

        assert publisher.calls == 1
        assert elapsed < 1.0  # woke on the event, did not sleep the full 5s timeout
        await deliver_task

    async def test_unrelated_wakeup_does_not_falsely_confirm_and_keeps_waiting(self):
        """An unrelated MQTT update for the same system (e.g. a different
        meter) sets the same per-system event, waking the wait - but must
        not falsely confirm this write. It should recheck, find no match,
        and keep waiting for the real update within the same attempt's
        timeout budget instead of publishing a retry."""
        publisher = _RecordingPublisher()
        coordinator = _make_coordinator(mqtt_client=publisher)
        coordinator._mqtt_update_events[SYSTEM_ID] = asyncio.Event()

        async def _deliver():
            await asyncio.sleep(0.02)
            coordinator._mqtt_update_events[SYSTEM_ID].set()  # unrelated wake
            await asyncio.sleep(0.02)
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()
            coordinator._mqtt_update_events[SYSTEM_ID].set()

        deliver_task = asyncio.ensure_future(_deliver())
        await _set_meter_value(coordinator, publisher, verify_delay_seconds=2.0)

        assert publisher.calls == 1  # confirmed within the same attempt, no retry publish
        await deliver_task


class TestWriteStateTracking:
    """Discussion #6 Phase 3, item 13 ("Improve MQTT diagnostics"): a
    successful/failed write records itself into coordinator._mqtt_write_state
    for diagnostics.py to read - purely additive, must never influence the
    confirmation logic itself (CONTRIBUTING.md #4)."""

    async def test_confirmed_write_records_last_confirmed_state(self):
        async def on_call(call_number):
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        await _set_meter_value(coordinator, publisher)

        assert coordinator._mqtt_write_state["last_confirmed_meter_id"] == METER_ID
        assert coordinator._mqtt_write_state["last_confirmed_bundle_key"] == BUNDLE_KEY
        assert coordinator._mqtt_write_state["last_confirmed_value"] == EXPECTED
        assert coordinator._mqtt_write_state["last_confirmed_at"] is not None
        assert coordinator._mqtt_write_state["last_error_at"] is None

    async def test_exhausted_retries_records_last_error_state(self):
        bundle = {"main_device_serial": "SN1", BUNDLE_KEY: "WRONG"}
        publisher = _RecordingPublisher()
        coordinator = _make_coordinator(mqtt_client=publisher, bundle=bundle)

        with pytest.raises(HomeAssistantError):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert coordinator._mqtt_write_state["last_error_meter_id"] == METER_ID
        assert coordinator._mqtt_write_state["last_error_bundle_key"] == BUNDLE_KEY
        assert "did not confirm" in coordinator._mqtt_write_state["last_error_message"]
        assert coordinator._mqtt_write_state["last_error_at"] is not None
        assert coordinator._mqtt_write_state["last_confirmed_at"] is None

    async def test_publish_failure_records_last_error_state(self):
        async def on_call(call_number):
            raise ConnectionError("broker unreachable")

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        with pytest.raises(HomeAssistantError):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert coordinator._mqtt_write_state["last_error_meter_id"] == METER_ID
        assert "broker unreachable" in coordinator._mqtt_write_state["last_error_message"]

    async def test_publish_count_increments_per_successful_publish(self):
        async def on_call(call_number):
            if call_number >= 2:
                coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
                coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        await _set_meter_value(coordinator, publisher)

        assert coordinator._mqtt_state["publish_count"] == 2

    async def test_publish_count_does_not_increment_on_publish_failure(self):
        async def on_call(call_number):
            raise ConnectionError("broker unreachable")

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)

        with pytest.raises(HomeAssistantError):
            await _set_meter_value(coordinator, publisher, max_attempts=2)

        assert coordinator._mqtt_state["publish_count"] == 0


class TestRefreshGroupCallback:
    async def test_refresh_group_runs_on_success(self):
        async def on_call(call_number):
            coordinator.data["systems"][SYSTEM_ID][BUNDLE_KEY] = EXPECTED
            coordinator._mqtt_live_values[SYSTEM_ID][TIMESTAMP_KEY] = dt_util.utcnow()

        publisher = _RecordingPublisher(on_call=on_call)
        coordinator = _make_coordinator(mqtt_client=publisher)
        refresh_calls = []

        async def refresh_group():
            refresh_calls.append(1)

        await _set_meter_value(coordinator, publisher, refresh_group=refresh_group)

        assert refresh_calls == [1]

    async def test_refresh_group_runs_even_on_failure(self):
        publisher = _RecordingPublisher()  # value never matches -> exhausts and raises
        coordinator = _make_coordinator(mqtt_client=publisher, bundle={"main_device_serial": "SN1"})
        refresh_calls = []

        async def refresh_group():
            refresh_calls.append(1)

        with pytest.raises(HomeAssistantError):
            await _set_meter_value(coordinator, publisher, max_attempts=1, refresh_group=refresh_group)

        assert refresh_calls == [1]
