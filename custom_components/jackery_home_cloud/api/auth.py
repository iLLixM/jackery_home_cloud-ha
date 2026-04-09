"""Authentication helper functions for Jackery Home Cloud."""

from __future__ import annotations

import uuid

from ..const import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_BUILD_ID,
    DEFAULT_CLIENT_TYPE,
    DEFAULT_ENCRYPTED,
    DEFAULT_LOGIN_TYPE,
    DEFAULT_MODEL,
    DEFAULT_REMEMBER_ME,
    DEFAULT_SDK_INT,
    DEFAULT_USER_END,
    DEFAULT_USER_TYPE,
    DEFAULT_X_APP_NAME,
    DEFAULT_X_APP_VERSION,
)


def build_phone_uid(seed: str | None = None) -> str:
    """Build a Home Assistant specific phone UID.

    The Jackery API requires a phoneUid value during login. The current reverse
    engineered findings indicate that the value does not need to match a real
    mobile device, as long as it stays stable for the account inside Home
    Assistant.
    """
    if seed:
        return f"ha-{seed}"
    return f"ha-{uuid.uuid4()}"


def build_login_payload(
    account: str,
    password: str,
    phone_uid: str,
    encrypted: bool = DEFAULT_ENCRYPTED,
) -> dict[str, object]:
    """Build the login body for the reverse engineered Jackery cloud API.

    The reverse engineered API currently accepts plain-text passwords when the
    encrypted flag is set to false.

    TODO: Reverse engineer the encrypted=true mode used by the official app and
    switch the integration to that mode once it is fully understood.
    """
    return {
        "encrypted": encrypted,
        "userEnd": DEFAULT_USER_END,
        "userType": DEFAULT_USER_TYPE,
        "account": account,
        "password": password,
        "phoneUid": phone_uid,
        "loginType": DEFAULT_LOGIN_TYPE,
        "rememberMe": DEFAULT_REMEMBER_ME,
        "clientType": DEFAULT_CLIENT_TYPE,
    }


def build_base_headers() -> dict[str, str]:
    """Build the common request headers observed in the Android app flow."""
    return {
        "user-agent": "Dart/3.11 (dart:io)",
        "accept-language": DEFAULT_ACCEPT_LANGUAGE,
        "model": DEFAULT_MODEL,
        "accept-encoding": "gzip",
        "x-app-name": DEFAULT_X_APP_NAME,
        "content-type": "application/json;charset=UTF-8",
        "x-app-version": DEFAULT_X_APP_VERSION,
        "sdkint": DEFAULT_SDK_INT,
        "id": DEFAULT_BUILD_ID,
        "userend": DEFAULT_USER_END,
    }


def build_auth_headers(access_token: str, token_prefix: str = "Bearer") -> dict[str, str]:
    """Build headers for authenticated requests."""
    headers = build_base_headers()
    headers["authorization"] = f"{token_prefix} {access_token}"
    return headers
