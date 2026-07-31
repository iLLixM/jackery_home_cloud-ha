"""MQTT client foundation for Jackery Home Cloud.

The MQTT layer is introduced in v0.2.3 as a safe architecture-preparation step.
It establishes the cloud connection, subscribes to known topic families, and
forwards raw messages into Home Assistant for inspection. Productive entity
mapping is intentionally deferred to later versions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
import ssl
from typing import Any

from homeassistant.core import HomeAssistant

from .exceptions import JackeryHomeMqttError
from .mqtt_parser import parse_mqtt_payload
from .mqtt_registry import build_default_subscriptions

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

_LOGGER = logging.getLogger(__name__)

MqttMessageCallback = Callable[[dict[str, Any]], Awaitable[None]]
MqttStatusCallback = Callable[[dict[str, Any]], Awaitable[None]]


class JackeryMqttClient:
    """Small wrapper around paho-mqtt for the Jackery cloud broker."""

    def __init__(
        self,
        hass: HomeAssistant,
        credentials: dict[str, Any],
        device_serial: str,
        *,
        debug_raw: bool,
        tls_insecure: bool = False,
        message_callback: MqttMessageCallback,
        status_callback: MqttStatusCallback,
    ) -> None:
        self._hass = hass
        self._credentials = credentials
        self._device_serial = str(device_serial).strip()
        self._debug_raw = debug_raw
        self._tls_insecure = tls_insecure
        self._message_callback = message_callback
        self._status_callback = status_callback
        self._client: Any | None = None
        self._started = False
        self._connected = False

        if mqtt is None:
            raise JackeryHomeMqttError(
                "The paho-mqtt package is not available in this runtime."
            )

    async def async_start(self) -> None:
        """Connect to the MQTT broker and start the background loop."""
        if self._started:
            return
        await self._hass.async_add_executor_job(self._sync_start)
        self._started = True

    async def async_stop(self) -> None:
        """Stop the MQTT loop and close the broker connection."""
        if not self._started:
            return
        await self._hass.async_add_executor_job(self._sync_stop)
        self._started = False

    def _sync_start(self) -> None:
        """Start the blocking paho-mqtt client in an executor thread."""
        client = mqtt.Client(protocol=mqtt.MQTTv311)
        client.enable_logger(_LOGGER)
        client.username_pw_set(
            str(self._credentials["username"]),
            str(self._credentials["password"]),
        )

        host = str(self._credentials["host"])
        port = int(self._credentials["port"])

        _LOGGER.debug(
            "Starting Jackery MQTT client for host=%s port=%s tls_insecure=%s",
            host,
            port,
            self._tls_insecure,
        )

        if self._tls_insecure:
            # Explicitly disable certificate validation for troubleshooting or
            # compatibility with expired/invalid broker certificates.
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            _LOGGER.warning(
                "MQTT TLS insecure mode enabled: certificate validation and hostname verification are disabled."
            )
        else:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            client.tls_insecure_set(False)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        client.connect(
            host,
            port,
            keepalive=60,
        )
        client.loop_start()

    def _sync_stop(self) -> None:
        """Stop the blocking paho-mqtt client."""
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        self._connected = False

    def _on_connect(self, client: Any, _: Any, __: Any, rc: int) -> None:
        self._connected = rc == 0
        status = {
            "connected": rc == 0,
            "connection_state": "connected" if rc == 0 else "error",
            "error": None if rc == 0 else f"MQTT connect failed with rc={rc}",
        }
        if rc == 0:
            subscriptions = build_default_subscriptions(self._device_serial)
            _LOGGER.debug(
                "Jackery MQTT targeted subscriptions for %s: %s",
                self._device_serial,
                subscriptions,
            )
            for topic, qos in subscriptions:
                client.subscribe(topic, qos)
        self._schedule_status(status)

    def _on_disconnect(self, _: Any, __: Any, rc: int) -> None:
        self._connected = False
        self._schedule_status(
            {
                "connected": False,
                "connection_state": "disconnected",
                "error": None if rc == 0 else f"MQTT disconnected with rc={rc}",
            }
        )

    def _on_message(self, _: Any, __: Any, message: Any) -> None:
        parsed = parse_mqtt_payload(message.topic, message.payload)
        if self._debug_raw:
            _LOGGER.debug("Jackery MQTT raw message on %s: %s", message.topic, parsed)
        self._schedule_message(parsed)

    def _schedule_message(self, message: dict[str, Any]) -> None:
        self._hass.loop.call_soon_threadsafe(
            self._hass.async_create_task,
            self._message_callback(message),
        )


    async def async_publish_json(self, topic: str, payload: dict[str, Any], *, qos: int = 1) -> None:
        """Publish a JSON payload to the MQTT broker."""
        await self._hass.async_add_executor_job(self._sync_publish_json, topic, payload, qos)

    def _sync_publish_json(self, topic: str, payload: dict[str, Any], qos: int = 1) -> None:
        """Publish a JSON payload using the blocking paho client."""
        if self._client is None or not self._started or not self._connected:
            raise JackeryHomeMqttError("MQTT client is not connected.")

        payload_text = json.dumps(payload, separators=(",", ":"))
        _LOGGER.debug("Publishing Jackery MQTT command to %s: %s", topic, payload_text)
        info = self._client.publish(topic, payload_text, qos=qos)
        info.wait_for_publish()
        if getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) != mqtt.MQTT_ERR_SUCCESS:
            raise JackeryHomeMqttError(
                f"MQTT publish failed with rc={getattr(info, 'rc', 'unknown')}"
            )

    def _schedule_status(self, status: dict[str, Any]) -> None:
        self._hass.loop.call_soon_threadsafe(
            self._hass.async_create_task,
            self._status_callback(status),
        )
