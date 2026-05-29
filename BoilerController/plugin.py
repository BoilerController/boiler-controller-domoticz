#
# Boiler Controller — Domoticz Python Plugin
#
# Turns an electric boiler into a "water battery" by sending the surplus
# reported by your P1 smart meter to the BoilerController (BC) device over
# HTTP. Mirrors the Home Assistant integration in this repository.
#
# Authors: @reinos, @XiloXL
#
"""
<plugin key="BoilerController" name="Boiler Controller" author="reinos"
        version="1.0.0"
        wikilink="https://github.com/BoilerController/boiler-controller-ha"
        externallink="https://github.com/BoilerController/boiler-controller-ha">
    <description>
        <h2>Boiler Controller</h2>
        Send the surplus reported by your P1 smart meter to a Boiler Controller
        (BC) device and use your electric boiler as a &quot;water battery&quot;.
        <h3>Devices</h3>
        <ul style="list-style-type:square">
            <li><b>Control Mode</b> — Selector (Off / Auto / Manual / On)</li>
            <li><b>Manual Power</b> — Setpoint (Watt) used in Manual mode</li>
            <li><b>Calibrate Start / Stop</b> — Push buttons to start or stop a calibration run</li>
            <li><b>Status</b> — Text sensor with the current status (Running, Idle, Calibrating)</li>
            <li><b>Device Power</b> — Power + cumulative energy (kWh)</li>
            <li><b>Heating Percentage</b> — Output percentage reported by the BC</li>
            <li><b>Temperature</b> — External temperature (I/O)</li>
            <li><b>RSSI</b> — Wi-Fi signal strength</li>
            <li><b>Firmware / IP / Last Update</b> — Diagnostic text sensors</li>
        </ul>
        <h3>Configuration</h3>
        Enter the IP address of the BC and pick how to read your P1 meter:
        net (one signed value) or split (two values for export and import).
        The plugin reads the P1 values through the Domoticz JSON API; supply
        the IDX numbers of your existing P1 or energy devices.
    </description>
    <params>
        <param field="Address" label="BC Device IP / Host" width="200px" required="true" default="192.168.1.50"/>
        <param field="Port"    label="BC Device Port"      width="60px"  required="true" default="80"/>

        <param field="Mode1" type="number" label="Poll interval (s)" width="80px"
               min="5" max="600" step="5" default="10"/>

        <param field="Mode2" label="P1 sensor mode" width="120px">
            <options>
                <option label="Net (1 sensor, signed)" value="net" default="true"/>
                <option label="Split (2 sensors)"      value="split"/>
            </options>
        </param>

        <param field="Mode3" label="Domoticz JSON URL" width="240px"
               default="http://127.0.0.1:8080"/>

        <param field="Mode4" label="P1 Net IDX (or Return IDX in split mode)" width="120px" default=""/>
        <param field="Mode5" label="P1 Usage IDX (split mode only)" width="120px" default=""
               visible_when="Mode2=split"/>

        <param field="Mode6" label="Debug" width="150px">
            <options>
                <option label="None"             value="0" default="true"/>
                <option label="Python Only"      value="2"/>
                <option label="Basic Debugging"  value="62"/>
                <option label="Basic+Messages"   value="126"/>
                <option label="Connections Only" value="16"/>
                <option label="All"              value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import Domoticz


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Unit numbers for the Domoticz devices created by this plugin
UNIT_CONTROL_MODE = 1
UNIT_MANUAL_POWER = 2
UNIT_CALIBRATE_START = 3
UNIT_CALIBRATE_STOP = 4
UNIT_STATUS = 5
UNIT_DEVICE_POWER = 6
UNIT_HEATING_PCT = 7
UNIT_TEMPERATURE = 8
UNIT_RSSI = 9
UNIT_FIRMWARE = 10
UNIT_IP = 11
UNIT_LAST_UPDATE = 12

# Selector switch levels (multiples of 10 per Domoticz convention)
MODE_OFF = 0
MODE_AUTO = 10
MODE_MANUAL = 20
MODE_ON = 30
MODE_CALIBRATING = 40

LEVEL_TO_MODE = {
    MODE_OFF: "off",
    MODE_AUTO: "auto",
    MODE_MANUAL: "manual",
    MODE_ON: "on",
    MODE_CALIBRATING: "calibrating",
}
MODE_TO_LEVEL = {v: k for k, v in LEVEL_TO_MODE.items()}

MAX_EXPORT_WATTS = 3500
DEFAULT_POLL_INTERVAL = 10
HTTP_TIMEOUT = 10
CALIBRATION_POLL_SECONDS = 5

# BC HTTP API paths
API_STATUS = "/api/status"
API_SYSTEM = "/api/system"
API_CONTROL = "/api/control"
API_CALIBRATION = "/api/calibration"
API_CALIBRATION_RUN = "/api/calibration/run"
API_CALIBRATION_STOP = "/api/calibration/stop"


# ---------------------------------------------------------------------------
# Minimal HTTP helpers (synchronous; called from worker thread only)
# ---------------------------------------------------------------------------

def _http_get_json(url, timeout=HTTP_TIMEOUT):
    """GET *url* and return parsed JSON, or ``None`` on any error."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                Domoticz.Debug("GET %s -> HTTP %s" % (url, resp.status))
                return None
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as err:
        Domoticz.Debug("GET %s failed: %s" % (url, err))
        return None


