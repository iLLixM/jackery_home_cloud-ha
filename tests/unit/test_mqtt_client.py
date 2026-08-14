"""Unit tests for mqtt_client.py (Family D: MQTT client, mocked paho).

`JackeryMqttClient` only needs a real `homeassistant.core.HomeAssistant`
for three thin bits of glue: `async_add_executor_job` (runs blocking paho
calls off the event loop) and `loop.call_soon_threadsafe` /
`async_create_task` (hands parsed messages/status back to the event loop
from paho's own background thread). None of that requires the `hass`
fixture from `pytest-homeassistant-custom-component` - a tiny `_FakeHass`
that runs the executor job inline and schedules the callback coroutine on
the *current* event loop (since these tests are themselves async) is
enough to exercise the real production code paths. The blocking paho
`mqtt.Client` itself is mocked so `_sync_start` never opens a real socket.
"""

from __future__ import annotations

import asyncio
import json
import ssl

import paho.mqtt.client as mqtt
import pytest

from custom_components.jackery_home_cloud import mqtt_client as mqtt_client_module
from custom_components.jackery_home_cloud.exceptions import JackeryHomeMqttError
from custom_components.jackery_home_cloud.mqtt_client import JackeryMqttClient

CREDENTIALS = {"host": "broker.test", "port": 8883, "username": "user1", "password": "secret"}
DEVICE_SERIAL = "SN123"


