"""Unit tests for api/client.py (Family B: API client, mocked HTTP).

aiohttp responses are faked via a tiny in-repo stub (`_FakeSession`)
instead of the `aioresponses` library listed in requirements-test.txt:
aioresponses 0.7.9 (the latest release on PyPI) is incompatible with the
aiohttp version `pytest-homeassistant-custom-component` pulls in on this
environment - aiohttp 3.14.3's `ClientResponse.__init__` now requires a
`stream_writer` kwarg that aioresponses does not pass, so any aioresponses
call raises `TypeError` before the mocked request even runs (verified
2026-08-05). The fake only implements the async-context-manager protocol
that `JackeryApiClient._request` actually uses, so it stays correct
regardless of aiohttp's internal response constructor.
"""

from __future__ import annotations

import asyncio
import base64
import json as json_module

import aiohttp
import pytest

from custom_components.jackery_home_cloud.api.client import JackeryApiClient
from custom_components.jackery_home_cloud.const import MQTT_DEFAULT_PORT
from custom_components.jackery_home_cloud.crypto_utils import encrypt_text
from custom_components.jackery_home_cloud.exceptions import (
    JackeryCryptoError,
    JackeryHomeApiError,
    JackeryHomeAuthError,
    JackeryHomeCryptoError,
)

BASE_URL = "https://api.jackery.test"
VALID_KEY = "0123456789abcdef"  # exactly 16 bytes once UTF-8 encoded


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequestCM:
    """Stands in for aiohttp's `_RequestContextManager`."""

    def __init__(self, response: _FakeResponse | None, exception: Exception | None) -> None:
        self._response = response
        self._exception = exception

    async def __aenter__(self) -> _FakeResponse:
        if self._exception is not None:
            raise self._exception
        assert self._response is not None
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession that scripts one response per
    call (in order) and records every call made against it.

    Pass either a single `status`/`body`/`exception` used for every call,
    or a `script` list of `{"status": ..., "body": ..., "exception": ...}`
    dicts consumed one per call - the latter is needed for methods like
    `async_get_mqtt_credentials` that issue more than one request.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: str = "",
        exception: Exception | None = None,
        script: list[dict] | None = None,
    ) -> None:
        self._script = list(script) if script is not None else None
        self._default = {"status": status, "body": body, "exception": exception}
        self.calls: list[dict] = []

    def request(self, *, method, url, headers, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json})
        step = self._script.pop(0) if self._script else self._default
        exception = step.get("exception")
        response = None if exception is not None else _FakeResponse(step.get("status", 200), step.get("body", ""))
        return _FakeRequestCM(response=response, exception=exception)


def _ok_body(result=None, **extra) -> str:
    body = {"success": True, "code": 0, "result": result}
    body.update(extra)
    return json_module.dumps(body)


def _fail_body(msg: str = "Something failed", code: int = 500, **extra) -> str:
    body = {"success": False, "code": code, "msg": msg}
    body.update(extra)
    return json_module.dumps(body)


def _client(session: _FakeSession) -> JackeryApiClient:
    return JackeryApiClient(session, base_url=BASE_URL)


def _authed_client(session: _FakeSession) -> JackeryApiClient:
    client = _client(session)
    client._access_token = "test-token"
    return client


class TestAuthHeadersGuard:
    async def test_authenticated_call_without_login_raises_and_makes_no_request(self):
        session = _FakeSession()
        client = _client(session)
        with pytest.raises(JackeryHomeAuthError):
            await client.async_get_app_user()
        assert session.calls == []


class TestLogin:
    async def test_success_caches_tokens_and_returns_result(self):
        result = {"accessToken": "tok-1", "refreshToken": "ref-1", "tokenPrefix": "Token"}
        session = _FakeSession(body=_ok_body(result))
        client = _client(session)

        returned = await client.async_login("user@example.com", "pw", "phone-1")

        assert returned == result
        assert client.access_token == "tok-1"
        assert client.refresh_token == "ref-1"
        assert session.calls[0]["method"] == "POST"
        assert session.calls[0]["url"] == f"{BASE_URL}/geneverse-iot-home/v1/home/auth/login"

    async def test_missing_access_token_raises_and_does_not_cache(self):
        session = _FakeSession(body=_ok_body({"refreshToken": "ref-1"}))
        client = _client(session)

        with pytest.raises(JackeryHomeAuthError):
            await client.async_login("u", "p", "phone")
        assert client.access_token is None

    async def test_missing_refresh_token_and_prefix_use_defaults(self):
        session = _FakeSession(body=_ok_body({"accessToken": "tok"}))
        client = _client(session)

        await client.async_login("u", "p", "phone")

        assert client.refresh_token is None
        # Default "Bearer" prefix must flow into subsequent auth headers.
        client._session = _FakeSession(body=_ok_body({}))
        await client.async_get_app_user()
        assert client._session.calls[0]["headers"]["authorization"] == "Bearer tok"

    async def test_non_string_refresh_token_is_ignored(self):
        session = _FakeSession(body=_ok_body({"accessToken": "tok", "refreshToken": 12345}))
        client = _client(session)

        await client.async_login("u", "p", "phone")

        assert client.refresh_token is None


