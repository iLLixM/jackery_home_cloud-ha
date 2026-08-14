"""Constants for the Jackery Home Cloud integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "jackery_home_cloud"
MANUFACTURER = "Jackery"

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_PHONE_UID = "phone_uid"
CONF_SELECTED_SYSTEMS = "selected_systems"
CONF_MQTT_SYSTEM_ID = "mqtt_system_id"
# Set by async_migrate_entry() when a migrated entry has more than one
# selected system and CONF_MQTT_SYSTEM_ID can't be defaulted without live
# API data (see coordinator._resolve_pending_mqtt_system_selection). Cleared
# once the coordinator's first successful refresh resolves and persists a
# real CONF_MQTT_SYSTEM_ID.
CONF_MQTT_SYSTEM_SELECTION_PENDING = "mqtt_system_selection_pending"
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
# Fixed (non-user-configurable) cadence for reconciling the config/schedule
# meter group against the device (discussion #6, item 8, "Configuration
# reconciliation for external changes"). This group is otherwise only
# requested on MQTT reconnect and right after a write targeting it (see
# refresh_group on async_set_meter_value) - this timer is what catches
# settings changed through the Jackery app instead of through HA.
MQTT_CONFIG_RECONCILE_INTERVAL_SECONDS = 1800
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

# confirmed: value/scale validated from observed MQTT traffic
# PROPERTY_MAPPING "21548033":"HB-EMS-MODEL_systemSoc"
MQTT_EMS_BATTERY_SOC_METER_ID: str = "21548033"
MQTT_EMS_BATTERY_SOC_SCALE = 10.0

# confirmed: value/scale validated from observed MQTT traffic
# PROPERTY_MAPPING "50490369": "HB-PCS-MODEL_pvP1"
# PROPERTY_MAPPING "50490370": "HB-PCS-MODEL_pvP2"
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
# Candidate, unconfirmed: the meter ID -> field mapping itself is not backed
# by a PROPERTY_MAP entry, only by observed traffic. See CONTRIBUTING.md's
# MQTT-vs-REST / AC main power sign validation checklist before removing
# this hedge.
# Confirmed via live validation on 2026-08-09: the sign derivation above
# (math.copysign(ac_main_magnitude, -battery_power_signed)) produced the
# physically correct sign during both a user-triggered forced charge
# (negative) and a user-triggered discharge (positive), cross-checked
# against the battery_soc trend and an exact grid_power match with REST at
# the same moment. The magnitude/scale of this raw meter is still NOT
# independently confirmed - REST's own acMainPower value cannot serve as a
# fast ground truth for that comparison (see the cloud-side lag note on
# MQTT_EMS_OTHER_LOAD_POWER_METER_ID below).
MQTT_PCS_AC_MAIN_POWER_METER_ID: str = "50416641"

# Instantaneous system battery charge/discharge power reported by the EMS.
# This value is expected to represent the combined battery power of the
# complete system, including additional battery packs.
# Observed raw values use the opposite sign convention from the Home
# Assistant entity: positive while discharging and negative while charging.
# The coordinator therefore negates the raw value before storing it.
# PROPERTY_MAPPING "16931841":"HB-EMS-MODEL_batteryPower"
MQTT_EMS_BATTERY_POWER_METER_ID: str = "16931841"

# Instantaneous charge/discharge power of the specific main-unit 
# battery (bms1). Negative while discharging, positive while charging.
# PROPERTY_MAPPING "33659905":"HB-BMS-MODEL_power"
MQTT_BMS1_BATTERY_POWER_METER_ID: str = "33659905"

# Household load power, signed like REST other_load_power itself. Do not
# confuse with the unsigned MQTT_PCS_AC_MAIN_POWER_METER_ID above - they
# read the same value only when grid_power is ~0.
# PROPERTY_MAPPING "16936961":"HB-EMS-MODEL_otherLoadPower"
# Confirmed via live validation on 2026-08-09: REST's otherLoadPower (and
# acMainPower above) update noticeably slower on Jackery's cloud backend
# than grid_power/soc do. During a live forced-charge -> discharge
# transition, REST's value for this field stayed frozen across several
# consecutive successful REST polls (confirmed via
# coordinator.last_rest_update_success_at advancing) while MQTT tracked the
# real change immediately. A REST/MQTT mismatch on this specific field is
# not by itself evidence of a wrong MQTT mapping - check whether REST has
# actually caught up before concluding anything.
MQTT_EMS_OTHER_LOAD_POWER_METER_ID: str = "16936961"

# Raw value is sign-flipped relative to REST grid_power, so store as -raw.
# Negative while "Power fed into the grid"; positive while "Power drawn from the grid"
# may require a connected smart meter to show appropriate values
# PROPERTY_MAPPING "16930817":"HB-EMS-MODEL_gridPower"
MQTT_EMS_GRID_POWER_METER_ID: str = "16930817"

# Power delivered through the unit's own physical AC output sockets,
# gated by the AC output relay (MQTT_EMS_AC_OUTPUT_METER_ID).
# negative while external power is fed into the AC socket
# positive while power is consumed externally from the AC socket
# PROPERTY_MAPPING "16933889":"HB-EMS-MODEL_epsLoadPower"
MQTT_EMS_EPS_LOAD_POWER_METER_ID: str = "16933889"

# Battery priority/mode register. See MODE_OPTIONS in select.py for the
# value mapping. Mode "5" (Time of use) only selects that mode - it does
# not itself configure the schedule, which lives in the separate
# charge/discharge time-window table below (MQTT_EMS_CHARGE_WINDOW_METER_IDS
# / MQTT_EMS_DISCHARGE_WINDOW_METER_IDS).
# PROPERTY_MAPPING "23132161": "HB-EMS-MODEL_workMode"
MQTT_EMS_WORK_MODE_METER_ID: str = "23132161"

# Scheduled charge/discharge time-window table, only takes effect while
# MQTT_EMS_WORK_MODE_METER_ID == "5". Each meter holds an 8-digit "HHMMHHMM"
# string (start+end, no separator) or "0" for an unused slot. The two
# 10-meter ranges are INDEPENDENT sequential lists, not paired per-cycle:
# MQTT_EMS_CHARGE_WINDOW_METER_IDS[0] is always the first charge window
# entered, [1] the second, etc., same for discharge in the other list.
MQTT_EMS_CHARGE_WINDOW_METER_IDS: tuple[str, ...] = tuple(str(23146497 + i) for i in range(10))
MQTT_EMS_DISCHARGE_WINDOW_METER_IDS: tuple[str, ...] = tuple(str(23147521 + i) for i in range(10))

# Battery SOC operating window: discharge limit (the SOC below which
# discharging stops) and charge limit (the SOC above which charging
# stops). Both are raw / 10, same scale as MQTT_EMS_BATTERY_SOC_SCALE.
# The official Android app was observed sending both SOC boundary meters in
# the same data_set request even when only one value was changed. Direct MQTT
# testing confirms that both meters can also be read and written independently.
MQTT_EMS_DISCHARGE_LIMIT_SOC_METER_ID: str = "23136257"
MQTT_EMS_CHARGE_LIMIT_SOC_METER_ID: str = "23135233"

# Max feed-in power limit, in Watts directly (not scaled, unlike the
# SOC meters above). Value limited to "800" in android app.
# That is the maximum power that may be fed into the 
# grid - meaning, fed out of the home's electrical system.
# A connected smart meter may be required for it to take effect correctly.

MQTT_EMS_FEED_POWER_LIMIT_METER_ID: str = "23286785"
MQTT_EMS_FEED_POWER_LIMIT_MAX_W: int = 800

# Standby toggle (labeled "Standby" / "Exit standby" in the app). See
# MQTT_EMS_STANDBY_RAW_ON / MQTT_EMS_STANDBY_RAW_OFF below for the raw
# values.
MQTT_EMS_STANDBY_METER_ID: str = "23133185"
MQTT_EMS_STANDBY_RAW_ON = "1"
MQTT_EMS_STANDBY_RAW_OFF = "2"

# Max output (discharge) power limit, as a preset index rather than a direct
# Watts value (unlike MQTT_EMS_FEED_POWER_LIMIT_METER_ID above, which is a
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

# Confirmed device capabilities, keyed by the REST `factoryModel` field (the
# same source _friendly_model_name()/MODEL_NAME_MAP above use). Only
# JAKS-IN1K5-BA2K-EUA1 has ever been tested against a real device (see
# README.md) - every meter id below was reverse engineered against that
# specific unit (see CONTRIBUTING.md #1 for how each was identified).
#
# A model with NO entry here is "unconfirmed", not "unsupported": see
# JackeryHomeCloudCoordinator.supports_meter()'s fallback policy in
# coordinator.py. We deliberately do NOT default an unmapped model to a
# reduced entity set, because we have no evidence either way for it and
# doing so would risk silently removing working entities for an existing
# user on a model that happens to work but was simply never added here.
# Unconfirmed-model entities are instead created disabled-by-default (see
# is_model_confirmed() usage in number.py/select.py/switch.py/button.py/
# sensor.py) so a user must explicitly opt in.
MODEL_CAPABILITIES: dict[str, frozenset[str]] = {
    "JAKS-IN1K5-BA2K-EUA1": frozenset(
        {
            MQTT_EMS_REBOOT_METER_ID,
            MQTT_EMS_AC_OUTPUT_METER_ID,
            MQTT_EMS_WORK_MODE_METER_ID,
            MQTT_EMS_OUTPUT_POWER_LIMIT_METER_ID,
            MQTT_EMS_DISCHARGE_LIMIT_SOC_METER_ID,
            MQTT_EMS_CHARGE_LIMIT_SOC_METER_ID,
            MQTT_EMS_FEED_POWER_LIMIT_METER_ID,
            MQTT_EMS_STANDBY_METER_ID,
            MQTT_EMS_AUTO_STANDBY_METER_ID,
        }
    ),
}
