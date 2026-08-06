#!/usr/bin/env python3
"""Jackery Home Cloud API test client.

This script is intended as a lightweight diagnostic and exploration tool for
the unofficial Jackery Home Cloud REST API. It supports login validation and a
set of read-only follow-up calls that are useful for testing account access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

DEFAULT_BASE_URL = (
    "https://prodeu-energymanagement-api.hello-tech.com:8000/"
    "geneverse-iot-gateway"
)


class JackeryHomeApiError(RuntimeError):
    """Raised when the Jackery API request fails or returns an error payload."""


@dataclass
class JackeryHomeConfig:
    """Static client configuration."""

    account: str
    password: str
    encrypted: bool
    phone_uid: str

    base_url: str = DEFAULT_BASE_URL
    user_end: str = "HOME"
    user_type: str = "2"
    client_type: str = "APP"
    login_type: int = 1
    remember_me: bool = False

    accept_language: str = "en-US"
    model: str = "Phone"
    x_app_name: str = "Custom-Phone"
    x_app_version: str = "home_android_v2.10.22"
    sdkint: str = "34"
    build_id: str = "UP1A.231105.003.A1"
    user_agent: str = "Dart/3.11 (dart:io)"

    verify_tls: bool = True
    timeout: int = 20


class JackeryHomeClient:
    """Minimal synchronous Jackery Home Cloud REST client."""

    def __init__(self, config: JackeryHomeConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_prefix: str = "Bearer"
        self.user_info: Optional[Dict[str, Any]] = None

    def _base_headers(self) -> Dict[str, str]:
        return {
            "user-agent": self.config.user_agent,
            "accept-language": self.config.accept_language,
            "model": self.config.model,
            "accept-encoding": "gzip",
            "x-app-name": self.config.x_app_name,
            "content-type": "application/json;charset=UTF-8",
            "x-app-version": self.config.x_app_version,
            "sdkint": self.config.sdkint,
            "id": self.config.build_id,
            "userend": self.config.user_end,
        }

    def _auth_headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise JackeryHomeApiError("No access token available. Please run login() first.")
        headers = self._base_headers()
        headers["authorization"] = f"{self.token_prefix} {self.access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        headers = self._auth_headers() if auth else self._base_headers()

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                timeout=self.config.timeout,
                verify=self.config.verify_tls,
            )
        except requests.RequestException as exc:
            raise JackeryHomeApiError(f"Request failed for {path}: {exc}") from exc

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise JackeryHomeApiError(
                f"HTTP error for {path}: {response.status_code} {response.text[:500]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise JackeryHomeApiError(
                f"Response is not valid JSON for {path}: {response.text[:500]}"
            ) from exc

        if not data.get("success", False):
            raise JackeryHomeApiError(
                f"API error for {path}: code={data.get('code')} msg={data.get('msg')}"
            )

        return data

    def login(self) -> Dict[str, Any]:
        payload = {
            "encrypted": self.config.encrypted,
            "userEnd": self.config.user_end,
            "userType": self.config.user_type,
            "account": self.config.account,
            "password": self.config.password,
            "phoneUid": self.config.phone_uid,
            "loginType": self.config.login_type,
            "rememberMe": self.config.remember_me,
            "clientType": self.config.client_type,
        }
        data = self._request(
            "POST",
            "/geneverse-iot-home/v1/home/auth/login",
            json_body=payload,
            auth=False,
        )
        result = data.get("result", {})
        self.token_prefix = result.get("tokenPrefix", "Bearer")
        self.access_token = result.get("accessToken")
        self.refresh_token = result.get("refreshToken")
        self.user_info = result.get("userInfo")

        if not self.access_token:
            raise JackeryHomeApiError("Login succeeded but accessToken is missing.")
        return data

    def get_login_status(self) -> Dict[str, Any]:
        return self._request("GET", "/geneverse-iot-home/v1/home/auth/loginStatus", auth=True)

    def get_app_user(self) -> Dict[str, Any]:
        return self._request("GET", "/geneverse-iot-home/v1/appUser/getOne", auth=True)

    def list_systems(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/geneverse-iot-home/v1/system/listByUserV2", auth=True)
        return data.get("result", [])

    def get_system_detail(self, system_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/geneverse-iot-home/v1/system/{system_id}", auth=True)

    def get_monitor_for_system(self, system_id: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/geneverse-iot-home/v1/app/monitor/",
            json_body={"systemId": system_id},
            auth=True,
        )

    def get_devices_by_system(self, system_id: str) -> List[Dict[str, Any]]:
        data = self._request(
            "GET",
            f"/geneverse-iot-home/v2/home/device/bySystemId/{system_id}",
            auth=True,
        )
        return data.get("result", [])

    def get_diy_epc_devices(self, system_id: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/geneverse-iot-home/v1/home/device/diyEpcDeviceList?systemId={system_id}",
            auth=True,
        )

    def get_device_detail(self, device_no: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/geneverse-iot-home/v1/home/device/detail?deviceNo={device_no}",
            auth=True,
        )

    def get_ct_detail(self, device_no: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/geneverse-iot-home/v1/home/device/ct/detail?deviceNo={device_no}",
            auth=True,
        )

    def get_mqtt_config(self) -> Dict[str, Any]:
        endpoints = (
            ("/geneverse-iot-home/v2/idc/config/mqttServer", True, "v2"),
            ("/geneverse-iot-home/v1/idc/config/mqttServer", False, "v1"),
        )

        last_error: Optional[Exception] = None
        for path, password_is_plaintext, source_endpoint in endpoints:
            try:
                data = self._request("GET", path, auth=True)
                result = data.get("result") or {}
                if (
                    result.get("mqttServer")
                    and result.get("mqttUserName")
                    and result.get("mqttPassword")
                ):
                    result["_source_endpoint"] = source_endpoint
                    result["_password_is_plaintext"] = password_is_plaintext
                    return result
            except JackeryHomeApiError as exc:
                last_error = exc

        if last_error is not None:
            raise last_error
        raise JackeryHomeApiError(
            "MQTT response did not contain complete credentials."
        )


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def mask_account(value: str) -> str:
    if "@" not in value:
        return value[:3] + "***" if len(value) > 3 else "***"
    local, domain = value.split("@", 1)
    local_masked = local[:2] + "***" if len(local) > 2 else (local[:1] + "*" if local else "*")
    return f"{local_masked}@{domain}"


def choose_output(data: Any, mode: str) -> str:
    return json.dumps(data, ensure_ascii=False) if mode == "json" else json.dumps(data, indent=2, ensure_ascii=False)


def resolve_password(args: argparse.Namespace) -> str:
    if args.encrypted:
        if args.encrypted_password:
            return args.encrypted_password
        if args.password:
            return args.password
        raise SystemExit("Error: encrypted login requires --encrypted-password or --password.")

    if args.plain_password:
        return args.plain_password
    if args.password:
        return args.password
    env_password = os.getenv("JACKERY_PASSWORD")
    if env_password:
        return env_password

    raise SystemExit(
        "Error: plain login requires --plain-password, --password, or JACKERY_PASSWORD."
    )


def extract_system_ids(systems: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    for item in systems:
        system_id = item.get("id") or item.get("systemId")
        if system_id is not None:
            ids.append(str(system_id))
    return ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jackery Home Cloud API test client (read-only).")
    parser.add_argument("--account", required=True, help="Jackery account email/login")
    parser.add_argument("--password", help="Generic password input. Interpreted according to --encrypted.")
    parser.add_argument("--plain-password", help="Plain-text password for encrypted=false login")
    parser.add_argument("--encrypted-password", help="Already encrypted password for encrypted=true login")
    parser.add_argument("--encrypted", type=parse_bool, default=False, help="Set login payload field 'encrypted' (default: false)")
    parser.add_argument("--phone-uid", default="sample-id-123", help="phoneUid used for the login request (default: sample-id-123)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Override the base API URL")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")

    parser.add_argument("--login-only", action="store_true", help="Only validate login and stop afterwards")
    parser.add_argument("--show-login-status", action="store_true", help="Call loginStatus after login")
    parser.add_argument("--show-app-user", action="store_true", help="Fetch and print app user information")
    parser.add_argument("--list-systems", action="store_true", help="Fetch and print the system list")
    parser.add_argument("--system-detail", action="store_true", help="Fetch and print system detail for --system-id or all systems")
    parser.add_argument("--list-devices", action="store_true", help="Fetch and print devices for --system-id or all systems")
    parser.add_argument("--show-monitor", action="store_true", help="Fetch and print monitor snapshot for --system-id or all systems")
    parser.add_argument("--show-diy-devices", action="store_true", help="Fetch and print DIY/EPC grouped devices for --system-id or all systems")
    parser.add_argument("--show-mqtt", action="store_true", help="Fetch and print MQTT credentials")
    parser.add_argument("--device-detail", action="store_true", help="Fetch and print generic device detail for --device-no")
    parser.add_argument("--ct-detail", action="store_true", help="Fetch and print CT/Smart Meter detail for --device-no")

    parser.add_argument("--system-id", help="Restrict system-based calls to a single system ID")
    parser.add_argument("--all-systems", action="store_true", help="Run system-based calls for all available systems")
    parser.add_argument("--device-no", help="Device number for --device-detail or --ct-detail")

    parser.add_argument("--output", choices=["pretty", "json"], default="pretty", help="Output format")
    parser.add_argument("--save", help="Save the combined output as JSON to a file")
    return parser


def should_run_followups(args: argparse.Namespace) -> bool:
    return any([
        args.show_login_status,
        args.show_app_user,
        args.list_systems,
        args.system_detail,
        args.list_devices,
        args.show_monitor,
        args.show_diy_devices,
        args.show_mqtt,
        args.device_detail,
        args.ct_detail,
        args.all_systems,
        bool(args.system_id),
        bool(args.device_no),
    ])


def determine_system_targets(args: argparse.Namespace, systems: List[Dict[str, Any]]) -> List[str]:
    all_ids = extract_system_ids(systems)
    if args.system_id:
        return [str(args.system_id)]
    if args.all_systems:
        return all_ids
    if any([args.system_detail, args.list_devices, args.show_monitor, args.show_diy_devices]):
        return all_ids[:1]
    return []


def print_block(title: str, data: Any, output_mode: str) -> None:
    print(f"\n== {title} ==")
    print(choose_output(data, output_mode))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    password = resolve_password(args)

    cfg = JackeryHomeConfig(
        account=args.account,
        password=password,
        encrypted=args.encrypted,
        phone_uid=args.phone_uid,
        base_url=args.base_url,
        verify_tls=not args.insecure,
        timeout=args.timeout,
    )
    client = JackeryHomeClient(cfg)

    collected: Dict[str, Any] = {
        "meta": {
            "base_url": cfg.base_url,
            "account": mask_account(cfg.account),
            "encrypted": cfg.encrypted,
            "phone_uid": cfg.phone_uid,
            "x_app_version": cfg.x_app_version,
            "user_agent": cfg.user_agent,
        }
    }

    login_data = client.login()
    collected["login"] = login_data
    print("LOGIN OK")

    if args.login_only:
        print_block("LOGIN", login_data, args.output)
        if args.save:
            with open(args.save, "w", encoding="utf-8") as handle:
                json.dump(collected, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        return

    if not should_run_followups(args):
        args.show_app_user = True
        args.list_systems = True
        args.list_devices = True
        args.show_mqtt = True

    if args.show_login_status:
        collected["login_status"] = client.get_login_status()
    if args.show_app_user:
        collected["app_user"] = client.get_app_user()

    systems: List[Dict[str, Any]] = []
    if (
        args.list_systems or args.system_detail or args.list_devices or args.show_monitor
        or args.show_diy_devices or args.all_systems or args.system_id
    ):
        systems = client.list_systems()
        collected["systems"] = systems

    target_system_ids = determine_system_targets(args, systems)

    if args.system_detail and target_system_ids:
        collected["system_detail"] = {sid: client.get_system_detail(sid) for sid in target_system_ids}
    if args.show_monitor and target_system_ids:
        collected["monitor"] = {sid: client.get_monitor_for_system(sid) for sid in target_system_ids}
    if args.list_devices and target_system_ids:
        collected["devices"] = {sid: client.get_devices_by_system(sid) for sid in target_system_ids}
    if args.show_diy_devices and target_system_ids:
        collected["diy_devices"] = {sid: client.get_diy_epc_devices(sid) for sid in target_system_ids}
    if args.show_mqtt:
        collected["mqtt"] = client.get_mqtt_config()
    if args.device_detail:
        if not args.device_no:
            raise SystemExit("Error: --device-detail requires --device-no")
        collected["device_detail"] = client.get_device_detail(args.device_no)
    if args.ct_detail:
        if not args.device_no:
            raise SystemExit("Error: --ct-detail requires --device-no")
        collected["ct_detail"] = client.get_ct_detail(args.device_no)

    for key in [
        "login_status", "app_user", "systems", "system_detail",
        "monitor", "devices", "diy_devices", "mqtt",
        "device_detail", "ct_detail"
    ]:
        if key in collected:
            print_block(key.upper(), collected[key], args.output)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(collected, handle, indent=2, ensure_ascii=False)
            handle.write("\n")


if __name__ == "__main__":
    try:
        main()
    except JackeryHomeApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
