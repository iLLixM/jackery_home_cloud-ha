# Jackery Home Cloud API (Unofficial – v2.10.22)

## Disclaimer

- This is an unofficial API.
- It was reverse engineered from Jackery Home Android app traffic using mitmproxy.
- The API may change without notice.
- Use at your own risk.

## Scope of This Document

This document summarizes the currently identified **Jackery Home Cloud** REST endpoints and related observations from app version **2.10.22**.

It focuses on:

- authentication
- system and device discovery
- monitor and trend data
- firmware / metadata endpoints
- MQTT credential retrieval

## Base URL

```text
https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway
```

## Authentication

### Login Endpoint

```text
POST /geneverse-iot-home/v1/home/auth/login
```

### Working Request Body

A working login is possible with a plain-text password when `encrypted` is set to `false`.

```json
{
  "encrypted": false,
  "userEnd": "HOME",
  "userType": "2",
  "account": "user@example.com",
  "password": "plain_password",
  "phoneUid": "device-id",
  "loginType": 1,
  "rememberMe": false,
  "clientType": "APP"
}
```

### Notes

- `userEnd` must be `HOME`
- `userType` must be `2`
- `clientType` must be `APP`
- `phoneUid` is required, but does not appear to need to match a real physical device
- `encrypted: true` is still used by the official app, but `encrypted: false` works for direct client integrations

### Authentication Header

All authenticated requests use:

```text
Authorization: Bearer <accessToken>
```

The app also sends:

```text
userend: HOME
```

## Common Request Headers

Observed common headers include:

```text
user-agent: Dart/3.11 (dart:io)
accept-language: en-US
model: Phone
accept-encoding: gzip
x-app-name: Custom-Phone
content-type: application/json;charset=UTF-8
x-app-version: home_android_v2.10.22
sdkint: 34
id: UP1A.231105.003.A1
userend: HOME
```

Not every request includes every header, but this is the general pattern.

## Endpoint List

Below is the consolidated endpoint list identified from the mitmproxy flow for app version 2.10.22.

---

## 1. Authentication and Account

### 1.1 Login

```text
POST /geneverse-iot-home/v1/home/auth/login
```

Purpose:
- user authentication
- returns access token, refresh token, and basic user info

### 1.2 Login Status

```text
GET /geneverse-iot-home/v1/home/auth/loginStatus
```

Purpose:
- likely used by the app to verify whether the current session is still valid

### 1.3 Update Language

```text
POST /geneverse-iot-home/v1/home/auth/updateLanguage
```

Purpose:
- update user language preference in cloud context

### 1.4 Update Firebase Token

```text
POST /geneverse-iot-home/v1/home/auth/updateFirebaseToken
```

Purpose:
- likely used for push notification token registration

### 1.5 Send Email Code

```text
POST /geneverse-iot-home/v1/home/auth/send/emailCode
```

Purpose:
- send verification code to email
- used for account registration or password reset flows

### 1.6 Confirm Code

```text
POST /geneverse-iot-home/v1/home/auth/confirmCode
```

Purpose:
- confirm email verification / auth code

### 1.7 Forgot Password

```text
POST /geneverse-iot-home/v1/home/auth/forgetPassword
```

Purpose:
- initiate forgot-password flow

### 1.8 Update Password

```text
POST /geneverse-iot-home/v1/home/auth/update/password
```

Purpose:
- change or reset account password

### 1.9 Logout

```text
POST /geneverse-iot-home/v1/home/auth/logout
```

Purpose:
- terminate current session

### 1.10 Cancel Account

```text
POST /geneverse-iot-home/v1/home/auth/cancel
```

Purpose:
- account cancellation / deletion workflow

### 1.11 Register Login

```text
POST /geneverse-iot-home/v1/home/auth/registerLogin
```

Purpose:
- combined registration/login flow using email verification code
- not the normal password login endpoint

---

## 2. App User / Profile

### 2.1 App User Info

```text
GET /geneverse-iot-home/v1/appUser/getOne
```

Purpose:
- returns app user metadata such as:
  - userId
  - email
  - country
  - ownerSysCount
  - flowSysCount
  - language
  - appVersion

### 2.2 Update Measurement Unit

```text
POST /geneverse-iot-home/v1/appUser/updateMedida
```

Purpose:
- likely updates measurement unit / locale-specific user settings

### 2.3 Current Notice

```text
GET /geneverse-iot-home/v1/home/appUserNotice/getCurrentNotice
```

Purpose:
- app notice / announcement retrieval

### 2.4 Update Notice State

