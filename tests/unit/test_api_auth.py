"""Unit tests for api/auth.py."""

from __future__ import annotations

import uuid

from custom_components.jackery_home_cloud.api.auth import (
    build_auth_headers,
    build_base_headers,
    build_login_payload,
    build_phone_uid,
)
from custom_components.jackery_home_cloud.const import (
    DEFAULT_CLIENT_TYPE,
    DEFAULT_LOGIN_TYPE,
    DEFAULT_REMEMBER_ME,
    DEFAULT_USER_END,
    DEFAULT_USER_TYPE,
)


class TestBuildPhoneUid:
    def test_with_seed_is_deterministic_and_prefixed(self):
        assert build_phone_uid("account-123") == "ha-account-123"
        assert build_phone_uid("account-123") == build_phone_uid("account-123")

    def test_without_seed_generates_unique_uuid_based_value(self):
        first = build_phone_uid()
        second = build_phone_uid()
        assert first != second
        assert first.startswith("ha-")
        # Suffix must be a parseable UUID.
        uuid.UUID(first.removeprefix("ha-"))

    def test_empty_seed_falls_back_to_random_uuid(self):
        value = build_phone_uid("")
        assert value.startswith("ha-")
        uuid.UUID(value.removeprefix("ha-"))


class TestBuildLoginPayload:
    def test_default_payload_fields(self):
        payload = build_login_payload("user@example.com", "pw", "ha-phone-1")
        assert payload["account"] == "user@example.com"
        assert payload["password"] == "pw"
        assert payload["phoneUid"] == "ha-phone-1"
        assert payload["encrypted"] is False
        assert payload["userEnd"] == DEFAULT_USER_END
        assert payload["userType"] == DEFAULT_USER_TYPE
        assert payload["loginType"] == DEFAULT_LOGIN_TYPE
        assert payload["rememberMe"] == DEFAULT_REMEMBER_ME
        assert payload["clientType"] == DEFAULT_CLIENT_TYPE

    def test_encrypted_flag_can_be_overridden(self):
        payload = build_login_payload("u", "p", "phone", encrypted=True)
        assert payload["encrypted"] is True


class TestHeaders:
    def test_base_headers_contain_required_fields(self):
        headers = build_base_headers()
        for key in (
            "user-agent",
            "accept-language",
            "model",
            "accept-encoding",
            "x-app-name",
            "content-type",
            "x-app-version",
            "sdkint",
            "id",
            "userend",
        ):
            assert key in headers

    def test_auth_headers_add_bearer_authorization(self):
        headers = build_auth_headers("tok123")
        assert headers["authorization"] == "Bearer tok123"
        # Base headers must still be present.
        assert "user-agent" in headers

    def test_auth_headers_custom_prefix(self):
        headers = build_auth_headers("tok123", token_prefix="Token")
        assert headers["authorization"] == "Token tok123"
