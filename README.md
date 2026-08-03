# Jackery Home Cloud for Home Assistant

Unofficial Home Assistant integration for **Jackery Home Cloud** energy systems.

This integration connects to the Jackery cloud backend, discovers systems linked to a Jackery Home account, and exposes cloud and MQTT-backed data as Home Assistant devices, sensors, controls, and diagnostics.

> [!WARNING]
> This project is based on reverse-engineered API and MQTT behavior observed from the Jackery Home Android app and supported hardware.
> It is unofficial, may be incomplete, and can break at any time if Jackery changes its backend, app, firmware, MQTT topics, or certificate infrastructure.

See the [API readme](docs/api.md) for a quick overview.

See the comprehensive [API documentation](docs/jackery_home_cloud_api_readme.md) for observed API calls and implementation notes.

---

## Current status

The Home Assistant integration and the associated API and MQTT research were primarily developed and validated with a **Jackery HomePower 2000 Ultra**.

Current release: `0.3.0`

The integration is currently able to:

- authenticate against the Jackery Home Cloud API
- automatically generate the required `phone_uid`
- discover systems linked to the user account
- let the user select one or more systems during setup
- create one Home Assistant device per selected Jackery system
- fetch current system data from the cloud
- expose daily energy trend entities
- optionally establish a direct connection to the Jackery cloud MQTT broker
- process MQTT telemetry and device-status messages
- expose cumulative MQTT energy totals
- control AC output through MQTT
- request a device reboot through MQTT
- provide reconfigure and options flows
- reload the config entry after relevant option changes

The integration combines cloud polling with optional cloud MQTT communication.

MQTT support does not use Home Assistant's own MQTT integration. The Jackery MQTT broker credentials are retrieved from the Jackery cloud API and used directly by this integration.

---

## Features in v0.3.0

### System-oriented device model

Each selected Jackery system is represented as one device in Home Assistant.

This keeps the integration understandable and avoids unnecessary clutter from multiple internal cloud-side components that are not independently modeled by the integration.

### Live cloud data

The integration reads current system data such as:

- battery state of charge
- remaining battery energy
- PV power
- grid power
- household or other load power
- operating and status information

### Daily energy entities

Daily energy sensors are derived from observed Jackery cloud trend endpoints:

- `solar_energy_generated_today`
- `battery_energy_charged_today`
- `battery_energy_discharged_today`
- `grid_energy_exported_today`
- `grid_energy_imported_today`
- `pv1_energy_today`
- `pv2_energy_today`

Daily battery values are API-based. They are intentionally not sourced from similarly named MQTT meter values because those meter semantics were found to be unsuitable for reliable daily totals.

### Optional MQTT connection

MQTT can be enabled during setup or later through the integration options.

When enabled, the integration:

- retrieves MQTT credentials from the Jackery cloud API
- establishes a TLS connection to the Jackery MQTT broker
- subscribes to device-specific telemetry and LWT topics
- processes cyclic `data_report` messages
- processes `data_get` and `data_set` responses
- publishes device-control commands
- actively polls live MQTT meters via `data_get` requests sent on every MQTT reconnect and, thereafter, on two recurring schedules, since the device does not proactively broadcast all of these values on its own: fast-changing values (AC output state, battery SOC, grid/PV/EPS/battery power) on the configurable interval (5-60 seconds, default 60 - shorter intervals increase MQTT traffic load), and cumulative energy totals (battery charged/discharged, PV1/PV2/PV total) on a fixed, slower interval. Configuration and schedule values are only requested on reconnect and immediately after a write

The integration supports an option to ignore invalid or expired MQTT TLS certificates. This is currently necessary because Jackery uses its own CA (not public trusted) for its certificates.

### MQTT-based cumulative energy entities

The following cumulative energy entities are derived from MQTT meter reports:

- **Battery charged**
- **Battery discharged**
- **PV1 energy**
- **PV2 energy**
- **PV energy total**

The integration includes monotonicity guards to reject unexpected lower cumulative values that could otherwise distort Home Assistant history or Energy Dashboard statistics.

The MQTT total entities also use state restoration so that the last known value can remain available until a new valid report is received.

### AC output control

When MQTT is enabled, the integration creates an **AC Output** switch.

The switch:

- requests its current state after MQTT connection
- updates from `data_get` and `data_set` responses
- sends `data_set` commands to turn AC output on or off
- keeps the last valid state until a newer state is received

### Device reboot

When MQTT is enabled, the integration creates a **Reboot device** button.

Pressing the button sends the corresponding MQTT command to the selected Jackery system.

### Device connection status

The **Device connection** diagnostic entity reflects the latest MQTT last-will or status message of the solar generator device:

