# Boiler Controller API Endpoints

This document describes the HTTP endpoints exposed by the firmware webserver.

- Base URL: `http://<device-ip>`
- API prefix: `/api`
- Content types:
  - JSON for status/system endpoints
  - Plain text for control endpoints
  - `multipart/form-data` for firmware upload

## 1) GET `/api/system`

Returns device and runtime information.

### Request

- Method: `GET`
- Query parameters: none

### Response

- Status: `200 OK`
- Content-Type: `application/json`
- Body example:

```json
{
  "system": {
    "firmwareVersion": 1,
    "cpuFrequency": "240 MHz",
    "ip": "192.168.1.123",
    "wifiStrength": -58,
    "uptimeSeconds": 98012,
    "upSince": "27h 13m 32s",
    "currentDateTime": "-"
  }
}
```

> **Note:** `currentDateTime` is not yet implemented and always returns `"-"`. `upSince` is the elapsed uptime formatted as `"Xh MMm SSs"`.

## 2) GET `/api/status`

Returns boiler status values used by the UI.

### Request

- Method: `GET`
- Query parameters: none

### Response

- Status: `200 OK`
- Content-Type: `application/json`
- Body example:

```json
{
  "power": 1320,
  "measuredPowerSource": "energy_meter",
  "heatingPercentage": 60,
  "temperature": 65.0,
  "total": 12.345,
  "rssi": -50
}
```

`measuredPowerSource` is one of `"energy_meter"`, `"pulse"`, or `"estimate"` (heuristic fallback).

## 3) GET `/api/heat`

Sets heating level percentage.

### Request

- Method: `GET`
- Query parameter:
  - `percentage` (optional): integer, clamped to `0..100`

Example:

`GET /api/heat?percentage=60`

### Response

- Status: `200 OK`
- Content-Type: `text/plain`
- Body: `OK`

## 4) GET `/api/reboot`

Triggers an immediate reboot.

### Request

- Method: `GET`
- Query parameters: none

### Response

- Status: `200 OK`
- Content-Type: `text/plain`
- Body: `Restart ESP`

## 5) GET `/api/factoryreset`

Triggers factory reset callback, responds `OK`, then reboots.

### Request

- Method: `GET`
- Query parameters: none

### Response

- Status: `200 OK`
- Content-Type: `text/plain`
- Body: `OK`

## 6) POST `/api/update`

Uploads new firmware image and, on success, reboots into the new partition.

### Request

- Method: `POST`
- Content-Type: `multipart/form-data`
- Payload: firmware `.bin` file (commonly sent in form field `update` by UI)

### Response

- Status: `200 OK`
- Content-Type: `text/plain`
- Body:
  - `OK` on successful OTA and boot partition switch (device then reboots)
  - `FAIL` on OTA failure

## 7) GET `/api/dashboard`

Single response combining `system`, `status`, `sensors`, `control`, `output`, and `calibration` (same field shapes as the individual endpoints below where applicable). Used by the web UI Status tab.

## 8) GET `/api/sensors/temperature`

JSON: `valid` (bool), `celsius` (number, when valid).

## 9) GET `/api/sensors/energy`

JSON: `valid` (bool), `everValid` (bool), `pollFailCount` (number), `pulseFallbackAvoidedCount` (number); when `valid`: `totalKwh`, `activeKw`, `voltageV`, `currentA`.

## 10) GET `/api/sensors/pulse`

JSON: `valid` (bool); when valid: `totalPulses`, `estimatedWatts`.

## 11) GET `/api/output`

JSON: `analog0to10V` (`requestedVoltage`, `dacMillivolt`), `ssr` (`heatingPercent`, `tonUs`, `toffUs`, `phaseHigh`, `gpioHigh`, `dutyCyclePercent`).

## 12) POST `/api/control`

Set heating setpoint. Body: `application/json` with either **`percentage`** or **`heatingPercent`**, or **`watts`** / **`heatingWatts`** (converted using the calibration curve when available, otherwise the 22 W/% heuristic).

Response `200` JSON: `heatingPercentage`, `wattageControlEnabled`, `estimatedHeatingWattsFromPercent`. `400` if neither watts nor percentage is provided.

## 13) GET `/api/calibration`

Returns the current calibration data and the state of an active calibration run.

### Response

- Status: `200 OK`
- Content-Type: `application/json`
- Body example:

```json
{
  "calibrated": true,
  "points": [
    { "percent": 0, "watts": 0, "fromMeasurement": false },
    { "percent": 1, "watts": 22, "fromMeasurement": true }
  ],
  "run": {
    "state": "idle",
    "step": 0,
    "currentPercent": 0,
    "lastSampleWatts": 0
  }
}
```

`run.state` is one of `"idle"`, `"running"`, or `"done"`. An optional `run.error` string is included when an error occurred.

## 14) POST `/api/calibration/run`

Starts an automated calibration run.

### Response

- `200 OK` — `{"ok": true}` on success
- `409 Conflict` — `{"ok": false, "error": "busy"}` if already running

## 15) POST `/api/calibration/stop`

Requests a stop of an active calibration run.

### Response

- `200 OK` — `{"ok": true}` on success
- `409 Conflict` — `{"ok": false, "error": "not_running"}` if not running

## 16) POST `/api/calibration/clear`

Clears all stored calibration data.

### Response

- `200 OK` — `{"ok": true}` on success
- `409 Conflict` — `{"ok": false, "error": "busy"}` if a calibration run is in progress

## 17) POST `/api/heat/watts`

Sets the target heating wattage. Rate-limited to one request per 4.5 seconds.

### Request

- Method: `POST`
- Content-Type: `application/json`
- Body: `{"watts": 1500}` (also accepts `"heatingWatts"`)

### Response

- `200 OK`:

```json
{
  "requestedWatts": 1500,
  "targetHeatingWatts": 1500,
  "wattageControlEnabled": true,
  "heatingPercentage": 68
}
```

- `400 Bad Request` — `{"error": "watts required"}` if the field is missing
- `429 Too Many Requests` — `{"error": "rate_limited", "retryAfterSeconds": 3}` with `Retry-After` header

## Non-API routes

- `GET /` serves the main web UI (gzipped HTML)
- `GET /logo.svg` serves the logo asset (gzipped SVG)

## Notes

- Endpoint list is based on currently registered handlers in firmware (`main/Webserver/Webserver.cpp`).
- A `/ota` route appears in the web UI source as documentation text but is not currently registered by the firmware webserver.