class _FakeHass:
    """Enough of HomeAssistant for JackeryMqttClient's glue code."""

    def __init__(self) -> None:
        self.loop = self
        self.created_tasks: list[asyncio.Task] = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def call_soon_threadsafe(self, fn, *args) -> None:
        fn(*args)

    def async_create_task(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.created_tasks.append(task)
        return task


class _Recorder:
    """Async callback stand-in that records every payload it receives."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.calls.append(payload)


def _make_client(hass=None, **overrides) -> JackeryMqttClient:
    kwargs = {
        "credentials": CREDENTIALS,
        "device_serial": DEVICE_SERIAL,
        "debug_raw": False,
        "tls_insecure": False,
        "message_callback": _Recorder(),
        "status_callback": _Recorder(),
    }
    kwargs.update(overrides)
    return JackeryMqttClient(hass or _FakeHass(), kwargs.pop("credentials"), kwargs.pop("device_serial"), **kwargs)


async def _flush() -> None:
    """Let tasks scheduled via `async_create_task` run to completion."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestInit:
    def test_raises_when_paho_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(mqtt_client_module, "mqtt", None)
        with pytest.raises(JackeryHomeMqttError, match="paho-mqtt"):
            _make_client()

    def test_device_serial_is_stripped(self):
        client = _make_client(device_serial="  SN123  ")
        assert client._device_serial == "SN123"

    def test_publish_count_starts_at_zero(self):
        client = _make_client()
        assert client.publish_count == 0


class TestOnConnect:
    async def test_success_subscribes_and_reports_connected(self):
        hass = _FakeHass()
        status = _Recorder()
        client = _make_client(hass, status_callback=status)
        paho_client = mqtt.Client()
        subscribed: list[tuple[str, int]] = []
        paho_client.subscribe = lambda topic, qos: subscribed.append((topic, qos))

        client._on_connect(paho_client, None, None, 0)
        await _flush()

        assert client._connected is True
        assert len(subscribed) == 2  # LWT + gateway data topics
        assert all(DEVICE_SERIAL in topic for topic, _ in subscribed)
        assert status.calls == [
            {"connected": True, "connection_state": "connected", "error": None}
        ]

    async def test_failure_does_not_subscribe_and_reports_error(self):
        hass = _FakeHass()
        status = _Recorder()
        client = _make_client(hass, status_callback=status)
        paho_client = mqtt.Client()
        subscribed: list[tuple[str, int]] = []
        paho_client.subscribe = lambda topic, qos: subscribed.append((topic, qos))

        client._on_connect(paho_client, None, None, 5)
        await _flush()

        assert client._connected is False
        assert subscribed == []
        assert status.calls == [
            {
                "connected": False,
                "connection_state": "error",
                "error": "MQTT connect failed with rc=5",
            }
        ]


class TestOnDisconnect:
    async def test_clean_disconnect_reports_no_error(self):
        hass = _FakeHass()
        status = _Recorder()
        client = _make_client(hass, status_callback=status)
        client._connected = True

        client._on_disconnect(None, None, 0)
        await _flush()

        assert client._connected is False
        assert status.calls == [
            {"connected": False, "connection_state": "disconnected", "error": None}
        ]

    async def test_unexpected_disconnect_reports_rc_in_error(self):
        hass = _FakeHass()
        status = _Recorder()
        client = _make_client(hass, status_callback=status)

        client._on_disconnect(None, None, 7)
        await _flush()

        assert status.calls[0]["error"] == "MQTT disconnected with rc=7"


class TestOnMessage:
    async def test_parses_and_forwards_payload(self):
        hass = _FakeHass()
        message_cb = _Recorder()
        client = _make_client(hass, message_callback=message_cb)

        class _RawMessage:
            topic = "jackery/dev/SN123/gw/data"
            payload = json.dumps({"dev_sn": "SN123", "value": 42}).encode("utf-8")

        client._on_message(None, None, _RawMessage())
        await _flush()

        assert len(message_cb.calls) == 1
        parsed = message_cb.calls[0]
        assert parsed["topic"] == "jackery/dev/SN123/gw/data"
        assert parsed["dev_sn"] == "SN123"
        assert parsed["payload_json"] == {"dev_sn": "SN123", "value": 42}


class TestSyncStart:
    def test_configures_and_connects_paho_client(self, monkeypatch):
        created: list = []

        class _FakeMqttClient:
            def __init__(self, protocol=None):
                self.protocol = protocol
                self.username_pw_set_args = None
                self.tls_set_args = None
                self.tls_insecure_arg = None
                self.connect_args = None
                self.loop_started = False
                created.append(self)

            def enable_logger(self, logger):
                pass

            def username_pw_set(self, username, password):
                self.username_pw_set_args = (username, password)

            def tls_set(self, cert_reqs=None):
                self.tls_set_args = cert_reqs

            def tls_insecure_set(self, value):
                self.tls_insecure_arg = value

            def connect(self, host, port, keepalive=60):
                self.connect_args = (host, port, keepalive)

            def loop_start(self):
                self.loop_started = True

        monkeypatch.setattr(mqtt_client_module.mqtt, "Client", _FakeMqttClient)

        client = _make_client(tls_insecure=False)
        client._sync_start()

        fake = created[0]
        assert fake.protocol == mqtt_client_module.mqtt.MQTTv311
        assert fake.username_pw_set_args == ("user1", "secret")
        assert fake.tls_set_args == ssl.CERT_REQUIRED
        assert fake.tls_insecure_arg is False
        assert fake.connect_args == ("broker.test", 8883, 60)
        assert fake.loop_started is True
        assert client._client is fake
        assert fake.on_connect == client._on_connect
        assert fake.on_disconnect == client._on_disconnect
        assert fake.on_message == client._on_message

    def test_tls_insecure_disables_certificate_validation(self, monkeypatch):
        created: list = []

        class _FakeMqttClient:
            def __init__(self, protocol=None):
                created.append(self)

            def enable_logger(self, logger):
                pass

            def username_pw_set(self, username, password):
                pass

            def tls_set(self, cert_reqs=None):
                self.tls_set_args = cert_reqs

            def tls_insecure_set(self, value):
                self.tls_insecure_arg = value

            def connect(self, host, port, keepalive=60):
                pass

            def loop_start(self):
                pass

        monkeypatch.setattr(mqtt_client_module.mqtt, "Client", _FakeMqttClient)

        client = _make_client(tls_insecure=True)
        client._sync_start()

        fake = created[0]
        assert fake.tls_set_args == ssl.CERT_NONE
        assert fake.tls_insecure_arg is True


class TestAsyncStartStopIdempotency:
    async def test_async_start_only_calls_sync_start_once(self):
        hass = _FakeHass()
        client = _make_client(hass)
        call_count = 0

        def _fake_sync_start():
            nonlocal call_count
            call_count += 1

        client._sync_start = _fake_sync_start

        await client.async_start()
        await client.async_start()

        assert call_count == 1
        assert client._started is True

    async def test_async_stop_is_noop_when_not_started(self):
        hass = _FakeHass()
        client = _make_client(hass)

        def _fail():
            raise AssertionError("_sync_stop should not be called when never started")

        client._sync_stop = _fail

        await client.async_stop()  # never started - must not call _sync_stop
        assert client._started is False

    async def test_async_stop_only_calls_sync_stop_once(self):
        hass = _FakeHass()
        client = _make_client(hass)
        client._started = True
        call_count = 0

        def _fake_sync_stop():
            nonlocal call_count
            call_count += 1

        client._sync_stop = _fake_sync_stop

        await client.async_stop()
        await client.async_stop()

        assert call_count == 1
        assert client._started is False


class TestSyncStop:
    def test_noop_when_no_client(self):
        client = _make_client()
        client._sync_stop()  # must not raise

    def test_stops_loop_and_disconnects(self):
        client = _make_client()

        class _FakePahoClient:
            def __init__(self):
                self.loop_stopped = False
                self.disconnected = False

            def loop_stop(self):
                self.loop_stopped = True

            def disconnect(self):
                self.disconnected = True

        fake = _FakePahoClient()
        client._client = fake
        client._connected = True

        client._sync_stop()

        assert fake.loop_stopped is True
        assert fake.disconnected is True
        assert client._client is None
        assert client._connected is False


class TestSyncPublishJson:
    def test_raises_when_no_client(self):
        client = _make_client()
        with pytest.raises(JackeryHomeMqttError, match="not connected"):
            client._sync_publish_json("some/topic", {"a": 1})

    def test_raises_when_not_started(self):
        client = _make_client()
        client._client = object()
        client._connected = True
        with pytest.raises(JackeryHomeMqttError, match="not connected"):
            client._sync_publish_json("some/topic", {"a": 1})

    def test_raises_when_not_connected(self):
        client = _make_client()
        client._client = object()
        client._started = True
        client._connected = False
        with pytest.raises(JackeryHomeMqttError, match="not connected"):
            client._sync_publish_json("some/topic", {"a": 1})

    def test_publishes_compact_json_and_waits(self):
        client = _make_client()
        client._started = True
        client._connected = True

        published: dict = {}

        class _FakeInfo:
            rc = mqtt.MQTT_ERR_SUCCESS

            def wait_for_publish(self):
                published["waited"] = True

        class _FakePahoClient:
            def publish(self, topic, payload_text, qos):
                published["topic"] = topic
                published["payload_text"] = payload_text
                published["qos"] = qos
                return _FakeInfo()

        client._client = _FakePahoClient()

        client._sync_publish_json("cmd/topic", {"b": 1, "a": 2}, qos=2)

        assert published["topic"] == "cmd/topic"
        assert published["payload_text"] == json.dumps({"b": 1, "a": 2}, separators=(",", ":"))
        assert published["qos"] == 2
        assert published["waited"] is True
        assert client.publish_count == 1

    def test_raises_when_broker_reports_failure_rc(self):
        client = _make_client()
        client._started = True
        client._connected = True

        class _FakeInfo:
            rc = mqtt.MQTT_ERR_NO_CONN

            def wait_for_publish(self):
                pass

        class _FakePahoClient:
            def publish(self, topic, payload_text, qos):
                return _FakeInfo()

        client._client = _FakePahoClient()

        with pytest.raises(JackeryHomeMqttError, match="rc="):
            client._sync_publish_json("cmd/topic", {"a": 1})

        assert client.publish_count == 0

    def test_publish_count_accumulates_across_calls_regardless_of_caller(self):
        """publish_count must reflect every successful publish through this
        client, not just ones the coordinator initiated - this is what lets
        diagnostics count publishes from entity platforms (number/select/
        switch/button) too, not only the coordinator's own two paths."""
        client = _make_client()
        client._started = True
        client._connected = True

        class _FakeInfo:
            rc = mqtt.MQTT_ERR_SUCCESS

            def wait_for_publish(self):
                pass

        class _FakePahoClient:
            def publish(self, topic, payload_text, qos):
                return _FakeInfo()

        client._client = _FakePahoClient()

        client._sync_publish_json("cmd/topic", {"a": 1})
        client._sync_publish_json("cmd/topic", {"a": 2})
        client._sync_publish_json("cmd/topic", {"a": 3})

        assert client.publish_count == 3


class TestAsyncPublishJson:
    async def test_delegates_to_executor_with_same_args(self):
        hass = _FakeHass()
        client = _make_client(hass)
        client._started = True
        client._connected = True
        seen = {}

        def _fake_sync_publish(topic, payload, qos=1):
            seen["args"] = (topic, payload, qos)

        client._sync_publish_json = _fake_sync_publish

        await client.async_publish_json("cmd/topic", {"a": 1}, qos=2)

        assert seen["args"] == ("cmd/topic", {"a": 1}, 2)
