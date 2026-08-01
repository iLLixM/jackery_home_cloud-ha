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
MQTT_POLL_INTERVAL_MAX_SECONDS = 600
MQTT_DEFAULT_PORT = 8883
MQTT_LWT_TOPIC_TEMPLATE = "v1/iot_gw/gw_lwt/{device_serial}"
MQTT_GW_DATA_TOPIC_TEMPLATE = "v1/iot_gw/gw/data/{device_serial}"
MQTT_CLOUD_COMMAND_TOPIC_TEMPLATE = "v1/iot_gw/cloud/data/{device_serial}"

MQTT_LIVE_VALUE_MAX_AGE_SECONDS = 900
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
# Values are cross-checked against REST snapshots where possible (see
# sensor.py comments); pending confirmation against the Jackery app.
MQTT_EMS_BATTERY_SOC_METER_ID: str = "21548033"
MQTT_EMS_BATTERY_SOC_SCALE = 10.0
MQTT_PCS_PV1_POWER_METER_ID = "50490369"
MQTT_PCS_PV2_POWER_METER_ID = "50490370"

# Magnitude of REST ac_main_power (power at the AC-main/PCS boundary tied to
# battery charge/discharge, NOT literal AC output socket power - that's
# MQTT_EMS_EPS_LOAD_POWER_METER_ID - and NOT household load - that's
# MQTT_EMS_OTHER_LOAD_POWER_METER_ID, a distinct meter, confirmed to diverge
# from this one whenever grid_power is non-zero). Confirmed by multiple live
# cross-checks on 2026-08-01 against |REST ac_main_power| (258, 580, 654 W
# discharging; 1489, 735, 854 W charging).
#
# The raw meter itself is UNSIGNED (no sign reported by the device for this
# quantity) - coordinator.py's _apply_mqtt_live_values_to_bundle derives the
# sign for the merged "ac_main_power_mqtt" bundle value from
# battery_power_mqtt's sign (REST ac_main_power's sign was confirmed to always
# be the opposite of MQTT_BMS1_BATTERY_POWER_METER_ID's sign). Do not assume
# this raw meter is signed if reading it directly elsewhere.
MQTT_PCS_AC_MAIN_POWER_METER_ID: str = "50416641"

# Not a REST-available field at all: instantaneous battery charge/discharge
# power. Sign convention confirmed from live traffic on 2026-08-01: negative
# while discharging (-280 to -314 W, matching an app-reported ~250-300 W
# discharge), positive while charging (+1416 to +1420 W, matching an
# app-reported ~1500 W charge rate derived from grid - household load).
MQTT_BMS1_BATTERY_POWER_METER_ID: str = "33659905"

# Confirmed mapping (EMS meter 16936961), signed like REST other_load_power
# itself: matched exactly twice on 2026-08-01, once at +609 W (grid=29 W,
# differed from ac_main_power=580 W by exactly the grid contribution) and
# once at -735 W (grid=0 W, coincided with ac_main_power that time only
# because grid was zero). Do not confuse with the unsigned
# MQTT_PCS_AC_MAIN_POWER_METER_ID above - they read the same value only when
# grid_power happens to be ~0.
MQTT_EMS_OTHER_LOAD_POWER_METER_ID: str = "16936961"

# Confirmed mapping (EMS meter 16930817): magnitude matched REST grid_power
# exactly (29 W) in the same cross-check; raw sign is flipped relative to
# REST (raw was -29 while REST read +29), so store as -raw. Cross-validated
# by energy balance in that same snapshot: ac_main_power_mqtt (580) +
# grid_power (29) = other_load_power (609) exactly.
MQTT_EMS_GRID_POWER_METER_ID: str = "16930817"

# Confirmed mapping (EMS meter 16933889): matched REST eps_load_power exactly
# in both states of a real on/off toggle test on 2026-08-01 (0.0 W with the
# AC output socket off, 28.0 W with a ~30 W load plugged into it) - this is
# power delivered through the unit's own physical AC output sockets
# ("sortie CA"), gated by the AC output relay (MQTT_EMS_AC_OUTPUT_METER_ID).
MQTT_EMS_EPS_LOAD_POWER_METER_ID: str = "16933889"

# Battery priority/mode register. Reverse engineered from live MQTT captures
# of app-driven mode changes (two independent sessions): value "3" and value
# "2" were each observed 3x when switching to "Priorite batterie" resp.
# "Autoconsommation". Value "7" (Smart / dynamic pricing) was observed once.
# "Duree d'utilisation" (scheduled charge/discharge) does not set this meter
# at all - it writes a separate time-window table starting at meter
# 23146497, not yet mapped.
MQTT_EMS_MODE_METER_ID: str = "23132161"

# Battery SOC operating window. Reverse engineered from 3 consistent live
# app-driven changes on 2026-08-01: charge floor (do not resume charging
# below this SOC) 23136257 = 300, 300, 200 -> 30.0%, 30.0%, 20.0% (raw / 10,
# same scale as MQTT_EMS_BATTERY_SOC_SCALE); discharge ceiling (do not
# discharge above this SOC) 23135233 = 950, 900, 950 -> 95.0%, 90.0%, 95.0%.
# Both meters are always sent together in one data_set even when only one
# value actually changed.
MQTT_EMS_CHARGE_FLOOR_SOC_METER_ID: str = "23136257"
MQTT_EMS_DISCHARGE_CEILING_SOC_METER_ID: str = "23135233"

# Max charge (input) power limit, in Watts directly (not scaled). Confirmed
# 2026-08-01: 23286785 = 600 then 800, matching the app's stated 600 W then
# 800 W settings exactly.
MQTT_EMS_CHARGE_POWER_LIMIT_METER_ID: str = "23286785"

# Standby toggle. Confirmed 2026-08-01: 23133185 = "1" when entering "veille"
# (standby), "2" when exiting ("sortie de veille"). This is the same meter
# that was already seen once earlier, alongside a battery-mode change (see
# MQTT_EMS_MODE_METER_ID) - the app appears to also wake the device whenever
# the mode is changed, which is why it showed up there too.
MQTT_EMS_STANDBY_METER_ID: str = "23133185"
MQTT_EMS_STANDBY_RAW_ON = "1"
MQTT_EMS_STANDBY_RAW_OFF = "2"

# Max output (discharge) power limit, as a preset index rather than a direct
# Watts value (unlike MQTT_EMS_CHARGE_POWER_LIMIT_METER_ID above, which is a
# direct W value). Confirmed 2026-08-01: the app sent "0" when set to
# "800 W" and "1" when set to "1500 W". Only these 2 presets have been
# observed - there may be more (e.g. a 3rd tier) not yet captured.
MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID: str = "23324673"


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