class TestRequestErrorHandling:
    async def test_timeout_raises_api_error(self):
        session = _FakeSession(exception=asyncio.TimeoutError())
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="Timeout"):
            await client.async_get_app_user()

    async def test_connection_error_raises_api_error(self):
        session = _FakeSession(exception=aiohttp.ClientConnectionError("boom"))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="Connection error"):
            await client.async_get_app_user()

    async def test_invalid_json_body_raises_api_error(self):
        session = _FakeSession(status=200, body="not-json{")
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="Invalid JSON"):
            await client.async_get_app_user()

    async def test_http_401_raises_auth_error(self):
        session = _FakeSession(status=401, body=_fail_body())
        client = _authed_client(session)
        with pytest.raises(JackeryHomeAuthError):
            await client.async_get_app_user()

    async def test_http_403_raises_auth_error(self):
        session = _FakeSession(status=403, body=_fail_body())
        client = _authed_client(session)
        with pytest.raises(JackeryHomeAuthError):
            await client.async_get_app_user()

    async def test_http_5xx_with_json_message_raises_api_error_with_message(self):
        session = _FakeSession(status=500, body=json_module.dumps({"msg": "server exploded"}))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="server exploded"):
            await client.async_get_app_user()

    async def test_http_5xx_with_non_dict_body_falls_back_to_raw_text(self):
        session = _FakeSession(status=500, body=json_module.dumps(["raw", "error", "list"]))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="raw"):
            await client.async_get_app_user()

    async def test_success_status_with_non_dict_body_raises_unexpected_structure(self):
        session = _FakeSession(status=200, body=json_module.dumps([1, 2, 3]))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="Unexpected response structure"):
            await client.async_get_app_user()

    async def test_envelope_failure_with_auth_code_raises_auth_error(self):
        # HTTP 200 but the app-level envelope reports its own 401 - the
        # two error channels (HTTP status vs. envelope `code`) are
        # independent and both must be handled.
        session = _FakeSession(status=200, body=_fail_body(msg="nope", code=401))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeAuthError, match="nope"):
            await client.async_get_app_user()

    async def test_envelope_failure_with_auth_marker_in_message_raises_auth_error(self):
        session = _FakeSession(status=200, body=_fail_body(msg="Login token expired", code=999))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeAuthError):
            await client.async_get_app_user()

    async def test_envelope_failure_generic_raises_api_error(self):
        session = _FakeSession(status=200, body=_fail_body(msg="Something else broke", code=999))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="Something else broke"):
            await client.async_get_app_user()


class TestResultHelpers:
    async def test_get_app_user_returns_empty_dict_when_result_missing(self):
        session = _FakeSession(body=_ok_body(None))
        client = _authed_client(session)
        assert await client.async_get_app_user() == {}

    async def test_get_app_user_raises_when_result_is_not_a_dict(self):
        session = _FakeSession(body=_ok_body([1, 2]))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="dictionary"):
            await client.async_get_app_user()

    async def test_list_systems_returns_empty_list_when_result_missing(self):
        session = _FakeSession(body=_ok_body(None))
        client = _authed_client(session)
        assert await client.async_list_systems() == []

    async def test_list_systems_raises_when_result_is_not_a_list(self):
        session = _FakeSession(body=_ok_body({"a": 1}))
        client = _authed_client(session)
        with pytest.raises(JackeryHomeApiError, match="list"):
            await client.async_list_systems()

    async def test_list_systems_filters_out_non_dict_items(self):
        session = _FakeSession(body=_ok_body([{"id": 1}, "garbage", {"id": 2}, 5]))
        client = _authed_client(session)
        assert await client.async_list_systems() == [{"id": 1}, {"id": 2}]