def _http_post_json(url, payload, timeout=HTTP_TIMEOUT):
    """POST *payload* (dict) as JSON to *url*. Returns the parsed response (or
    an empty dict on 200 with empty body) or ``None`` on failure."""
    try:
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                Domoticz.Debug("POST %s -> HTTP %s" % (url, resp.status))
                return None
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                return {}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        Domoticz.Debug("POST %s failed: %s" % (url, err))
        return None


# ---------------------------------------------------------------------------
# BoilerController plugin
# ---------------------------------------------------------------------------

class BasePlugin:
    """Domoticz plugin orchestrating the Boiler Controller device."""

    def __init__(self):
        # Configuration
        self.base_url = ""
        self.poll_interval = DEFAULT_POLL_INTERVAL
        self.p1_mode = "net"        # "net" or "split"
        self.domoticz_url = "http://127.0.0.1:8080"
        self.p1_idx_a = None        # net IDX, or return IDX in split mode
        self.p1_idx_b = None        # usage IDX in split mode

        # State
        self._control_mode = "off"
        self._manual_watts = 0
        self._device_status = None  # last /api/status payload
        self._system_status = None  # last /api/system payload
        self._calibration_active = False
        self._last_control_update = None

        # Worker thread plumbing
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker = None
        self._command_lock = threading.Lock()
        self._calibration_thread = None

    # ------------------------------------------------------------------
    # Domoticz callbacks
    # ------------------------------------------------------------------

    def onStart(self):
        debug_level = 0
        try:
            debug_level = int(Parameters.get("Mode6", "0") or 0)
        except ValueError:
            debug_level = 0
        if debug_level != 0:
            Domoticz.Debugging(debug_level)
        Domoticz.Log("Boiler Controller plugin starting (debug=%s)" % debug_level)

        # Parse configuration
        address = (Parameters.get("Address") or "").strip()
        port = (Parameters.get("Port") or "80").strip()
        if not address:
            Domoticz.Error("BC device address is empty - configure the IP first")
            return
        if address.startswith("http://") or address.startswith("https://"):
            self.base_url = address.rstrip("/")
        else:
            self.base_url = "http://%s:%s" % (address, port)

        try:
            self.poll_interval = max(5, int(Parameters.get("Mode1") or DEFAULT_POLL_INTERVAL))
        except ValueError:
            self.poll_interval = DEFAULT_POLL_INTERVAL

        mode = (Parameters.get("Mode2") or "net").lower().strip()
        self.p1_mode = mode if mode in ("net", "split") else "net"

        self.domoticz_url = (Parameters.get("Mode3") or "http://127.0.0.1:8080").rstrip("/")
        self.p1_idx_a = _parse_int(Parameters.get("Mode4"))
        self.p1_idx_b = _parse_int(Parameters.get("Mode5"))

        Domoticz.Log(
            "Config: bc_url=%s poll=%ss p1_mode=%s domoticz=%s idx_a=%s idx_b=%s"
            % (
                self.base_url,
                self.poll_interval,
                self.p1_mode,
                self.domoticz_url,
                self.p1_idx_a,
                self.p1_idx_b,
            )
        )

        # Use a 5 second heartbeat; the worker thread does the real work.
        Domoticz.Heartbeat(min(self.poll_interval, 30))

        self._create_devices()
        self._restore_mode_from_device()

        # Start background worker
        self._stop_event.clear()
        self._wake_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="BC-Worker", daemon=True)
        self._worker.start()

        # Force an initial wake-up so devices get filled in quickly.
        self._wake_event.set()

    def onStop(self):
        Domoticz.Log("Boiler Controller plugin stopping")
        self._stop_event.set()
        self._wake_event.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=15)
            if worker.is_alive():
                Domoticz.Error("Worker thread did not stop in time")
        cal = self._calibration_thread
        if cal is not None and cal.is_alive():
            cal.join(timeout=10)
        self._worker = None
        self._calibration_thread = None

    def onHeartbeat(self):
        # The worker handles polling on its own schedule; the heartbeat just
        # nudges it in case the wait interval was long.
        self._wake_event.set()

    def onCommand(self, Unit, Command, Level, Color):  # noqa: N803 (Domoticz API)
        Domoticz.Debug(
            "onCommand Unit=%s Command=%s Level=%s" % (Unit, Command, Level)
        )

        if Unit == UNIT_CONTROL_MODE:
            self._handle_mode_command(Command, Level)
            return
        if Unit == UNIT_MANUAL_POWER:
            self._handle_manual_power_command(Command, Level)
            return
        if Unit == UNIT_CALIBRATE_START:
            self._handle_calibrate_start()
            return
        if Unit == UNIT_CALIBRATE_STOP:
            self._handle_calibrate_stop()
            return

        Domoticz.Debug("Ignoring command for Unit %s" % Unit)

    # ------------------------------------------------------------------
    # Device creation
    # ------------------------------------------------------------------

    def _create_devices(self):
        if UNIT_CONTROL_MODE not in Devices:
            Domoticz.Device(
                Name="Control Mode",
                Unit=UNIT_CONTROL_MODE,
                TypeName="Selector Switch",
                Switchtype=18,
                Image=9,
                Options={
                    "LevelActions": "|||||",
                    "LevelNames": "Off|Auto|Manual|On|Calibrating",
                    "LevelOffHidden": "false",
                    "SelectorStyle": "1",
                },
                Used=1,
            ).Create()

        if UNIT_MANUAL_POWER not in Devices:
            Domoticz.Device(
                Name="Manual Power",
                Unit=UNIT_MANUAL_POWER,
                Type=242,
                Subtype=1,
                Options={
                    "ValueStep": "50",
                    "ValueMin": "0",
                    "ValueMax": str(MAX_EXPORT_WATTS),
                    "ValueUnit": "W",
                },
                Used=1,
            ).Create()

        if UNIT_CALIBRATE_START not in Devices:
            Domoticz.Device(
                Name="Calibrate Start",
                Unit=UNIT_CALIBRATE_START,
                TypeName="Switch",
                Switchtype=9,  # Push On
                Used=1,
            ).Create()

        if UNIT_CALIBRATE_STOP not in Devices:
            Domoticz.Device(
                Name="Calibrate Stop",
                Unit=UNIT_CALIBRATE_STOP,
                TypeName="Switch",
                Switchtype=9,
                Used=1,
            ).Create()

        if UNIT_STATUS not in Devices:
            Domoticz.Device(
                Name="Status",
                Unit=UNIT_STATUS,
                TypeName="Alert",
                Used=1,
            ).Create()

        if UNIT_DEVICE_POWER not in Devices:
            Domoticz.Device(
                Name="Device Power",
                Unit=UNIT_DEVICE_POWER,
                Type=243,
                Subtype=29,
                Switchtype=0,
                Options={"EnergyMeterMode": "0"},
                Used=1,
            ).Create()

        if UNIT_HEATING_PCT not in Devices:
            Domoticz.Device(
                Name="Heating Percentage",
                Unit=UNIT_HEATING_PCT,
                Type=243,
                Subtype=6,
                Used=1,
            ).Create()

        if UNIT_TEMPERATURE not in Devices:
            Domoticz.Device(
                Name="Device Temperature",
                Unit=UNIT_TEMPERATURE,
                TypeName="Temperature",
                Used=1,
            ).Create()

        if UNIT_RSSI not in Devices:
            Domoticz.Device(
                Name="WiFi RSSI",
                Unit=UNIT_RSSI,
                Type=243,
                Subtype=31,
                Options={"Custom": "1;dBm"},
                Used=0,
            ).Create()

        if UNIT_FIRMWARE not in Devices:
            Domoticz.Device(
                Name="Firmware Version",
                Unit=UNIT_FIRMWARE,
                Type=243,
                Subtype=19,
                Used=0,
            ).Create()

        if UNIT_IP not in Devices:
            Domoticz.Device(
                Name="IP Address",
                Unit=UNIT_IP,
                Type=243,
                Subtype=19,
                Used=0,
            ).Create()

        if UNIT_LAST_UPDATE not in Devices:
            Domoticz.Device(
                Name="Last Control Update",
                Unit=UNIT_LAST_UPDATE,
                Type=243,
                Subtype=19,
                Used=0,
            ).Create()

    def _restore_mode_from_device(self):
        """Pick up the persisted control mode/manual watts on (re)start."""
        if UNIT_CONTROL_MODE in Devices:
            try:
                level = int(Devices[UNIT_CONTROL_MODE].sValue or "0")
            except (TypeError, ValueError):
                level = 0
            self._control_mode = LEVEL_TO_MODE.get(level, "off")
            if self._control_mode == "calibrating":
                # We can't actually resume a calibration run; fall back to off.
                self._control_mode = "off"
                self._update_mode_device("off")
        if UNIT_MANUAL_POWER in Devices:
            try:
                self._manual_watts = max(0, min(MAX_EXPORT_WATTS, int(float(
                    Devices[UNIT_MANUAL_POWER].sValue or "0"
                ))))
            except (TypeError, ValueError):
                self._manual_watts = 0

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_mode_command(self, command, level):
        if self._calibration_active:
            Domoticz.Log("Ignoring mode change while calibration is active")
            self._update_mode_device("calibrating")
            return

        new_mode = None
        if str(command).lower() == "set level":
            new_mode = LEVEL_TO_MODE.get(int(level))
        elif str(command).lower() == "off":
            new_mode = "off"
        elif str(command).lower() == "on":
            new_mode = "on"

        if new_mode is None or new_mode not in ("off", "auto", "manual", "on"):
            Domoticz.Log("Unsupported mode command/level: %s / %s" % (command, level))
            return

        Domoticz.Log("Switching control mode to '%s'" % new_mode)
        self._control_mode = new_mode
        self._update_mode_device(new_mode)
        # Apply immediately
        self._wake_event.set()

    def _handle_manual_power_command(self, command, level):
        try:
            watts = int(round(float(level)))
        except (TypeError, ValueError):
            Domoticz.Log("Invalid manual power value: %s" % level)
            return
        watts = max(0, min(MAX_EXPORT_WATTS, watts))
        Domoticz.Log("Manual power set to %sW" % watts)
        self._manual_watts = watts
        if UNIT_MANUAL_POWER in Devices:
            Devices[UNIT_MANUAL_POWER].Update(nValue=0, sValue=str(watts))
        if self._control_mode == "manual" and not self._calibration_active:
            self._wake_event.set()

    def _handle_calibrate_start(self):
        if self._calibration_active:
            Domoticz.Log("Calibration already running")
            return
        Domoticz.Log("Starting calibration run on BC device")
        self._calibration_thread = threading.Thread(
            target=self._calibration_loop,
            name="BC-Calibration",
            daemon=True,
        )
        self._calibration_thread.start()

    def _handle_calibrate_stop(self):
        if not self._calibration_active:
            Domoticz.Log("No calibration running")
            return
        Domoticz.Log("Requesting calibration stop")
        with self._command_lock:
            _http_post_json(self.base_url + API_CALIBRATION_STOP, {})

    # ------------------------------------------------------------------
    # Worker loop (background thread)
    # ------------------------------------------------------------------

    def _worker_loop(self):
        Domoticz.Debug("Worker thread started")
        while not self._stop_event.is_set():
            try:
                self._poll_once()
                if not self._calibration_active:
                    self._apply_control_mode()
            except Exception as err:  # noqa: BLE001
                Domoticz.Error("Worker iteration failed: %s" % err)

            # Wait poll_interval seconds, but wake early on command/heartbeat.
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()
        Domoticz.Debug("Worker thread exiting")

    def _poll_once(self):
        status = _http_get_json(self.base_url + API_STATUS)
        if status is not None:
            self._device_status = status
            self._publish_status_devices(status)

        system = _http_get_json(self.base_url + API_SYSTEM)
        if system is not None:
            self._system_status = system
            self._publish_system_devices(system)

        # Status indicator (Alert device)
        self._publish_status_indicator()

    def _apply_control_mode(self):
        mode = self._control_mode
        with self._command_lock:
            if mode == "off":
                self._send_percentage(0)
                return
            if mode == "on":
                self._send_percentage(100)
                return
            if mode == "manual":
                self._send_watts(self._manual_watts)
                return
            if mode == "auto":
                surplus = self._compute_surplus()
                if surplus is None:
                    Domoticz.Debug("Auto: no surplus data yet")
                    return
                boiler_watts = self._extract_boiler_consumption()
                available = max(0, min(MAX_EXPORT_WATTS, int(boiler_watts + surplus)))
                Domoticz.Debug(
                    "Auto: surplus=%.1f boiler=%.1f -> target=%dW"
                    % (surplus, boiler_watts, available)
                )
                self._send_watts(available)

    # ------------------------------------------------------------------
    # Calibration loop
    # ------------------------------------------------------------------

    def _calibration_loop(self):
        self._calibration_active = True
        previous_mode = self._control_mode
        try:
            self._control_mode = "calibrating"
            self._update_mode_device("calibrating")

            ok = _http_post_json(self.base_url + API_CALIBRATION_RUN, {})
            if ok is None:
                Domoticz.Error("Device rejected calibration start")
                return

            Domoticz.Log("Calibration started; polling for completion")
            seen_running = False
            while not self._stop_event.is_set():
                time.sleep(CALIBRATION_POLL_SECONDS)
                cal = _http_get_json(self.base_url + API_CALIBRATION)
                if cal is None:
                    Domoticz.Error("Lost contact with device during calibration")
                    break
                run = cal.get("run") or {}
                state = run.get("state", "idle")
                Domoticz.Debug(
                    "Calibration state=%s step=%s pct=%s watts=%s"
                    % (state, run.get("step"), run.get("currentPercent"),
                       run.get("lastSampleWatts"))
                )
                if state == "running":
                    seen_running = True
                if run.get("error"):
                    Domoticz.Error("Calibration error: %s" % run["error"])
                    break
                if state == "done" or (seen_running and state == "idle"):
                    Domoticz.Log("Calibration completed")
                    break
        except Exception as err:  # noqa: BLE001
            Domoticz.Error("Calibration loop failed: %s" % err)
        finally:
            self._calibration_active = False
            self._control_mode = previous_mode
            self._update_mode_device(previous_mode)
            self._wake_event.set()

    # ------------------------------------------------------------------
    # P1 surplus reading via Domoticz JSON API
    # ------------------------------------------------------------------

    def _compute_surplus(self):
        """Return signed grid surplus in Watts (positive = exporting)."""
        if self.p1_mode == "split":
            if not self.p1_idx_a or not self.p1_idx_b:
                Domoticz.Debug("Split mode but P1 IDX values missing")
                return None
            ret = self._read_domoticz_power(self.p1_idx_a)
            use = self._read_domoticz_power(self.p1_idx_b)
            if ret is None or use is None:
                return None
            return float(ret) - float(use)

        # net mode: single signed sensor (negative = export)
        if not self.p1_idx_a:
            Domoticz.Debug("Net mode but P1 IDX missing")
            return None
        net = self._read_domoticz_power(self.p1_idx_a)
        if net is None:
            return None
        return -float(net)

    def _read_domoticz_power(self, idx):
        """Query a Domoticz device by IDX and return its power in W or None."""
        url = (
            self.domoticz_url
            + "/json.htm?type=command&param=getdevices&rid="
            + urllib.parse.quote(str(idx))
        )
        data = _http_get_json(url)
        if not data or data.get("status") != "OK":
            return None
        result = data.get("result")
        if not result:
            return None
        row = result[0]

        # P1 Smart Meter devices have a "Usage" field like "1234 Watt".
        for field in ("Usage", "CounterToday", "Data"):
            value = row.get(field)
            if value is None:
                continue
            watts = _parse_watts(value)
            if watts is not None:
                return watts

        # Generic numeric Data field (Watt sensor)
        return _parse_watts(row.get("Data"))

    # ------------------------------------------------------------------
    # Sending commands to the BC device
    # ------------------------------------------------------------------

    def _send_percentage(self, pct):
        pct = max(0, min(100, int(pct)))
        Domoticz.Debug("BC set heating to %d%%" % pct)
        resp = _http_post_json(self.base_url + API_CONTROL, {"percentage": pct})
        if resp is not None:
            self._last_control_update = _now_iso()
            self._update_last_update_device()

    def _send_watts(self, watts):
        watts = max(0, min(MAX_EXPORT_WATTS, int(watts)))
        Domoticz.Debug("BC set heating to %dW" % watts)
        resp = _http_post_json(self.base_url + API_CONTROL, {"watts": watts})
        if resp is not None:
            self._last_control_update = _now_iso()
            self._update_last_update_device()

    def _extract_boiler_consumption(self):
        status = self._device_status or {}
        try:
            return float(status.get("power") or 0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Domoticz device updates
    # ------------------------------------------------------------------

    def _publish_status_devices(self, status):
        # /api/status -> power, heatingPercentage, temperature, total (kWh), rssi
        power = _parse_float(status.get("power"))
        pct = _parse_float(status.get("heatingPercentage"))
        temp = _parse_float(status.get("temperature"))
        total_kwh = _parse_float(status.get("total"))
        rssi = _parse_float(status.get("rssi"))

        if UNIT_DEVICE_POWER in Devices and power is not None:
            energy_wh = (total_kwh or 0.0) * 1000.0
            Devices[UNIT_DEVICE_POWER].Update(
                nValue=0,
                sValue="%.0f;%.0f" % (power, energy_wh),
            )

        if UNIT_HEATING_PCT in Devices and pct is not None:
            Devices[UNIT_HEATING_PCT].Update(nValue=0, sValue="%.0f" % pct)

        if UNIT_TEMPERATURE in Devices and temp is not None:
            Devices[UNIT_TEMPERATURE].Update(nValue=0, sValue="%.1f" % temp)

        if UNIT_RSSI in Devices and rssi is not None:
            Devices[UNIT_RSSI].Update(nValue=0, sValue="%.0f" % rssi)

    def _publish_system_devices(self, system):
        sysinfo = (system or {}).get("system") or {}
        fw = sysinfo.get("firmwareVersion")
        ip = sysinfo.get("ip")

        if UNIT_FIRMWARE in Devices and fw is not None:
            Devices[UNIT_FIRMWARE].Update(nValue=0, sValue=str(fw))
        if UNIT_IP in Devices and ip is not None:
            Devices[UNIT_IP].Update(nValue=0, sValue=str(ip))

    def _publish_status_indicator(self):
        if UNIT_STATUS not in Devices:
            return
        status = self._device_status or {}
        try:
            power = float(status.get("power") or 0)
        except (TypeError, ValueError):
            power = 0
        if self._calibration_active:
            text = "Calibrating"
            level = 3  # yellow
        elif power > 1:
            text = "Running (%dW)" % int(power)
            level = 1  # green
        else:
            text = "Idle"
            level = 2  # grey/blue
        Devices[UNIT_STATUS].Update(nValue=level, sValue=text)

    def _update_mode_device(self, mode):
        if UNIT_CONTROL_MODE not in Devices:
            return
        level = MODE_TO_LEVEL.get(mode, MODE_OFF)
        nvalue = 0 if mode == "off" else 1
        Devices[UNIT_CONTROL_MODE].Update(nValue=nvalue, sValue=str(level))

    def _update_last_update_device(self):
        if UNIT_LAST_UPDATE in Devices and self._last_control_update:
            Devices[UNIT_LAST_UPDATE].Update(
                nValue=0, sValue=self._last_control_update
            )


# ---------------------------------------------------------------------------
# Plumbing required by the Domoticz plugin framework
# ---------------------------------------------------------------------------

global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()


def onCommand(Unit, Command, Level, Color):  # noqa: N803
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Color)


# ---------------------------------------------------------------------------
# Small utility helpers
# ---------------------------------------------------------------------------

def _parse_int(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_watts(value):
    """Best-effort extraction of a wattage value from a Domoticz JSON field.

    Accepts numbers, plain numeric strings and strings like ``"1234 Watt"``.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Strip unit suffix if present
    token = text.split()[0]
    try:
        return float(token)
    except ValueError:
        return None


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