```text
POST /geneverse-iot-home/v1/home/appUserNotice/updateNotice
```

Purpose:
- mark notice as handled / read / dismissed

---

## 3. Systems

### 3.1 List Systems by User

```text
GET /geneverse-iot-home/v1/system/listByUserV2
```

Purpose:
- primary system list endpoint
- returns systems owned or visible to the current account

### 3.2 Get System Count

```text
GET /geneverse-iot-home/v1/system/getSystemCount
```

Purpose:
- returns system count summary

### 3.3 Get Single System

```text
GET /geneverse-iot-home/v1/system/<systemId>
```

Purpose:
- retrieve details for a specific system

### 3.4 Get System Device Data

```text
GET /geneverse-iot-home/v1/system/systemDeviceDataGet/<systemId>
```

Purpose:
- likely returns aggregated system/device data for a specific installation

### 3.5 Get Series by SN

```text
GET /geneverse-iot-home/v1/system/getSeriesBySn/<serial>
```

Purpose:
- determine product series by serial number

### 3.6 Submit Operating Mode

```text
POST /geneverse-iot-home/v1/system/operatingMode/submit
```

Purpose:
- appears to be a control/configuration endpoint

### 3.7 Submit Basic Info

```text
POST /geneverse-iot-home/v1/system/basicInfo/submit
```

Purpose:
- update editable system basic data

### 3.8 Cancel Networking

```text
POST /geneverse-iot-home/v1/system/cancelNetworking/
```

Purpose:
- likely used in device/system networking workflows

### 3.9 Remove System

```text
POST /geneverse-iot-home/v1/system/epcSystemRemove/
```

Purpose:
- remove EPC/home system

### 3.10 Delete System by SystemNo

```text
POST /geneverse-iot-home/v1/system/deleteBySystemNo/
```

Purpose:
- delete or unbind system by system number

### 3.11 EPC System Detail Update

```text
POST /geneverse-iot-home/v1/system/epcSystemDetailUpdate
```

Purpose:
- update EPC system configuration/details

### 3.12 EPC System Detail V2

```text
GET /geneverse-iot-home/v1/system/epcSystemDetailVOV2/<systemId>
```

Purpose:
- EPC system detail endpoint

### 3.13 Grid Info Submit

```text
POST /geneverse-iot-home/v1/system//gridInfo/submit/
```

Purpose:
- grid info submission/update
- note the double slash was visible in string extraction; actual runtime request format may normalize this

---

## 4. Devices

### 4.1 System Device List (V2)

```text
GET /geneverse-iot-home/v2/home/device/bySystemId/<systemId>
```

Purpose:
- primary device list per system
- returns all known devices in a system

### 4.2 Device Detail by DeviceNo

```text
GET /geneverse-iot-home/v1/home/device/detail?deviceNo=<deviceNo>
```

Purpose:
- device detail lookup

### 4.3 Device Detail Property

```text
GET /geneverse-iot-home/v1/home/device/detail/property
```

Purpose:
- property-oriented detail endpoint for a device

### 4.4 Get Device Bind

```text
GET /geneverse-iot-home/v1/home/device/getDeviceBind/<systemId or device context>
```

Purpose:
- binding-related device lookup

### 4.5 Device by DeviceNo

```text
GET /geneverse-iot-home/v1/home/device/byDeviceNo/<deviceNo>
```

Purpose:
- fetch a device by serial / device number

### 4.6 Delete Device by DeviceNo

```text
POST /geneverse-iot-home/v1/home/device/deleteByDeviceNo/<deviceNo>
```

Purpose:
- unbind/remove device

### 4.7 Device Offline Info

```text
GET /geneverse-iot-home/v1/home/device/deviceOffline/<deviceNo>
```

Purpose:
- offline state / offline-related device info

### 4.8 Device Update

```text
POST /geneverse-iot-home/v1/home/device/update
```

Purpose:
- update device information

### 4.9 Device Property Report

```text
POST /geneverse-iot-home/v1/home/device/propertyReport
```

Purpose:
- likely cloud-side reporting or status upload endpoint used by the app/device workflow

### 4.10 Set Device Property

```text
POST /geneverse-iot-home/v1/home/device/property/set/
```

Purpose:
- send property write/update

### 4.11 DIY EPC Device List

```text
GET /geneverse-iot-home/v1/home/device/diyEpcDeviceList?systemId=<systemId>
```

Purpose:
- return DIY / EPC specific device grouping

### 4.12 EPC Device Detail

```text
GET /geneverse-iot-home/v1/home/device/epcDeviceDetail
```

