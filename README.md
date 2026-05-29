# Boiler Controller — Domoticz plugin

Python plugin for [Domoticz](https://www.domoticz.com/) that turns your
electric boiler into a "water battery". Instead of exporting surplus solar
or dynamic-tariff energy to the grid, the plugin reads the surplus reported
by your P1 smart meter and sends it straight to the Boiler Controller (BC)
device so you store it as hot water.

## Features

- Reads live consumption/production from your P1 smart meter — either a
  single signed net device, or a pair of separate return + usage devices —
  through the Domoticz JSON API.
- Controls the Boiler Controller (BC) over HTTP using watt-based setpoints.
- Four control modes: `auto`, `manual`, `on`, `off`.
- Exposes device telemetry as Domoticz devices (power, energy, heating
  percentage, temperature, RSSI, firmware, IP).
- Built-in calibration support — start a manual run from a push button, or
  let the device calibrate itself automatically over time.

## Installation

Domoticz expects plugins to live under `<domoticz>/plugins/<Name>/plugin.py`.
Create that directory and copy (or clone) this file into it:

```bash
mkdir -p <domoticz>/plugins/BoilerController
cp plugin.py <domoticz>/plugins/BoilerController/plugin.py
```

Or via git:

```bash
cd <domoticz>/plugins
git clone https://github.com/BoilerController/boiler-controller-domoticz.git BoilerController
```

(on a typical Raspberry Pi install `<domoticz>` is `/home/pi/domoticz`.)

1. Make sure Domoticz is started with Python plugin support enabled (see the
   [Domoticz wiki](https://wiki.domoticz.com/Using_Python_plugins)).
2. Restart Domoticz.
3. Go to **Setup → Hardware** and add a new hardware of type
   **Boiler Controller**.

## Configuration

| Field | Description |
| --- | --- |
| **BC Device IP / Host** | IP address or hostname of your Boiler Controller (e.g. `192.168.1.50`). May also be a full `http://...` URL. |
| **BC Device Port** | HTTP port of the BC device (default `80`). |
| **Poll interval (s)** | How often the plugin polls the BC and (re)applies the control mode (default `10`). |
| **P1 sensor mode** | `net` — single signed P1 device (negative = exporting). `split` — two separate devices (export + import). |
| **Domoticz JSON URL** | Local Domoticz API endpoint (default `http://127.0.0.1:8080`). Used to read the P1 values. |
| **P1 Net IDX / Return IDX** | IDX of the Domoticz device providing the net P1 value (net mode) or the export value (split mode). |
| **P1 Usage IDX** | Only in split mode: IDX of the import (usage) device. |
| **Debug** | Domoticz debug level (use `Basic Debugging` when troubleshooting). |

> If your Domoticz instance uses basic auth, supply credentials in the URL:
> `http://user:pass@127.0.0.1:8080`.

### About the *Domoticz JSON URL* field

This is the base URL of the **JSON/HTTP API of the same Domoticz instance**
the plugin is running in. The plugin does not read your P1 meter directly —
that device is already configured in Domoticz. Instead it asks Domoticz for
the current value by calling:

```
GET <Domoticz JSON URL>/json.htm?type=command&param=getdevices&rid=<IDX>
```

…and parses the `Usage` / `Data` field from the response. In split mode it
does this for both the Return IDX and the Usage IDX.

When to change it from the default `http://127.0.0.1:8080`:

- Domoticz listens on a non-standard **port** (e.g. `http://127.0.0.1:8084`).
- Domoticz uses **HTTPS** (e.g. `https://127.0.0.1:443`).
- Domoticz requires **basic auth** — embed the credentials:
  `http://user:pass@127.0.0.1:8080`.
- The plugin and Domoticz run on **different hosts** — use the actual host
  or IP reachable from the plugin process.

If you restricted the API to specific networks, make sure the address you
use here is in the **"Local Networks (no username/password)"** list under
*Setup → Settings → Security*, otherwise the API call will be rejected.

## Power-sensor modes

The plugin computes a signed surplus (positive = exporting) from your
configured P1 device(s):

- **Net mode**: `surplus = -net_value`
- **Split mode**: `surplus = return - usage`

In `auto` mode the boiler setpoint is
`max(0, min(3500, boiler_watts + surplus))`. Because the surplus goes
negative as soon as you start importing, the controller automatically backs
off instead of getting stuck at 100%.

## Control modes

| Mode | Behaviour |
| --- | --- |
| `auto` | Continuously reads the P1 surplus and sends the matching wattage to the boiler. |
| `manual` | Sends a fixed wattage taken from the **Manual Power** device. |
| `on` | Sets the boiler to 100%. |
| `off` | Sets the boiler to 0% (default on startup). |

Switch modes from the **Control Mode** selector on the device page or via a
Domoticz event/script.

## Devices created

| # | Name | Type | Description |
| - | --- | --- | --- |
| 1 | Control Mode | Selector Switch | Off / Auto / Manual / On / Calibrating |
| 2 | Manual Power | Setpoint (W) | Target wattage used in `manual` mode |
| 3 | Calibrate Start | Push button | Starts a calibration run |
| 4 | Calibrate Stop | Push button | Stops a running calibration |
| 5 | Status | Alert | `Running`, `Idle` or `Calibrating` |
| 6 | Device Power | kWh (power + energy) | Live power draw + cumulative energy |
| 7 | Heating Percentage | Custom (%) | Output percentage reported by the BC |
| 8 | Device Temperature | Temperature | External temperature sensor (I/O) |
| 9 | WiFi RSSI | Custom (dBm) | Wi-Fi signal strength |
| 10 | Firmware Version | Text | BC firmware version |
| 11 | IP Address | Text | IP address of the BC |
| 12 | Last Control Update | Text | Timestamp of the last command sent to the BC |

## Calibration

Run the calibration sweep once so the BC knows how many watts correspond to
each output percentage. The result is stored on the device itself.

1. Press the **Calibrate Start** push button on the BC hardware page.
2. The Status device switches to `Calibrating` and the Control Mode selector
   moves to `Calibrating` while the sweep runs.
3. When the sweep finishes the plugin restores the previous control mode
   automatically.

Press **Calibrate Stop** to abort a running calibration.

> **Before you start a manual calibration:**
> - The boiler must be **cooled down**. If it is already at temperature the
>   heating element cannot reach the higher setpoints and the curve will be
>   incomplete.
> - The sweep takes **at least 6 minutes** while it measures every
>   percentage point against the actual wattage.
> - Running it manually is **optional** — the controller will calibrate
>   itself automatically over time. Use the button only when you want an
>   immediate, complete curve.

## How it works

The plugin spawns its own background thread that, every `poll_interval`
seconds:

1. Fetches `GET /api/status` and `GET /api/system` and updates the Domoticz
   devices.
2. Applies the current **Control Mode**:
   - **off** → `POST /api/control {"percentage": 0}`
   - **on**  → `POST /api/control {"percentage": 100}`
   - **manual** → `POST /api/control {"watts": <manual_power>}`
   - **auto** → reads the P1 surplus through the Domoticz JSON API and sends
     `max(0, min(3500, boiler_watts + surplus))` to the BC.

Calibration runs in a separate thread that calls
`POST /api/calibration/run` and polls `GET /api/calibration` until the run
finishes. During calibration the control mode is temporarily locked; when
the run ends the previous mode is restored.

## Tips

- Pin **Manual Power** to your Domoticz dashboard to quickly dial in a fixed
  wattage.
- Use Domoticz events or scripts to flip the **Control Mode** selector, for
  example `on` at night on a dynamic tariff and `auto` during the day.

## Example automations (dzVents)

Create these scripts under *Setup → More options → Events* (script type
**dzVents**). Replace the device names with the ones you used when adding
the hardware — by default they are prefixed with the hardware name, e.g.
`Boiler Controller - Control Mode`.

The **Control Mode** selector accepts these level labels: `Off`, `Auto`,
`Manual`, `On`, `Calibrating` (don't set `Calibrating` yourself — that
state is managed by the plugin).

**Force the boiler on during off-peak hours:**

```lua
return {
    on = {
        timer = { 'at 23:00' },
    },
    execute = function(domoticz)
        domoticz.devices('Boiler Controller - Control Mode').switchSelector('On')
    end,
}
```

**Return to auto mode in the morning:**

```lua
return {
    on = {
        timer = { 'at 07:00' },
    },
    execute = function(domoticz)
        domoticz.devices('Boiler Controller - Control Mode').switchSelector('Auto')
    end,
}
```

**Set a fixed wattage manually:**

```lua
return {
    on = {
        timer = { 'at 12:00' },
    },
    execute = function(domoticz)
        domoticz.devices('Boiler Controller - Manual Power').updateSetPoint(1000)
        domoticz.devices('Boiler Controller - Control Mode').switchSelector('Manual')
    end,
}
```

**React to a price-signal device (example):**

```lua
return {
    on = {
        devices = { 'Dynamic Tariff Price' },
    },
    execute = function(domoticz, priceDevice)
        local mode = domoticz.devices('Boiler Controller - Control Mode')
        if priceDevice.value < 0.05 then
            mode.switchSelector('On')
        else
            mode.switchSelector('Auto')
        end
    end,
}
```

> Tip: instead of names you can also reference devices by IDX:
> `domoticz.devices(123).switchSelector('Auto')`. Use whichever is more
> stable in your setup.
