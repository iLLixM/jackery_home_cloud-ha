"""Constants for the Jackery Home Cloud integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "jackery_home_cloud"
MANUFACTURER = "Jackery"

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_PHONE_UID = "phone_uid"
CONF_SELECTED_SYSTEMS = "selected_systems"

DEFAULT_USER_END = "HOME"
DEFAULT_USER_TYPE = "2"
DEFAULT_CLIENT_TYPE = "APP"
DEFAULT_LOGIN_TYPE = 1
DEFAULT_REMEMBER_ME = False
DEFAULT_ENCRYPTED = False

DEFAULT_ACCEPT_LANGUAGE = "en-US"
DEFAULT_MODEL = "Phone"
DEFAULT_X_APP_NAME = "Custom-Phone"
DEFAULT_X_APP_VERSION = "home_android_v2.10.22"
DEFAULT_SDK_INT = "34"
DEFAULT_BUILD_ID = "UP1A.231105.003.A1"

DEFAULT_BASE_URL = (
    "https://prodeu-energymanagement-api.hello-tech.com:8000/"
    "geneverse-iot-gateway"
)

API_REQUEST_TIMEOUT_SECONDS = 30
UPDATE_INTERVAL_SECONDS = 60
TREND_TYPE_DAY = "2"
TREND_DATE_FORMAT = "%Y%m%d"

PLATFORMS: list[Platform] = [Platform.SENSOR]