- `online`
- `offline`

The last known state remains valid until a newer status message is received.

### MQTT diagnostics

When MQTT is enabled, the integration can provide:

- MQTT connection status
- Device connection
- MQTT message count
- MQTT last message at
- MQTT last topic

The following technical diagnostics are disabled by default:

- MQTT message count
- MQTT last message at
- MQTT last topic

They can be enabled manually in the Home Assistant entity registry.

### MQTT-dependent entity availability

The following entities are created only when MQTT is enabled:

- Battery charged
- Battery discharged
- PV1 energy
- PV2 energy
- PV energy total
- Device connection
- MQTT diagnostics
- AC Output
- Reboot device

### Simplified configuration flows

The initial config flow asks for:

- Jackery Home account
- Jackery Home password
- systems to import
- whether MQTT should be enabled
- whether invalid or expired MQTT TLS certificates should be ignored
- whether raw MQTT debug logging should be enabled
- the MQTT live meter poll interval for fast-changing values (5-60 seconds, default 60; shorter intervals increase MQTT traffic load)

The required `phone_uid` is generated automatically.

The reconfigure flow keeps the `phone_uid` visible and editable for troubleshooting or compatibility cases.

---

## Screenshot

![Jackery Home Cloud device view](docs/img/jackery-home-cloud-device-view.png)

---

## Installation

### Option 1: HACS (Recommended)

