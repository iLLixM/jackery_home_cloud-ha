"""Async REST client for Jackery Home Cloud."""

from __future__ import annotations

import base64
import asyncio
from collections.abc import Mapping
import json
import logging
from typing import Any

import aiohttp

from ..const import (
    API_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_BASE_URL,
    DEFAULT_ENCRYPTED,
    MQTT_DEFAULT_PORT,
    TREND_TYPE_DAY,
)
from ..crypto_utils import decrypt_text
from ..exceptions import JackeryHomeApiError, JackeryHomeAuthError
from .auth import build_auth_headers, build_base_headers, build_login_payload

_LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT_SECONDS)
_AUTH_ERROR_MARKERS = ("token", "login", "auth", "expired", "unauthorized")


class JackeryApiClient:
    """Minimal async client for the Jackery Home Cloud REST API.

    The client intentionally keeps responses as dictionaries so the integration
    can evolve quickly while the reverse engineered API is still changing.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_prefix: str = "Bearer"

    @property
    def access_token(self) -> str | None:
        """Return the currently cached access token, if available."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return the currently cached refresh token, if available.

        The token is stored for a future refresh-token workflow. The currently
        reverse engineered integration still performs a full re-login when the
        access token becomes invalid.
        """
        return self._refresh_token

    async def async_login(
        self,
        account: str,
        password: str,
        phone_uid: str,
        encrypted: bool = DEFAULT_ENCRYPTED,
    ) -> dict[str, Any]:
        """Authenticate and cache the returned access token."""
        payload = build_login_payload(
            account=account,
            password=password,
            phone_uid=phone_uid,
            encrypted=encrypted,
        )
        data = await self._request(
            method="POST",
            path="/geneverse-iot-home/v1/home/auth/login",
            headers=build_base_headers(),
            json_body=payload,
        )

        result = data.get("result") or {}
        access_token = result.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise JackeryHomeAuthError(
                "Login succeeded but the API did not return an access token."
            )

        self._access_token = access_token
        refresh_token = result.get("refreshToken")
        self._refresh_token = refresh_token if isinstance(refresh_token, str) else None
        token_prefix = result.get("tokenPrefix")
        self._token_prefix = token_prefix if isinstance(token_prefix, str) else "Bearer"
        return result

    async def async_get_app_user(self) -> dict[str, Any]:
        """Fetch the app user profile."""
        data = await self._request(
            method="GET",
            path="/geneverse-iot-home/v1/appUser/getOne",
            headers=self._auth_headers(),
        )
        return self._result_dict(data)

    async def async_list_systems(self) -> list[dict[str, Any]]:
        """Return all systems visible to the current account."""
        data = await self._request(
            method="GET",
            path="/geneverse-iot-home/v1/system/listByUserV2",
            headers=self._auth_headers(),
        )
        return self._result_list(data)

    async def async_get_monitor(self, system_id: str | None = None) -> dict[str, Any]:
        """Return the monitor snapshot for a system.

        The app also uses this endpoint without a system id for the default
        system. The integration always passes a system id for deterministic
        multi-system behaviour.
        """
        payload: dict[str, Any] = {}
        if system_id is not None:
            payload["systemId"] = system_id

        data = await self._request(
            method="POST",
            path="/geneverse-iot-home/v1/app/monitor/",
            headers=self._auth_headers(),
            json_body=payload,
        )
        return self._result_dict(data)

    async def async_get_devices_by_system(self, system_id: str) -> list[dict[str, Any]]:
        """Return all devices for the given system id."""
        data = await self._request(
            method="GET",
            path=f"/geneverse-iot-home/v2/home/device/bySystemId/{system_id}",
            headers=self._auth_headers(),
        )
        return self._result_list(data)

    async def async_get_device_detail(self, device_no: str) -> dict[str, Any]:
        """Fetch detailed metadata for a single Jackery device.

        The detail response is used for Device Info enrichment, especially
        result.baseVO.softVer for the main device firmware.
        """
        data = await self._request(
            method="GET",
            path=f"/geneverse-iot-home/v1/home/device/detail?deviceNo={device_no}",
            headers=self._auth_headers(),
        )
        return self._result_dict(data)

    async def async_get_mqtt_credentials(self) -> dict[str, Any]:
        """Fetch MQTT connection metadata from the Jackery REST API.

        Prefer the newer v2 endpoint, which may already provide ready-to-use
        plaintext MQTT credentials. Fall back to the legacy v1 endpoint if
        v2 is unavailable or incomplete.
        """
        endpoints = (
            ("/geneverse-iot-home/v2/idc/config/mqttServer", True, "v2"),
            ("/geneverse-iot-home/v1/idc/config/mqttServer", False, "v1"),
        )

        last_error: Exception | None = None
        for path, password_is_plaintext, source_endpoint in endpoints:
            try:
                data = await self._request(
                    method="GET",
                    path=path,
                    headers=self._auth_headers(),
                )
                result = self._result_dict(data)
                if result.get("mqttServer") and result.get("mqttUserName") and result.get("mqttPassword"):
                    result["_password_is_plaintext"] = password_is_plaintext
                    result["_source_endpoint"] = source_endpoint
                    _LOGGER.debug(
                        "Jackery MQTT config retrieved via %s endpoint; password_is_plaintext=%s",
                        source_endpoint,
                        password_is_plaintext,
                    )
                    return result
                _LOGGER.debug(
                    "Jackery MQTT config via %s endpoint was incomplete: %s",
                    source_endpoint,
                    result,
                )
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "Jackery MQTT config request via %s endpoint failed: %s",
                    source_endpoint,
                    err,
                )

        if last_error is not None:
            raise last_error
        raise JackeryHomeApiError("Unable to retrieve MQTT configuration from Jackery API.")

    async def async_build_mqtt_credentials(
        self,
        crypto_key: str | None = None,
        mqtt_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build ready-to-use MQTT credentials from the REST API response.

        The newer v2 endpoint may return plaintext MQTT credentials directly.
        The legacy v1 endpoint still returns an encoded credential value that
        requires the optional integration crypto key for decoding.
        """
        raw = dict(mqtt_config or await self.async_get_mqtt_credentials())
        host = str(raw.get("mqttServer") or "").strip()
        username = str(raw.get("mqttUserName") or "").strip()
        mqtt_secret = str(raw.get("mqttPassword") or "")
        password_is_plaintext = bool(raw.get("_password_is_plaintext"))
        source_endpoint = str(raw.get("_source_endpoint") or "unknown")

        try:
            port = int(raw.get("mqttPort") or MQTT_DEFAULT_PORT)
        except (TypeError, ValueError):
            port = MQTT_DEFAULT_PORT

        _LOGGER.debug(
            "Jackery MQTT server info from API: host=%s port=%s username=%s source_endpoint=%s password_is_plaintext=%s raw=%s",
            host,
            port,
            username,
            source_endpoint,
            password_is_plaintext,
            raw,
        )

        if not host or not username or not mqtt_secret:
            raise JackeryHomeApiError(
                "The MQTT configuration response did not contain all required fields."
            )

        password = mqtt_secret
        if password_is_plaintext:
            _LOGGER.debug(
                "Jackery MQTT credentials from %s endpoint are ready to use without crypto decode.",
                source_endpoint,
            )
        else:
            crypto_key = (crypto_key or "").strip()
            _LOGGER.debug(
                "Jackery legacy MQTT credential flow selected; crypto key available=%s",
                bool(crypto_key),
            )
            if not crypto_key:
                raise JackeryHomeCryptoError(
                    "Legacy MQTT credential flow requires an integration crypto key."
                )
            try:
                password = decrypt_text(mqtt_secret, crypto_key)
            except Exception as err:
                _LOGGER.debug(
                    "Jackery legacy MQTT credential decode failed. host=%s port=%s username=%s source_endpoint=%s encoded_value=%s crypto_key=%s error=%s",
                    host,
                    port,
                    username,
                    source_endpoint,
                    mqtt_secret,
                    crypto_key,
                    err,
                )
                raise

        _LOGGER.debug("Jackery resolved MQTT credential value: %s", password)

        return {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "tls": True,
            "raw": raw,
        }

    async def async_get_cluster_trend_daily(
        self,
        system_id: str,
        day_key: str,
        trend_type: str = TREND_TYPE_DAY,
    ) -> dict[str, Any]:
        """Fetch daily cluster trend data for a system.

        The reverse engineered mobile app uses this endpoint for the historical
        trend chart that contains PV, grid, and battery energy buckets.
        """
        data = await self._request(
            method="POST",
            path="/geneverse-iot-home/v2/app/trend/cluster/sta",
            headers=self._auth_headers(),
            json_body={
                "startTime": day_key,
                "endTime": day_key,
                "type": trend_type,
                "systemId": system_id,
            },
        )
        return self._result_dict(data)

    async def async_get_battery_bms_trend_daily(
        self,
        system_id: str,
        day_key: str,
        trend_type: str = TREND_TYPE_DAY,
    ) -> dict[str, Any]:
        """Fetch daily battery BMS trend totals for a system."""
        data = await self._request(
            method="POST",
            path="/geneverse-iot-home/v1/app/trend/battery/bms/",
            headers=self._auth_headers(),
            json_body={
                "startTime": day_key,
                "endTime": day_key,
                "type": trend_type,
                "systemId": system_id,
            },
        )
        return self._result_dict(data)

    def _auth_headers(self) -> dict[str, str]:
        """Return headers for authenticated requests."""
        if not self._access_token:
            raise JackeryHomeAuthError("No access token is available. Login is required.")
        return build_auth_headers(self._access_token, self._token_prefix)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a single REST request and validate the common API envelope."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("Jackery API request %s %s", method, path)

        try:
            async with self._session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                body_text = await response.text()
        except asyncio.TimeoutError as err:
            raise JackeryHomeApiError(f"Timeout during {method} {path}") from err
        except aiohttp.ClientError as err:
            raise JackeryHomeApiError(
                f"Connection error during {method} {path}: {err}"
            ) from err

        try:
            data = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError as err:
            raise JackeryHomeApiError(
                f"Invalid JSON received from {path}: {body_text[:200]}"
            ) from err

        if response.status in (401, 403):
            raise JackeryHomeAuthError(
                f"Authentication failed for {path}: HTTP {response.status}"
            )

        if response.status >= 400:
            message = data.get("msg") if isinstance(data, dict) else None
            raise JackeryHomeApiError(
                f"HTTP {response.status} from {path}: {message or body_text[:200]}"
            )

        if not isinstance(data, dict):
            raise JackeryHomeApiError(f"Unexpected response structure from {path}")

        if not data.get("success", False):
            code = data.get("code")
            message = str(data.get("msg", "Unknown API error"))
            lowered = message.lower()
            if code in {401, 403} or any(marker in lowered for marker in _AUTH_ERROR_MARKERS):
                raise JackeryHomeAuthError(message)
            raise JackeryHomeApiError(message)

        return data

    @staticmethod
    def _result_dict(data: dict[str, Any]) -> dict[str, Any]:
        """Return the result payload as a dictionary."""
        result = data.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise JackeryHomeApiError("Expected a dictionary in API result")
        return result

    @staticmethod
    def _result_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the result payload as a list of dictionaries."""
        result = data.get("result")
        if result is None:
            return []
        if not isinstance(result, list):
            raise JackeryHomeApiError("Expected a list in API result")
        return [item for item in result if isinstance(item, dict)]
