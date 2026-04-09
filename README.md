# Jackery Home Cloud for Home Assistant

Unofficial Home Assistant integration for **"Jackery Home" Cloud** systems.

This integration connects to the Jackery cloud backend and exposes system data from supported Jackery Home energy systems inside Home Assistant.

> [!WARNING]
> This project is based on **reverse engineered** API behavior observed from the "Jackery Home" Android app.
> It is **unofficial**, may be incomplete, and can break at any time if Jackery changes their backend.

[API readme](docs/jackery_home_cloud_api_readme.md) for a quick overview.

Comprehensive [API documentation](docs/api.md) with all API calls inspected to date.

---

## Current status

The HA integration and the collection of API calls that I originally created was carried out with and for a **Jackery HomePower 2000 Ultra**.

**Version:** `0.2.0`

The integration is already able to:

- authenticate against the Jackery Home Cloud API
- discover systems linked to the user account
- let the user select one or more systems during setup
- create **one Home Assistant device per selected Jackery system**
- fetch live system snapshot data from the cloud
- expose entities for core system values
- expose daily energy trend entities derived from observed trend endpoints

The current implementation is **cloud polling based**.  
MQTT credential retrieval is already implemented, but the realtime MQTT transport layer is still planned for a future version.

---

## Features in v0.2.0

### System-oriented device model

Each selected Jackery system is represented as **one device in Home Assistant**.

This keeps the integration understandable and avoids clutter from multiple internal cloud-side sub-devices that are not yet independently controlled by the integration.

### Live cloud data

The integration reads current system data such as:

- battery state of charge
- remaining battery energy
- PV power
- grid power
- household / other load power
- operating and status information

### Daily energy entities

Version `0.2.0` adds daily energy sensors based on trend endpoints observed in the Jackery Home app traffic:

- `solar_energy_generated_today`
- `battery_energy_charged_today`
- `battery_energy_discharged_today`
- `grid_energy_exported_today`
- `grid_energy_imported_today`
- `pv1_energy_today`
- `pv2_energy_today`

In addition, diagnostic values can be exposed where useful for further reverse engineering and validation.

---

## Screenshot

![Jackery Home Cloud device view](img/jackery-home-cloud-device-view.png)

---

## Installation

### Option 1: Manual installation

1. Copy the `custom_components/jackery_home_cloud` folder into your Home Assistant configuration directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services**.
4. Add the **Jackery Home Cloud** integration.
5. Enter your Jackery account credentials.
6. Select the systems you want to import.

### Option 2: HACS

HACS packaging is planned, but availability depends on the current repository setup and release state.

---

## Configuration

The integration currently uses:

- "Jackery Home" account email
- "Jackery Home" password
- a generated stable `phone_uid`
- one or more selected system IDs

The integration performs cloud login and then reads system, monitor, device, and trend data from the Jackery backend.

---

## Project goals

This project is intended to become a production-ready Home Assistant integration for Jackery Home Cloud systems.

Current and planned goals include:

- stable cloud authentication
- robust system discovery
- proper Home Assistant device and entity modeling
- daily energy history sensors
- MQTT-based realtime updates
- HACS-ready repository structure
- improved diagnostics and error handling
- continued reverse engineering of unsupported API areas

---

## Technical notes

- The integration is **cloud-based**
- The current `iot_class` is **`cloud_polling`**
- MQTT credentials are already available from the backend, but MQTT topic decoding and push transport are still under development
- The API is **unofficial** and may change without notice
- Login with `encrypted=false` currently works for the integration, while the official app appears to use `encrypted=true`

---

## Compatibility

This integration is currently being developed and tested against the **Jackery Home** cloud platform observed from app version `2.10.22`.

Because the API is reverse engineered, compatibility with all Jackery products, regions, and future backend versions cannot be guaranteed.

---

## Contributing

Contributions are very welcome.

If you are using this project and find problems, please:

- open an issue
- share logs and observations where possible
- describe your Jackery hardware and region
- report entities or values that seem incorrect

If you want to improve the integration, feel free to open a **Pull Request**.

Especially helpful contributions include:

- API observations from additional regions or device families
- MQTT reverse engineering
- validation of energy trend semantics
- testing on additional Jackery Home systems
- code quality, typing, and documentation improvements
- HACS packaging and release workflow improvements

---

## Feedback wanted

Feedback is explicitly encouraged.

Please comment on:

- incorrect entity names or units
- missing sensors
- semantic interpretation of trend values
- device model decisions
- API changes that you observe in newer Jackery app versions

---

## Development status

This is an active work in progress.

The integration already provides useful data in Home Assistant, but it should still be considered **early-stage community software**.

If you test it, review it, or improve it: thank you.

---

## Disclaimer

This repository is **not affiliated with or endorsed by Jackery**.

All product names, trademarks, and brand names are the property of their respective owners.