class TestSimplePassThroughEndpoints:
    async def test_get_devices_by_system_returns_list(self):
        session = _FakeSession(body=_ok_body([{"deviceNo": "d1"}]))
        client = _authed_client(session)

        result = await client.async_get_devices_by_system("sys-1")

        assert result == [{"deviceNo": "d1"}]
        assert session.calls[0]["method"] == "GET"
        assert session.calls[0]["url"] == f"{BASE_URL}/geneverse-iot-home/v2/home/device/bySystemId/sys-1"

    async def test_get_device_detail_returns_dict(self):
        session = _FakeSession(body=_ok_body({"baseVO": {"softVer": "1.2.3"}}))
        client = _authed_client(session)

        result = await client.async_get_device_detail("dev-1")

        assert result == {"baseVO": {"softVer": "1.2.3"}}
        assert session.calls[0]["url"] == f"{BASE_URL}/geneverse-iot-home/v1/home/device/detail?deviceNo=dev-1"

    async def test_get_cluster_trend_daily_sends_expected_payload(self):
        session = _FakeSession(body=_ok_body({"pv": 1}))
        client = _authed_client(session)

        result = await client.async_get_cluster_trend_daily("sys-1", "2026-08-05")

        assert result == {"pv": 1}
        assert session.calls[0]["method"] == "POST"
        assert session.calls[0]["json"] == {
            "startTime": "2026-08-05",
            "endTime": "2026-08-05",
            "type": "2",
            "systemId": "sys-1",
        }

    async def test_get_battery_bms_trend_daily_sends_expected_payload(self):
        session = _FakeSession(body=_ok_body({"charged": 1}))
        client = _authed_client(session)

        result = await client.async_get_battery_bms_trend_daily("sys-1", "2026-08-05")

        assert result == {"charged": 1}
        assert session.calls[0]["json"] == {
            "startTime": "2026-08-05",
            "endTime": "2026-08-05",
            "type": "2",
            "systemId": "sys-1",
        }


class TestGetMonitor:
    async def test_without_system_id_sends_empty_payload(self):
        session = _FakeSession(body=_ok_body({}))
        client = _authed_client(session)
        await client.async_get_monitor()
        assert session.calls[0]["json"] == {}

    async def test_with_system_id_includes_it_in_payload(self):
        session = _FakeSession(body=_ok_body({"soc": 50}))
        client = _authed_client(session)
        result = await client.async_get_monitor("sys-1")
        assert result == {"soc": 50}
        assert session.calls[0]["json"] == {"systemId": "sys-1"}


class TestGetMqttCredentials:
    async def test_v2_success_short_circuits_before_v1(self):
        v2_result = {"mqttServer": "h", "mqttUserName": "u", "mqttPassword": "p"}
        session = _FakeSession(script=[{"body": _ok_body(v2_result)}])
        client = _authed_client(session)

        result = await client.async_get_mqtt_credentials()

        assert result["_password_is_plaintext"] is True
        assert result["_source_endpoint"] == "v2"
        assert len(session.calls) == 1
        assert "/v2/idc/config/mqttServer" in session.calls[0]["url"]

    async def test_incomplete_v2_falls_back_to_v1(self):
        v2_incomplete = {"mqttServer": "h"}  # missing username/password
        v1_result = {"mqttServer": "h", "mqttUserName": "u", "mqttPassword": "enc"}
        session = _FakeSession(
            script=[{"body": _ok_body(v2_incomplete)}, {"body": _ok_body(v1_result)}]
        )
        client = _authed_client(session)

        result = await client.async_get_mqtt_credentials()

        assert result["_password_is_plaintext"] is False
        assert result["_source_endpoint"] == "v1"
        assert len(session.calls) == 2

    async def test_both_endpoints_incomplete_raises_api_error(self):
        session = _FakeSession(
            script=[
                {"body": _ok_body({"mqttServer": "h"})},
                {"body": _ok_body({"mqttServer": "h"})},
            ]
        )
        client = _authed_client(session)

        with pytest.raises(JackeryHomeApiError, match="Unable to retrieve"):
            await client.async_get_mqtt_credentials()

    async def test_v2_error_falls_back_to_v1_success(self):
        v1_result = {"mqttServer": "h", "mqttUserName": "u", "mqttPassword": "enc"}
        session = _FakeSession(
            script=[
                {"exception": aiohttp.ClientConnectionError("v2 down")},
                {"body": _ok_body(v1_result)},
            ]
        )
        client = _authed_client(session)

        result = await client.async_get_mqtt_credentials()

        assert result["_source_endpoint"] == "v1"

    async def test_both_endpoints_erroring_reraises_last_error(self):
        session = _FakeSession(
            script=[
                {"exception": aiohttp.ClientConnectionError("v2 down")},
                {"exception": aiohttp.ClientConnectionError("v1 down")},
            ]
        )
        client = _authed_client(session)

        with pytest.raises(JackeryHomeApiError, match="v1 down"):
            await client.async_get_mqtt_credentials()