1. Make sure you have [HACS](https://hacs.xyz/) installed.
2. Open HACS in Home Assistant.
3. Add this repository as a custom repository:
   - Repository: `https://github.com/iLLixM/jackery_home_cloud-ha`
   - Category: `Integration`
4. Search for **Jackery Home Cloud**.
5. Download the latest release.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration**.
8. Search for **Jackery Home Cloud**.

### Option 2: Manual installation

1. Download the latest release archive.
2. Copy the folder:

   ```text
   custom_components/jackery_home_cloud
   ```

   into:

   ```text
   <home-assistant-config>/custom_components/
   ```

3. Restart Home Assistant.
4. Go to **Settings → Devices & services**.
5. Add the **Jackery Home Cloud** integration.
6. Enter your Jackery account credentials.
7. Select the systems you want to import.

---

## Configuration

The integration uses:

- Jackery Home account email
- Jackery Home password
- an automatically generated stable `phone_uid`
- one or more selected system IDs
- optional MQTT settings

The integration performs a cloud login and then reads system, monitor, device, and trend data from the Jackery backend.

### Initial setup

During initial setup, the `phone_uid` is generated automatically and is not shown as a free-text field.

The MQTT settings are shown in this order:

1. Enable MQTT connection
2. Ignore invalid / expired MQTT TLS certificates
3. Enable MQTT raw debug logging

### Reconfigure

The reconfigure flow allows account-related settings to be updated.

The `phone_uid` remains visible and editable in this flow so that it can be changed for troubleshooting or compatibility purposes.

### Options

The options flow provides the same MQTT settings and ordering as the initial config flow.

Disabling MQTT does not intentionally delete the stored TLS or raw-debug preferences. These settings remain available if MQTT is enabled again later.

### Multiple systems

Accounts containing multiple Jackery systems remain supported for REST-based data.

In this initial MQTT implementation, MQTT telemetry and controls are enabled only for a single, automatically resolved primary system per config entry. Additional systems continue to use REST-based entities only; no MQTT-only entities or MQTT polling are created for them.

Explicit MQTT system selection and validated multi-system MQTT support are planned for a later release.

### MQTT TLS option

The option **Ignore invalid / expired MQTT TLS certificates** disables certificate and hostname verification for the Jackery MQTT connection.

> [!WARNING]
> Enabling this option reduces transport security and increases the risk of man-in-the-middle attacks.
> Use it only when required and only if you understand the implications.

### Raw MQTT debug logging

The option **Enable MQTT raw debug logging** adds verbose MQTT payload information to the Home Assistant log.

Raw payloads may contain:

- device serial numbers
- system identifiers
- topic names
- operational values
- account- or device-related metadata

Do not publish unredacted debug logs.

### Debug logging

To enable integration debug logging in `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.jackery_home_cloud: debug
```

Restart Home Assistant after changing the logger configuration.

### Energy Dashboard

The cumulative MQTT energy sensors use `SensorStateClass.TOTAL_INCREASING` and can be suitable for Home Assistant long-term statistics and Energy Dashboard use.

Before adding them to the Energy Dashboard:

- verify that values and units are plausible
- observe the entities for a reasonable period
- check for unexpected device-side counter resets
- remove incorrect statistics before relying on derived totals

---

## Project goals

This project is intended to become a stable and useful Home Assistant integration for Jackery Home Cloud systems.

Current and future goals include:

- stable cloud authentication
- robust system discovery
- proper Home Assistant device and entity modeling
- reliable daily energy history sensors
- dependable MQTT-based telemetry
- safe MQTT-backed controls
- improved diagnostics and error handling
- broader device and regional compatibility
- continued reverse engineering of unsupported API and MQTT areas
- maintainable HACS-compatible packaging

---

## Technical notes

- The integration is cloud-dependent.
- REST data is retrieved by cloud polling.
- MQTT data is received from the Jackery-hosted cloud broker.
- The integration does not use Home Assistant's MQTT integration.
- The current Home Assistant `iot_class` remains cloud-based because both communication paths depend on Jackery infrastructure.
- MQTT broker credentials are retrieved from the Jackery cloud API.
- MQTT subscriptions are device-specific.
- Cumulative MQTT values are protected against unexpected decreases.
- MQTT total sensors use state restoration.
- MQTT message processing is isolated defensively so that a failure in one ingest path does not block all other MQTT processing.
- The API and MQTT protocol are unofficial and may change without notice.
- Existing entity-registry entries may remain after MQTT is disabled.

### Technical architecture

```text
Jackery Home Cloud REST API
├── authentication
├── system discovery
├── system snapshots
└── daily trend data

Jackery Cloud MQTT
├── cumulative energy telemetry
├── LWT device status
├── AC output state and control
└── device reboot command
```

### Troubleshooting MQTT entities

If MQTT entities are unavailable, check:

- MQTT is enabled in the integration options
- the config entry was reloaded after changing options
- the Jackery cloud API returned MQTT credentials
- the MQTT connection succeeded
- the selected device publishes to the expected topics
- certificate validation is not blocking the broker connection

### Troubleshooting AC Output

The integration expects the device to return meter `23120897` in a `data_get` or `data_set` response.

Useful debug messages include:

```text
Accepted MQTT AC output state ...
```

If commands work physically but the UI does not update, collect a redacted debug log containing the command response.

### Troubleshooting cumulative totals

Rejected lower values should produce debug messages similar to:

```text
Ignoring decreasing MQTT ...
```

If decreases still appear in history, include the relevant raw MQTT payload and entity history in an issue report.

---

## Compatibility

The integration is primarily developed and tested against:

- Jackery HomePower 2000 Ultra
- European Jackery Home cloud infrastructure
- modern Home Assistant versions with config entries and entity descriptions

Because the API and MQTT behavior are reverse engineered, compatibility with all Jackery products, regions, firmware versions, app versions, and future backend variants cannot be guaranteed.

The integration requires:

- internet access
- a functioning Jackery Home account
- access to the Jackery cloud backend
- MQTT credentials returned by the backend when MQTT is enabled

Reports from additional regions and device families are welcome.

---

## Contributing

Contributions are very welcome.

If you are using this project and find problems, please:

- open an issue
- describe your Jackery hardware and region
- include the Home Assistant version
- include the integration version
- describe expected and observed behavior
- share relevant redacted logs and MQTT payloads where possible
- report entities or values that appear incorrect

If you want to improve the integration, feel free to open a pull request.

Especially helpful contributions include:

- API observations from additional regions or device families
- MQTT topic and payload observations
- validation of meter semantics
- testing on additional Jackery Home systems
- code quality, typing, and documentation improvements
- translations
- automated tests
- HACS packaging and release workflow improvements

Do not include credentials, tokens, MQTT passwords, or other secrets in public issues.

---

## Feedback wanted

Feedback is explicitly encouraged.

Please comment on:

- incorrect entity names or units
- missing sensors
- MQTT meter semantics
- unexpected counter resets
- incorrect daily trend interpretation
- device-model decisions
- regional backend differences
- API or MQTT changes observed in newer Jackery app or firmware versions
- behavior of AC Output and device reboot controls
- usability of config, reconfigure, and options flows

Security-sensitive findings should be reported privately before technical details are published.

---

## Development status

This is active community software.

Version `0.3.0` provides a functional combination of Jackery cloud polling, cloud MQTT telemetry, cumulative energy entities, diagnostics, and basic MQTT-backed controls.

The integration should still be treated as unofficial software that depends on undocumented interfaces.

Backend, firmware, certificate, topic, or payload changes can require updates at any time.

If you test it, review it, report issues, or improve it: thank you.

---

## Disclaimer

This repository is not affiliated with, maintained by, sponsored by, or endorsed by Jackery.

All product names, trademarks, and registered trademarks are the property of their respective owners.

Use this integration at your own risk.
