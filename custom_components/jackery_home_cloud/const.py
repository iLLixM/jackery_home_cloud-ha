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
CONF_MQTT_POLL_INTERVAL = "mqtt_poll_interval"

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
DEFAULT_MQTT_TLS_INSECURE = False
DEFAULT_MQTT_USE_TLS = True
DEFAULT_MQTT_POLL_INTERVAL_SECONDS = 60
MQTT_POLL_INTERVAL_MIN_SECONDS = 5
MQTT_POLL_INTERVAL_MAX_SECONDS = 60
MQTT_DEFAULT_PORT = 8883
MQTT_LWT_TOPIC_TEMPLATE = "v1/iot_gw/gw_lwt/{device_serial}"
MQTT_GW_DATA_TOPIC_TEMPLATE = "v1/iot_gw/gw/data/{device_serial}"
MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE = "v1/iot_gw/cloud/data/{device_serial}"

MQTT_LIVE_VALUE_MAX_AGE_SECONDS = 900
# Fixed (non-user-configurable) cadence for the slow "cumulative totals" poll
# group - 3x headroom under MQTT_LIVE_VALUE_MAX_AGE_SECONDS above.
MQTT_TOTALS_POLL_INTERVAL_SECONDS = 300
# MQTT_POLL_INTERVAL_MAX_SECONDS above must stay comfortably below this
# value, or the power/SOC sensors it gates will flap between MQTT and REST
# every poll cycle.
MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS = 120
MQTT_EMS_BATTERY_CHARGED_TODAY_METER_ID = "16952321"
MQTT_EMS_BATTERY_DISCHARGED_TODAY_METER_ID = "16953345"
MQTT_EMS_BATTERY_CHARGED_TOTAL_METER_ID = "16964609"
MQTT_EMS_BATTERY_DISCHARGED_TOTAL_METER_ID = "16965633"
MQTT_EMS_PV1_ENERGY_TOTAL_METER_ID = "16966657"
MQTT_EMS_PV2_ENERGY_TOTAL_METER_ID = "16967681"
MQTT_EMS_PV_ENERGY_TOTAL_METER_ID = "16961537"
MQTT_EMS_REBOOT_METER_ID = "22027265"
MQTT_EMS_AC_OUTPUT_METER_ID = "23120897"

# Unverified candidate meters for the MQTT-vs-REST comparison sensors below.
# Cross-checked against REST snapshots where possible; not yet confirmed
# against the Jackery app.
MQTT_EMS_BATTERY_SOC_METER_ID: str = "21548033"
MQTT_EMS_BATTERY_SOC_SCALE = 10.0
MQTT_PCS_PV1_POWER_METER_ID = "50490369"
MQTT_PCS_PV2_POWER_METER_ID = "50490370"

# Magnitude of REST ac_main_power (power at the AC-main/PCS boundary tied to
# battery charge/discharge) - distinct from MQTT_EMS_EPS_LOAD_POWER_METER_ID
# (AC output socket power) and MQTT_EMS_OTHER_LOAD_POWER_METER_ID (household
# load, which only matches this value when grid_power is ~0). The raw meter
# is UNSIGNED; the signed "ac_main_power_mqtt" bundle value is derived in
# coordinator.py from battery_power_mqtt's sign (REST ac_main_power's sign is
# always the opposite of MQTT_BMS1_BATTERY_POWER_METER_ID's). Do not assume
# this raw meter is signed if reading it directly elsewhere.
MQTT_PCS_AC_MAIN_POWER_METER_ID: str = "50416641"

# Not a REST-available field at all: instantaneous battery charge/discharge
# power. Negative while discharging, positive while charging.
MQTT_BMS1_BATTERY_POWER_METER_ID: str = "33659905"

# Household load power, signed like REST other_load_power itself. Do not
# confuse with the unsigned MQTT_PCS_AC_MAIN_POWER_METER_ID above - they
# read the same value only when grid_power is ~0.
MQTT_EMS_OTHER_LOAD_POWER_METER_ID: str = "16936961"