class TestBuildMqttCredentials:
    async def test_missing_required_fields_raises_api_error(self):
        client = _authed_client(_FakeSession())
        with pytest.raises(JackeryHomeApiError):
            await client.async_build_mqtt_credentials(
                mqtt_config={"mqttServer": "", "mqttUserName": "u", "mqttPassword": "p"}
            )

    async def test_plaintext_password_used_as_is_without_crypto_key(self):
        client = _authed_client(_FakeSession())
        creds = await client.async_build_mqtt_credentials(
            mqtt_config={
                "mqttServer": "broker.test",
                "mqttUserName": "user1",
                "mqttPassword": "plain-secret",
                "_password_is_plaintext": True,
                "_source_endpoint": "v2",
            }
        )
        assert creds["password"] == "plain-secret"
        assert creds["host"] == "broker.test"
        assert creds["tls"] is True

    async def test_legacy_path_decrypts_with_crypto_key(self):
        encrypted = encrypt_text("legacy-secret", VALID_KEY)
        client = _authed_client(_FakeSession())

        creds = await client.async_build_mqtt_credentials(
            crypto_key=VALID_KEY,
            mqtt_config={
                "mqttServer": "broker.test",
                "mqttUserName": "user1",
                "mqttPassword": encrypted,
                "_password_is_plaintext": False,
                "_source_endpoint": "v1",
            },
        )

        assert creds["password"] == "legacy-secret"

    async def test_legacy_path_without_crypto_key_raises_crypto_error_not_name_error(self):
        """Regression test for a real bug caught while writing this suite:
        `JackeryHomeCryptoError` is raised on this branch but was not
        imported in client.py, so this path blew up with `NameError`
        instead of the intended, catchable `JackeryHomeCryptoError` (fixed
        by importing it alongside the other Jackery* exceptions - see the
        top-of-file import list).
        """
        client = _authed_client(_FakeSession())
        with pytest.raises(JackeryHomeCryptoError, match="crypto key"):
            await client.async_build_mqtt_credentials(
                crypto_key=None,
                mqtt_config={
                    "mqttServer": "broker.test",
                    "mqttUserName": "user1",
                    "mqttPassword": "encoded",
                    "_password_is_plaintext": False,
                },
            )

    async def test_legacy_path_blank_crypto_key_also_raises_crypto_error(self):
        client = _authed_client(_FakeSession())
        with pytest.raises(JackeryHomeCryptoError):
            await client.async_build_mqtt_credentials(
                crypto_key="   ",
                mqtt_config={
                    "mqttServer": "broker.test",
                    "mqttUserName": "user1",
                    "mqttPassword": "encoded",
                    "_password_is_plaintext": False,
                },
            )

    async def test_legacy_path_decrypt_failure_propagates_crypto_error(self):
        # Valid base64 but not a multiple of the AES block size - every
        # decrypt strategy in decrypt_text() fails deterministically.
        bad_ciphertext = base64.b64encode(b"short").decode()
        client = _authed_client(_FakeSession())

        with pytest.raises(JackeryCryptoError):
            await client.async_build_mqtt_credentials(
                crypto_key=VALID_KEY,
                mqtt_config={
                    "mqttServer": "broker.test",
                    "mqttUserName": "user1",
                    "mqttPassword": bad_ciphertext,
                    "_password_is_plaintext": False,
                },
            )

    async def test_invalid_port_falls_back_to_default(self):
        client = _authed_client(_FakeSession())
        creds = await client.async_build_mqtt_credentials(
            mqtt_config={
                "mqttServer": "broker.test",
                "mqttUserName": "user1",
                "mqttPassword": "plain",
                "mqttPort": "not-a-number",
                "_password_is_plaintext": True,
            }
        )
        assert creds["port"] == MQTT_DEFAULT_PORT

    async def test_valid_string_port_is_coerced_to_int(self):
        client = _authed_client(_FakeSession())
        creds = await client.async_build_mqtt_credentials(
            mqtt_config={
                "mqttServer": "broker.test",
                "mqttUserName": "user1",
                "mqttPassword": "plain",
                "mqttPort": "1883",
                "_password_is_plaintext": True,
            }
        )
        assert creds["port"] == 1883

    async def test_fetches_mqtt_config_itself_when_none_passed(self):
        v2_result = {"mqttServer": "h", "mqttUserName": "u", "mqttPassword": "p"}
        session = _FakeSession(script=[{"body": _ok_body(v2_result)}])
        client = _authed_client(session)

        creds = await client.async_build_mqtt_credentials()

        assert creds["host"] == "h"
        assert len(session.calls) == 1
