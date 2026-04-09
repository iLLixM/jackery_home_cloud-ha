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

``` json
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

-   `encrypted: false` works (app uses `true`)
-   `phoneUid` required but arbitrary
-   Returns:
    -   `accessToken`
    -   `refreshToken`

### Auth Header

    Authorization: Bearer <accessToken>
    userend: HOME

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

``` json
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

-   `pvChargeAmount` -- Solar generation\
-   `batteryCharge` -- Battery charged\
-   `batteryDischarge` -- Battery discharged\
-   `gridOut` -- Exported to grid\
-   `gridInput` -- Imported from grid\
-   `pv1TotalGen` -- PV string 1\
-   `pv2TotalGen` -- PV string 2

------------------------------------------------------------------------

### Battery Trends (BMS)

    POST /v1/app/monitor/battery/bms

### Observed Types

-   2 -- Daily\
-   3 -- Weekly / Range\
-   4 -- Monthly

------------------------------------------------------------------------

## 📶 MQTT (Realtime Push)

### Get MQTT Credentials

    GET /v1/idc/config/mqttServer

Example:

``` json
{
  "mqttServer": "...",
  "mqttPort": "8883",
  "mqttUserName": "...",
  "mqttPassword": "..."
}
```

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

-   Cloud-based API
-   Polling and MQTT possible
-   Used in Home Assistant integration

------------------------------------------------------------------------

## 🚧 Known Limitations

-   Incomplete reverse engineering
-   `encrypted=true` login not implemented
-   MQTT topics not fully decoded
-   API may change anytime

------------------------------------------------------------------------

## 📚 Related

https://github.com/iLLixM/jackery_home_cloud
