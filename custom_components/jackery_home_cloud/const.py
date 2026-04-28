"""Constants for the Jackery Home Cloud integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "jackery_home_cloud"
MANUFACTURER = "Jackery"

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_PHONE_UID = "phone_uid"
CONF_SELECTED_SYSTEMS = "selected_systems"
CONF_ENABLE_MQTT = "enable_mqtt"
CONF_CRYPTO_KEY = "crypto_key"
CONF_MQTT_DEBUG_RAW = "mqtt_debug_raw"

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
METADATA_UPDATE_INTERVAL_SECONDS = 3600
UPDATE_INTERVAL_SECONDS = 60
DAILY_TREND_UPDATE_INTERVAL_SECONDS = 900

TREND_TYPE_DAY = "2"
TREND_DATE_FORMAT = "%Y%m%d"

DEFAULT_ENABLE_MQTT = False
DEFAULT_MQTT_DEBUG_RAW = False
DEFAULT_MQTT_USE_TLS = True
MQTT_DEFAULT_PORT = 8883
MQTT_LWT_TOPIC_TEMPLATE = "v1/iot_gw/gw_lwt/{device_serial}"
MQTT_CLOUD_DATA_TOPIC_TEMPLATE = "v1/iot_gw/cloud/data/{device_serial}"


PLATFORMS: list[Platform] = [Platform.SENSOR]

CONF_MQTT_TLS_INSECURE = "mqtt_tls_insecure"


MODEL_NAME_MAP: dict[str, str] = {
    "JAKS-IN1K5-BA2K-EUA1": "HomePower 2000 Ultra",
}