Purpose:
- EPC device detail

### 4.13 Plug Detail V2

```text
GET /geneverse-iot-home/v1/home/device/plugDetailV2
```

Purpose:
- plug-specific detail endpoint

### 4.14 Plug Electricity

```text
GET /geneverse-iot-home/v1/home/device/plug/getElectricity
```

Purpose:
- electricity consumption for plug device

### 4.15 CT Detail

```text
GET /geneverse-iot-home/v1/home/device/ct/detail
```

Purpose:
- CT meter / smart meter detail

### 4.16 CT Energy List

```text
GET /geneverse-iot-home/v1/home/device/ct/energyList
```

Purpose:
- CT energy history/list endpoint

---

## 5. Monitor / Dashboard

### 5.1 Main Monitor Snapshot

```text
POST /geneverse-iot-home/v1/app/monitor/
```

Purpose:
- primary dashboard snapshot endpoint
- returns:
  - total charge amount
  - total income
  - co2
  - systemVO
  - systemConfigVO
  - energyFlowChartVO

### 5.2 Shortcut Control

```text
POST /geneverse-iot-home/v1/app/monitor/shortcutControl/<systemId>
```

Purpose:
- app quick-action / shortcut control endpoint
- likely important for future writable HA functionality

---

## 6. Trend / History Endpoints

### 6.1 Load Power Trend

```text
POST /geneverse-iot-home/v1/app/trend/loadPower/
```

Purpose:
- load power trend/history

### 6.2 Grid Power Trend

```text
POST /geneverse-iot-home/v1/app/trend/gridPower/
```

Purpose:
- grid power trend/history

### 6.3 Battery / BMS Trend

```text
POST /geneverse-iot-home/v1/app/trend/battery/bms/
```

Purpose:
- battery trend/history

### 6.4 Cluster Status Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/sta
```

Purpose:
- cluster status trend

### 6.5 Cluster SOC / Temperature Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/socTemp
```

Purpose:
- state of charge / temperature trend

### 6.6 Cluster Charge Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/charge/V2
```

Purpose:
- charge trend

### 6.7 Cluster Grid Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/grid
```

Purpose:
- grid trend

