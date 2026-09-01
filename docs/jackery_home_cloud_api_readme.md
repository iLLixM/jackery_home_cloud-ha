# Jackery Home Cloud API (Unofficial)

> ⚠️ **Disclaimer**\
> This API documentation is based on reverse engineering of the Jackery
> Home Android app using mitmproxy.\
> It is **unofficial**, may be incomplete, and can change at any time
> without notice.

------------------------------------------------------------------------

## 🌐 Base URL

    https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway

------------------------------------------------------------------------

## 🔐 Authentication

### Login

    POST /geneverse-iot-home/v1/home/auth/login

### Example Request

```json
{
  "encrypted": false,
  "userEnd": "HOME",
  "userType": "2",
  "account": "user@example.com",
  "password": "plain_password",
  "phoneUid": "ha-device-id",
  "loginType": 1,
  "rememberMe": false,
  "clientType": "APP"
}
```

### Notes

- `encrypted: false` works for direct client integrations.
- The official app has been observed using `encrypted: true`.
- `phoneUid` is required but does not appear to need to match a physical device.
- The login response includes:
  - `accessToken`
  - `refreshToken`

### Auth Header

    Authorization: Bearer <accessToken>
    userend: HOME

All API calls are transported over HTTPS. References to plain-text values in
this document describe their representation inside the authenticated HTTPS
payload and do not mean that they are sent over the network without TLS.

------------------------------------------------------------------------

## 👤 User & Systems

### Get User Info

    GET /v1/appUser/getOne

### List Systems

    GET /v1/system/listByUserV2

### Get System Details

    GET /v1/system/{systemId}

------------------------------------------------------------------------

## 📡 Monitoring (Realtime Snapshot)

### System Monitor

    POST /v1/app/monitor/

```json
{
  "systemId": "<systemId>"
}
```

### Key Fields

-   `energyFlowChartVO`
-   `pvInfo.pvPower`
-   `gridVO.gridPower`
-   `otherLoadVO.otherLoadPower`
-   `emsGwVO.soc`

------------------------------------------------------------------------

## 🔌 Devices

### List Devices by System

    GET /v2/home/device/bySystemId/{systemId}

Includes:

-   EMS
-   PCS
-   Battery (BMS / Stack)
-   Smart Meter (CT)

------------------------------------------------------------------------

## 📊 Energy & Trends

### Cluster / Energy Statistics

    POST /v1/app/monitor/energy/cluster/sta

### Important Fields

- `pvChargeAmount` -- Solar generation
- `batteryCharge` -- Battery charged
- `batteryDischarge` -- Battery discharged
- `gridOut` -- Exported to grid
- `gridInput` -- Imported from grid
- `pv1TotalGen` -- PV string 1
- `pv2TotalGen` -- PV string 2

------------------------------------------------------------------------

### Battery Trends (BMS)

    POST /v1/app/monitor/battery/bms

### Observed Types

- `2` -- Daily
- `3` -- Weekly / Range
- `4` -- Monthly

------------------------------------------------------------------------

## 📶 MQTT (Realtime Telemetry and Control)

MQTT is used as a direct realtime transport in addition to the REST API.

The Home Assistant integration establishes its own TLS connection to the
Jackery-hosted MQTT broker. It does not depend on the Home Assistant MQTT
integration.

### Get MQTT Credentials - API v2 (preferred)

    GET /v2/idc/config/mqttServer

Full EU URL:

    https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway/geneverse-iot-home/v2/idc/config/mqttServer

Example response:

```json
{
  "mqttServer": "prodeu-energymanagement-mqtts.hello-tech.com",
  "mqttPort": "8883",
  "mqttUserName": "...",
  "mqttPassword": "..."
}
```

Observed behavior:

- `mqttUserName` is returned by the API.
- `mqttPassword` is returned in plain text inside the authenticated HTTPS
  response and can be passed directly to the MQTT client.

### Get MQTT Credentials - API v1 (legacy fallback)

    GET /v1/idc/config/mqttServer

Full EU URL:

    https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway/geneverse-iot-home/v1/idc/config/mqttServer

The response uses the same general structure:

```json
{
  "mqttServer": "prodeu-energymanagement-mqtts.hello-tech.com",
  "mqttPort": "8883",
  "mqttUserName": "...",
  "mqttPassword": "..."
}
```

Observed behavior differs from v2:

- the legacy `mqttPassword` value is encrypted / encoded,
- it is not directly usable as the MQTT broker password,

### MQTT credential handling summary

| API | MQTT username | MQTT password | Additional decoding |
|---|---|---|---|
| `/v2/idc/config/mqttServer` | returned by API | plain text in HTTPS payload | No |
| `/v1/idc/config/mqttServer` | returned by API | encrypted / encoded | Yes |

Current integration order:

1. Try `/v2/idc/config/mqttServer`.
2. If v2 fails or returns incomplete broker configuration, fall back to
   `/v1/idc/config/mqttServer`.
3. Apply the legacy credential-decoding path only for the v1 result.

> **Security note:**  
> "Plain text" here refers only to the value contained inside the HTTPS API
> response. The REST request and response are still protected in transit by
> HTTPS/TLS.

### MQTT broker

Observed broker configuration:

| Property | Value |
|---|---|
| Protocol | MQTT over TLS |
| Port | `8883` |
| Authentication | Username + password |
| Separate MQTT token | Not observed |
| EU broker | `prodeu-energymanagement-mqtts.hello-tech.com` |

------------------------------------------------------------------------

## 🧩 Additional Endpoints

### Firmware

    GET /v2/upgradeRecord/rom/check/{systemId}

### App Version

    POST /v1/app/common/getAppUpdateVersion

### Language Assets

    GET /v1/platform/language/languageJsonAttach/{id}

### Device Protocol / Model Files

    GET /iot-hub-asset/v1/protocol/protocolFileUrl
    GET /iot-hub-asset/v1/model/modelTslFileUrl

------------------------------------------------------------------------

## ⚡ Integration Notes

- Cloud-based API
- Hybrid REST and MQTT architecture
- REST is used for authentication and system/device discovery
- MQTT is used for realtime telemetry and supported controls
- MQTT broker credentials are preferably retrieved through the v2 API
- v1 MQTT credential retrieval remains available as a legacy fallback
- MQTT uses TLS on port 8883
- Active MQTT polling and spontaneous MQTT reports are both supported
- Selected MQTT writes are verified against fresh device responses
- Used by the Jackery Home Cloud Home Assistant integration

------------------------------------------------------------------------

## 🚧 Known Limitations

- Reverse engineering is incomplete and based on observed app/device behavior.
- The API and MQTT protocol are undocumented and may change at any time.
- `encrypted=true` login used by the app is not required by the current direct
  integration path; login with `encrypted=false` has been confirmed to work.
- MQTT topic, meter, and control mappings may differ by device model, firmware,
  cloud region, or future backend changes.
- MQTT support currently targets one primary system per Home Assistant config
  entry; additional systems remain REST-only.
- The EMS battery-power value is expected to represent system-level battery
  power, including additional battery packs where supported, but broader model
  validation is still useful.
- PCS active power L1 is used directly as the fresh MQTT source for AC main
  power after validation against an independent meter on the tested systems.
  Its semantics may still require validation across other models or firmware.
- Writable controls can change real device behavior and should be used with
  appropriate care.
- Debug logs and captured MQTT payloads may contain device identifiers,
  operational data, account metadata, or credentials and should be redacted
  before sharing.
