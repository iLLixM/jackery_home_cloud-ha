# Jackery Home Cloud API Test Script

This directory contains a small **read-only** Python CLI script that can be used to test access to the unofficial **Jackery Home Cloud** REST API.

> [!WARNING]
> This script is based on reverse engineered API behavior observed from the Jackery Home Android app.
> The API is unofficial and may change without notice.

## Purpose

The script is intended to help other users quickly verify that their Jackery Home Cloud account can be accessed successfully.

It supports these core tasks:

- validate login with account and password
- fetch and display app user information
- fetch and display the complete system list
- fetch and display the device list for one or all systems
- fetch and display MQTT connection information returned by the API

In addition, the script supports a few useful **read-only diagnostic calls**:

- login status
- system detail
- monitor snapshot
- DIY/EPC grouped device view
- generic device detail
- Smart Meter / CT detail

The script does **not** include:

- trend/history endpoints
- TSL/protocol downloads
- MQTT topic tests
- write/control endpoints
- rename/update actions

## Requirements

Before using the script, make sure the following prerequisites are met:

- Python 3.10 or newer is recommended
- the Python package `requests` is installed
- you have a valid Jackery Home account
- the system can reach the Jackery Home Cloud over HTTPS

Install the only external dependency with:

```bash
python -m pip install requests
```

## Native Windows / PowerShell

The PowerShell port `jackery-api-test.ps1` provides the same read-only REST
diagnostics without Python or external PowerShell modules. It supports Windows
PowerShell 5.1 (`powershell.exe`) as well as PowerShell 7 (`pwsh`).

For a login-only test, let the script prompt for the password securely:

```powershell
.\jackery-api-test.ps1 `
  -account 'you@example.com' `
  -login-only
```

For non-interactive use, the password can be supplied through the environment:

```powershell
$env:JACKERY_PASSWORD = 'your-password'
try {
  .\jackery-api-test.ps1 `
    -account 'you@example.com' `
    -list-devices `
    -all-systems `
    -save '.\result.json'
}
finally {
  Remove-Item Env:JACKERY_PASSWORD
}
```

The public parameter names follow the same lowercase kebab-case convention as
the Python script. Python uses two leading hyphens, while PowerShell uses its
native single-hyphen syntax:

```text
Python:     --plain-password, --list-systems, --system-id
PowerShell: -plain-password,  -list-systems,  -system-id
```

## Basic Usage

### 1. Login test only

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --login-only
```

### 2. Run the default smoke test

If no follow-up actions are selected explicitly, the script automatically runs a practical default set:

- app user
- systems
- devices
- MQTT credentials

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password"
```

### 3. Show systems only

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --list-systems
```

### 4. Show devices for all systems

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --list-devices \
  --all-systems
```

### 5. Show MQTT credentials

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --show-mqtt
```

## Optional Read-Only Diagnostics

### App user

```bash
python jackery-api-test.py ... --show-app-user
```

### Login status

```bash
python jackery-api-test.py ... --show-login-status
```

### System detail

```bash
python jackery-api-test.py ... --system-detail --system-id 2000000000000000001
```

### Monitor snapshot

```bash
python jackery-api-test.py ... --show-monitor --system-id 2000000000000000001
```

### DIY/EPC grouped devices

```bash
python jackery-api-test.py ... --show-diy-devices --system-id 2000000000000000001
```

### Generic device detail

```bash
python jackery-api-test.py ... --device-detail --device-no ems_MAINDEVICE123456
```

### CT / Smart Meter detail

```bash
python jackery-api-test.py ... --ct-detail --device-no meter_SAMPLE1234
```

## Password Handling

For plain login, use one of:

- `--plain-password`
- `--password`
- environment variable `JACKERY_PASSWORD`

For encrypted login, use one of:

- `--encrypted-password`
- `--password`

and additionally set:

```bash
--encrypted true
```

## phoneUid Handling

The `phoneUid` parameter is optional.

If you do not provide it explicitly, the script uses this default value:

```text
sample-id-123
```

You can still override it if needed:

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --phone-uid "custom-id-456" \
  --list-systems
```

## Output Options

### Pretty output (default)

```bash
--output pretty
```

### Compact JSON output

```bash
--output json
```

### Save combined output to a file

```bash
--save result.json
```

Example:

```bash
python jackery-api-test.py \
  --account you@example.com \
  --plain-password "your-password" \
  --list-systems \
  --show-mqtt \
  --save result.json
```

## TLS Verification

TLS certificate verification is enabled by default.

Disable it only for controlled troubleshooting:

```bash
--insecure
```

## Header / App Version Notes

The script currently aligns its headers with observed **Jackery Home Android app 2.10.22** traffic, including:

- `user-agent: Dart/3.11 (dart:io)`
- `x-app-version: home_android_v2.10.22`