### 6.8 Cluster PV Power Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/pvPower
```

Purpose:
- PV trend

### 6.9 Cluster Output Trend

```text
POST /geneverse-iot-home/v2/app/trend/cluster/outPut
```

Purpose:
- output trend

---

## 7. Alarms / Messages / Notifications

### 7.1 Alarm Record Page

```text
POST /geneverse-iot-home/v1/app/alarm/record/page
```

Purpose:
- paginated alarm list

### 7.2 Alarm Name Dictionary

```text
GET /geneverse-iot-home/v1/app/alarm/record/alarmName
```

Purpose:
- alarm name metadata / dictionary

### 7.3 Has Message

```text
GET /geneverse-iot-home/v1/home/messageRecord/hasMessage
```

Purpose:
- indicates whether unread app messages exist

### 7.4 Platform Notify Page

```text
GET /geneverse-iot-home/v1/home/messageRecord/platformNotifyPage
```

Purpose:
- notification list / page

### 7.5 Get One Message Record

```text
GET /geneverse-iot-home/v1/home/messageRecord/getOne
```

Purpose:
- single message retrieval

### 7.6 Read Message

```text
POST /geneverse-iot-home/v1/home/messageRecord/read
```

Purpose:
- mark message as read

### 7.7 Weather Record Read

```text
POST /geneverse-iot-home/v1/home/messageRecord/weatherAllRecordRead
```

Purpose:
- mark weather records as read

### 7.8 Platform Record Read

```text
POST /geneverse-iot-home/v1/home/messageRecord/platformAllRecordRead
```

Purpose:
- mark platform messages as read

### 7.9 Weather Warning Record Detail

```text
GET /geneverse-iot-home/v1/home/weatherWarningRecord/getOne
```

Purpose:
- weather warning detail

---

## 8. Updates / Firmware / Upgrade

### 8.1 App Update Version

```text
GET /geneverse-iot-home/v1/app/common/getAppUpdateVersion
```

Purpose:
- app update metadata

### 8.2 Upgrade Record Update

```text
POST /geneverse-iot-home/v1/upgradeRecord/update
```

Purpose:
- firmware / upgrade tracking

### 8.3 Upgrade Start

```text
POST /geneverse-iot-home/v1/upgradeRecord/upgrade
```

Purpose:
- trigger or manage upgrade

### 8.4 Bluetooth Upgrade

```text
POST /geneverse-iot-home/v1/upgradeRecord/upgrade/bluetooth
```

Purpose:
- bluetooth-based upgrade path

### 8.5 ROM Check

```text
GET /geneverse-iot-home/v2/upgradeRecord/rom/check/<systemId>
```

Purpose:
- check firmware/ROM availability

### 8.6 ROM Attachment

```text
GET /geneverse-iot-home/v1/upgradeRecord/romAttach
```

Purpose:
- retrieve upgrade attachment / metadata

---

## 9. Dictionaries / Metadata / Static Data

### 9.1 Time Zones

```text
GET /geneverse-iot-home/v1/dictionary/timeZone
```

Purpose:
- timezone list

### 9.2 Predefined Currency Values

```text
GET /geneverse-iot-home/v1/dictionary/predefined/values/currency
```

Purpose:
- currency dictionary

### 9.3 Feedback Type Dictionary

```text
GET /geneverse-iot-home/v1/dictionary/predefined/values/feedBackType
```

Purpose:
- feedback category list

### 9.4 Dynamic Price List

```text
GET /geneverse-iot-home/v1/dynamic/price/list
```

### 9.5 Dynamic Price Detail

```text
GET /geneverse-iot-home/v1/dynamic/price/detail/
GET /geneverse-iot-home/v1/dynamic/price/price/detail
GET /geneverse-iot-home/v1/dynamic/price/price/detail/
```

### 9.6 Dynamic Price Save

```text
POST /geneverse-iot-home/v1/dynamic/price/save
```

### 9.7 Dynamic Price Config

```text
GET /geneverse-iot-home/v1/dynamic/price/config/
```

### 9.8 Dynamic Price Cancel Config

```text
POST /geneverse-iot-home/v1/dynamic/price/config/cancel/
```

### 9.9 Dynamic Price Contracts

```text
GET /geneverse-iot-home/v1/dynamic/price/contracts/
```

### 9.10 Dynamic Price Customer Link

```text
GET /geneverse-iot-home/v1/dynamic/price/customer/link/
```

Purpose:
- dynamic electricity tariff / pricing features

---

## 10. Documentation / FAQ / Manuals / Feedback

### 10.1 FAQ Page

```text
GET /geneverse-iot-home/v1/home/faq/page
```

### 10.2 User Manual

```text
GET /geneverse-iot-home/v1/userManual
```

### 10.3 Feedback Submit

```text
POST /geneverse-iot-home/v1/home/feedback
```

### 10.4 User Privacy Policy

```text
GET /geneverse-iot-home/v1/home/userPrivacyPolicy/getOne
GET /geneverse-iot-home/v1/home/userPrivacyPolicy/downloadSelf
```

### 10.5 Captcha

```text
GET /geneverse-iot-home/v1/home/captcha
```

---

## 11. MQTT Configuration

The Jackery Home Cloud API exposes MQTT broker connection information through the `idc/config/mqttServer` endpoint.

Two API versions have been identified. Both return the same general broker configuration, but they differ in how the MQTT credentials are represented.

### 11.1 MQTT Server Config - API v1

```text
GET /geneverse-iot-home/v1/idc/config/mqttServer
```

Purpose:
- retrieve MQTT connection settings for the authenticated user / system context
- legacy MQTT credential endpoint

Example EU URL:

```text
https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway/geneverse-iot-home/v1/idc/config/mqttServer
```

### 11.2 MQTT Server Config - API v2

```text
GET /geneverse-iot-home/v2/idc/config/mqttServer
```

Purpose:
- retrieve MQTT connection settings for the authenticated user / system context
- current preferred MQTT credential endpoint

Example EU URL:

```text
https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway/geneverse-iot-home/v2/idc/config/mqttServer
```

### Response Shape

Both versions return the same general response structure:

```json
{
  "mqttServer": "prodeu-energymanagement-mqtts.hello-tech.com",
  "mqttPort": "8883",
  "mqttUserName": "...",
  "mqttPassword": "..."
}
```

### Credential Handling

Observed behavior differs between the two API versions:

| API endpoint | MQTT username | MQTT password | Additional credential decryption required |
|---|---|---|---|
| `v1/idc/config/mqttServer` | returned by API | encrypted / encoded | Yes |
| `v2/idc/config/mqttServer` | returned by API | plaintext | No |

The current integration therefore prefers the endpoints in the following order:

1. `GET /geneverse-iot-home/v2/idc/config/mqttServer`
2. `GET /geneverse-iot-home/v1/idc/config/mqttServer` as legacy fallback

If the `v2` request succeeds and returns a complete MQTT configuration, the returned credentials are treated as directly usable broker credentials. If `v2` fails or returns incomplete data, the integration can fall back to `v1` and apply the legacy credential-decoding path.

> **Security note:**  
> "Plaintext" in this context describes the representation of the MQTT credential inside the authenticated HTTPS API response. It does **not** mean that the credential is transmitted over the network without transport encryption. The REST request itself is protected by HTTPS/TLS.

### Observations

- MQTT authentication uses:
  - `mqttUserName`
  - `mqttPassword`
- No separate MQTT token was observed in the REST traffic.
- The broker uses MQTT over TLS.
- The observed broker port is `8883`.
- The integration connects directly to the Jackery-hosted MQTT broker.
- The `v2` endpoint removes the need for the legacy MQTT password-decryption step when its response is available.
- The `v1` endpoint remains relevant as a backward-compatibility fallback.
- The actual MQTT publish/subscribe traffic was not visible as decoded application data in the original mitmproxy REST flow.

---

## 12. Asset / Model / Protocol Metadata

### 12.1 Model TSL File URL

```text
GET /iot-hub-asset/v1/model/modelTslFileUrl
```

Purpose:
- retrieve a downloadable model/TSL definition

### 12.2 Protocol File URL

```text
GET /iot-hub-asset/v1/protocol/protocolFileUrl
```

Purpose:
- retrieve downloadable protocol metadata

These endpoints may be extremely useful for future reverse engineering of:
- device property definitions
- writable command fields
- telemetry field meaning

## MQTT Summary

| Property | Value |
|---|---|
| Protocol | MQTT over TLS |
| Port | 8883 |
| Authentication | Username + Password |
| MQTT config API | `v2/idc/config/mqttServer` preferred; `v1/idc/config/mqttServer` fallback |
| v1 credential representation | MQTT password encrypted / encoded |
| v2 credential representation | MQTT password plaintext inside HTTPS response |
| Separate MQTT token | Not observed |
| Broker hostname | `prodeu-energymanagement-mqtts.hello-tech.com` |

## Architectural Summary

Current understanding:

1. The app authenticates against the Jackery Home Cloud REST API.
2. It retrieves:
   - app user info
   - systems
   - devices
   - dashboard/monitor snapshots
   - MQTT broker credentials
3. MQTT broker credentials can currently be retrieved from:
   - the preferred `v2` MQTT configuration endpoint, which returns directly usable credentials
   - the legacy `v1` endpoint, which requires credential decoding
4. It then opens a direct TLS MQTT connection to the broker for realtime data and device communication.

## Stability Assessment vs Previous Analysis

The 2.10.22 flow validates previous findings:

- login endpoint remains unchanged
- required login semantics remain unchanged
- REST system/device/monitor flow remains valid
- MQTT credential retrieval remains valid

Additional MQTT credential analysis identified:

- `v2/idc/config/mqttServer` as the preferred MQTT configuration endpoint
- plaintext MQTT credentials in the authenticated HTTPS `v2` response
- encrypted / encoded MQTT credentials in the legacy `v1` response
- retained `v1` support as a compatibility fallback

New findings in 2.10.22 mainly include:

- additional trend/history endpoints
- more device detail endpoints
- shortcut control endpoint
- model/protocol asset endpoints
- continued evidence of a cloud-plus-MQTT architecture

## Current Status

| Feature | Status |
|---|---|
| Login | Confirmed working |
| Plain password login (`encrypted=false`) | Confirmed working |
| App user retrieval | Confirmed working |
| System list retrieval | Confirmed working |
| Device list retrieval | Confirmed working |
| Monitor snapshot retrieval | Confirmed working |
| MQTT credential retrieval | Confirmed working |
| MQTT credential retrieval via v2 | Confirmed working |
| Plaintext MQTT credentials in v2 HTTPS response | Confirmed working / observed |
| Legacy encrypted MQTT credentials via v1 | Confirmed / retained as fallback |
| MQTT topic reverse engineering | Partially identified and implemented |
| Writable control path | Partially identified and implemented |

## Recommended Next Steps

1. Continue reverse engineering MQTT topic structure and payloads
2. Continue validation of writable MQTT controls and meter mappings across additional device models
3. Investigate `shortcutControl/<systemId>` for additional REST-based writable controls
4. Download and inspect TSL / protocol assets
5. Add refresh-token or re-login handling in client implementations
6. Continue using REST as bootstrap/discovery and MQTT as realtime transport in Home Assistant