# Raw value is sign-flipped relative to REST grid_power, so store as -raw.
MQTT_EMS_GRID_POWER_METER_ID: str = "16930817"

# Power delivered through the unit's own physical AC output sockets,
# gated by the AC output relay (MQTT_EMS_AC_OUTPUT_METER_ID).
MQTT_EMS_EPS_LOAD_POWER_METER_ID: str = "16933889"

# Battery priority/mode register. See MODE_OPTIONS in select.py for the
# value mapping. Mode "5" (Time of use) only selects that mode - it does
# not itself configure the schedule, which lives in the separate
# charge/discharge time-window table below (MQTT_EMS_CHARGE_WINDOW_METER_IDS
# / MQTT_EMS_DISCHARGE_WINDOW_METER_IDS).
MQTT_EMS_MODE_METER_ID: str = "23132161"

# Scheduled charge/discharge time-window table, only takes effect while
# MQTT_EMS_MODE_METER_ID == "5". Each meter holds an 8-digit "HHMMHHMM"
# string (start+end, no separator) or "0" for an unused slot. The two
# 10-meter ranges are INDEPENDENT sequential lists, not paired per-cycle:
# MQTT_EMS_CHARGE_WINDOW_METER_IDS[0] is always the first charge window
# entered, [1] the second, etc., same for discharge in the other list.
MQTT_EMS_CHARGE_WINDOW_METER_IDS: tuple[str, ...] = tuple(str(23146497 + i) for i in range(10))
MQTT_EMS_DISCHARGE_WINDOW_METER_IDS: tuple[str, ...] = tuple(str(23147521 + i) for i in range(10))

# Battery SOC operating window: charge floor (do not resume charging below
# this SOC) and discharge ceiling (do not discharge above this SOC). Both
# are raw / 10, same scale as MQTT_EMS_BATTERY_SOC_SCALE, and are always
# sent together in one data_set even when only one value actually changed.
MQTT_EMS_CHARGE_FLOOR_SOC_METER_ID: str = "23136257"
MQTT_EMS_DISCHARGE_CEILING_SOC_METER_ID: str = "23135233"

# Max charge (input) power limit, in Watts directly (not scaled, unlike the
# SOC meters above).
MQTT_EMS_CHARGE_POWER_LIMIT_METER_ID: str = "23286785"

# Standby toggle (labeled "Standby" / "Exit standby" in the app). See
# MQTT_EMS_STANDBY_RAW_ON / MQTT_EMS_STANDBY_RAW_OFF below for the raw
# values.
MQTT_EMS_STANDBY_METER_ID: str = "23133185"
MQTT_EMS_STANDBY_RAW_ON = "1"
MQTT_EMS_STANDBY_RAW_OFF = "2"

# Max output (discharge) power limit, as a preset index rather than a direct
# Watts value (unlike MQTT_EMS_CHARGE_POWER_LIMIT_METER_ID above, which is a
# direct W value). See OUTPUT_POWER_LIMIT_OPTIONS in select.py for the known
# preset values.
MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID: str = "23324673"

# "Auto standby" toggle, distinct from MQTT_EMS_STANDBY_METER_ID above
# (which is the immediate/manual "enter standby now" action). The raw
# values are large unsigned ints that read back as negative when
# interpreted as signed 32-bit two's complement (-5 / -1) - likely a single
# bit within a wider feature-flags bitmask register rather than a dedicated
# boolean meter.
MQTT_EMS_AUTO_STANDBY_METER_ID: str = "23375873"
MQTT_EMS_AUTO_STANDBY_RAW_ON = "4294967295"
MQTT_EMS_AUTO_STANDBY_RAW_OFF = "4294967291"


PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
]

CONF_MQTT_TLS_INSECURE = "mqtt_tls_insecure"


MODEL_NAME_MAP: dict[str, str] = {
    "JAKS-IN1K5-BA2K-EUA1": "HomePower 2000 Ultra",
}
