"""bambu-mcp core — MQTT + FTPS drivers for Bambu Lab printers.

Every protocol decision here was proven live on real hardware (P1S fw 01.07.00.00
in LAN-only mode; A1 fw 01.08.01.00 in cloud mode) on 2026-08-20:
  - MQTT v3.1.1, user `bblp` / <access code>, TLS with verification disabled
    (printer certs chain to Bambu's self-signed BBL CA).
  - ONE long-lived connection per printer. Connection churn destabilises the
    P1-series ESP32 broker and knocks other clients (OE companion) offline.
  - Print start = `project_file` with url `ftp:///<file>` (three slashes, SD root).
    P1S requires LAN-only mode for this; A1 accepts it in cloud mode.
  - `ams_mapping` selects the physical spool per file filament — the feature the
    BTT Panda Touch lacks.
  - FTPS = implicit TLS on :990, passive mode.
"""

import io
import json
import os
import re
import socket
import ssl
import threading
import time
import zipfile
from ftplib import FTP_TLS


# ---------------------------------------------------------------- FTPS (implicit)

class ImplicitFTPTLS(FTP_TLS):
    """ftplib speaks explicit FTPS; Bambu printers use implicit TLS on 990."""

    def connect(self, host="", port=990, timeout=30):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


# ---------------------------------------------------------------- helpers

def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def parse_3mf_meta(data: bytes) -> dict:
    """Extract print metadata from a .gcode.3mf's Metadata/slice_info.config."""
    z = zipfile.ZipFile(io.BytesIO(data))
    si = [n for n in z.namelist() if n.endswith("slice_info.config")]
    if not si:
        return {"error": "no slice_info.config"}
    t = z.read(si[0]).decode("utf8", "ignore")
    out = {}
    m = re.search(r'printer_model_id"\s+value="([^"]+)"', t)
    out["printer_model_id"] = m.group(1) if m else None
    m = re.search(r'prediction"\s+value="(\d+)"', t)
    if m:
        s = int(m.group(1))
        out["print_seconds"] = s
        out["print_time"] = f"{s // 3600}h{(s % 3600) // 60:02d}m"
    m = re.search(r'weight"\s+value="([\d.]+)"', t)
    out["weight_g"] = float(m.group(1)) if m else None
    out["filaments"] = [
        {"id": int(a), "type": b, "color": "#" + c.lstrip("#").upper()[:6]}
        for a, b, c in re.findall(
            r'<filament\s+id="(\d+)"[^>]*type="([^"]*)"[^>]*color="#?([0-9A-Fa-f]{6})', t
        )
    ]
    plates = [n for n in z.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n)]
    out["plates"] = plates
    return out


# ---------------------------------------------------------------- printer driver

class BambuPrinter:
    def __init__(self, name, ip, serial, access_code, model=""):
        self.name, self.ip, self.serial = name, ip, serial
        self.access_code, self.model = access_code, model
        self.state: dict = {}
        self.last_msg = 0.0
        self._lock = threading.Lock()
        self._client = None
        self._connected = False

    # ---- MQTT ----------------------------------------------------------

    def connect(self):
        import paho.mqtt.client as mqtt

        cid = f"bambu-mcp-{self.name}"
        try:  # paho 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                            client_id=cid, protocol=mqtt.MQTTv311)
        except AttributeError:  # paho 1.x
            c = mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)
        c.username_pw_set("bblp", self.access_code)
        c.tls_set(cert_reqs=ssl.CERT_NONE)   # BBL CA is self-signed; LAN + access code is the auth
        c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        # Gentle reconnect: the P1 ESP32 broker hates churn.
        c.reconnect_delay_set(min_delay=5, max_delay=120)
        c.connect_async(self.ip, 8883, keepalive=60)
        c.loop_start()
        self._client = c

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = (rc == 0)
        if rc == 0:
            client.subscribe(f"device/{self.serial}/report")
            self.request({"pushing": {"sequence_id": "0", "command": "pushall",
                                      "version": 1, "push_target": 1}})

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except Exception:
            return
        with self._lock:
            _deep_merge(self.state, payload)
            self.last_msg = time.time()

    def request(self, payload: dict):
        if not self._client:
            raise RuntimeError(f"{self.name}: not connected")
        self._client.publish(f"device/{self.serial}/request", json.dumps(payload))

    def refresh(self, wait=3.0):
        """Ask for a full state push and give it a moment to arrive."""
        self.request({"pushing": {"sequence_id": str(int(time.time())),
                                  "command": "pushall", "version": 1,
                                  "push_target": 1}})
        time.sleep(wait)

    # ---- state readers --------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            p = dict(self.state.get("print", {}))
        return {
            "printer": self.name,
            "connected": self._connected,
            "state": p.get("gcode_state", "UNKNOWN"),
            "job": p.get("subtask_name", ""),
            "percent": p.get("mc_percent"),
            "remaining_min": p.get("mc_remaining_time"),
            "layer": f'{p.get("layer_num", "?")}/{p.get("total_layer_num", "?")}',
            "nozzle_c": p.get("nozzle_temper"),
            "nozzle_target_c": p.get("nozzle_target_temper"),
            "bed_c": p.get("bed_temper"),
            "bed_target_c": p.get("bed_target_temper"),
            "print_error": p.get("print_error", 0),
            "sdcard": p.get("sdcard"),
            "age_seconds": round(time.time() - self.last_msg) if self.last_msg else None,
        }

    def ams_trays(self) -> list:
        with self._lock:
            ams = self.state.get("print", {}).get("ams", {})
        out = []
        for unit in ams.get("ams", []):
            for t in unit.get("tray", []):
                tid = int(t.get("id", -1))
                if t.get("tray_type"):
                    out.append({"tray": tid, "slot_1based": tid + 1,
                                "type": t["tray_type"],
                                "color": "#" + t.get("tray_color", "")[:6].upper()})
                else:
                    out.append({"tray": tid, "slot_1based": tid + 1,
                                "type": None, "color": None})
        return out

    # ---- actions ---------------------------------------------------------

    def start_print(self, filename: str, ams_mapping: list, plate="Metadata/plate_1.gcode"):
        self.request({"print": {
            "sequence_id": str(int(time.time())),
            "command": "project_file",
            "param": plate,
            "project_id": "0", "profile_id": "0",
            "task_id": "0", "subtask_id": "0",
            "subtask_name": filename.replace(".gcode.3mf", ""),
            "url": f"ftp:///{filename}",          # three slashes = SD-card root (proven dialect)
            "use_ams": True,
            "ams_mapping": ams_mapping,
            "timelapse": False, "bed_leveling": True,
            "flow_cali": False, "vibration_cali": False,
            "layer_inspect": False,
        }})

    def _simple(self, command):
        self.request({"print": {"sequence_id": str(int(time.time())),
                                "command": command, "param": ""}})

    def stop(self):   self._simple("stop")
    def pause(self):  self._simple("pause")
    def resume(self): self._simple("resume")

    def light(self, on: bool):
        self.request({"system": {"sequence_id": str(int(time.time())),
                                 "command": "ledctrl", "led_node": "chamber_light",
                                 "led_mode": "on" if on else "off",
                                 "led_on_time": 500, "led_off_time": 500,
                                 "loop_times": 0, "interval_time": 0}})

    def set_tray(self, tray_id: int, tray_type: str, color_hex: str,
                 temp_min=220, temp_max=270, info_idx="GFG99"):
        self.request({"print": {"sequence_id": str(int(time.time())),
                                "command": "ams_filament_setting", "ams_id": 0,
                                "tray_id": tray_id, "tray_info_idx": info_idx,
                                "tray_color": color_hex.lstrip("#").upper()[:6] + "FF",
                                "nozzle_temp_min": temp_min,
                                "nozzle_temp_max": temp_max,
                                "tray_type": tray_type}})

    # ---- FTPS ------------------------------------------------------------

    def _ftps(self) -> ImplicitFTPTLS:
        f = ImplicitFTPTLS()
        f.connect(self.ip, 990, timeout=30)
        f.login("bblp", self.access_code)
        f.prot_p()
        f.set_pasv(True)
        return f

    def sd_list(self) -> list:
        f = self._ftps()
        rows = []
        f.retrlines("LIST", rows.append)
        f.quit()
        out = []
        for r in rows:
            m = re.match(r"[-rwx]{10}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$", r)
            if m and m.group(3).lower().endswith(".3mf"):
                out.append({"name": m.group(3), "size": int(m.group(1)),
                            "modified": m.group(2)})
        return out

    def sd_download(self, filename: str) -> bytes:
        f = self._ftps()
        buf = io.BytesIO()
        f.retrbinary(f"RETR {filename}", buf.write)
        f.quit()
        return buf.getvalue()

    def sd_upload(self, filename: str, data: bytes):
        f = self._ftps()
        f.storbinary(f"STOR {filename}", io.BytesIO(data))
        f.quit()

    def sd_delete(self, filename: str):
        f = self._ftps()
        f.delete(filename)
        f.quit()


# ---------------------------------------------------------------- OctoPrint driver
# For printers driven by OctoPrint (e.g. a Prusa MK4 on USB). Same read surface
# as BambuPrinter where it makes sense; no AMS, start goes through the job API.

import urllib.request


class OctoPrintPrinter:
    kind = "octoprint"

    def __init__(self, name, url, api_key, model=""):
        self.name, self.url, self.api_key, self.model = name, url.rstrip("/"), api_key, model
        self._last = {}
        self._last_ts = 0.0
        self._ok = False

    def _get(self, path, timeout=4):
        req = urllib.request.Request(self.url + path,
                                     headers={"X-Api-Key": self.api_key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _post(self, path, payload, timeout=8):
        req = urllib.request.Request(self.url + path, method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={"X-Api-Key": self.api_key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status

    def connect(self):  # parity with BambuPrinter; OctoPrint is polled, not streamed
        pass

    def refresh(self, wait=0):
        pass

    def snapshot(self) -> dict:
        now = time.time()
        if now - self._last_ts > 3:
            try:
                p = self._get("/api/printer?exclude=sd")
                j = self._get("/api/job")
                self._last = (p, j)
                self._ok = True
            except Exception:
                self._ok = False
            self._last_ts = now
        if not self._ok or not self._last:
            return {"printer": self.name, "connected": False, "state": "UNKNOWN",
                    "job": "", "percent": None, "remaining_min": None, "layer": "",
                    "nozzle_c": None, "nozzle_target_c": None, "bed_c": None,
                    "bed_target_c": None, "print_error": 0, "sdcard": None,
                    "age_seconds": None}
        p, j = self._last
        flags = p.get("state", {}).get("flags", {})
        if flags.get("printing"):
            state = "RUNNING"
        elif flags.get("paused") or flags.get("pausing"):
            state = "PAUSE"
        elif flags.get("error") or flags.get("closedOrError"):
            state = "UNKNOWN"
        else:
            state = "IDLE"
        temps = p.get("temperature", {})
        left = j.get("progress", {}).get("printTimeLeft")
        return {
            "printer": self.name, "connected": True, "state": state,
            "job": (j.get("job", {}).get("file", {}).get("name") or "").replace(".gcode", ""),
            "percent": round(j.get("progress", {}).get("completion") or 0),
            "remaining_min": round(left / 60) if left else None,
            "layer": "",
            "nozzle_c": temps.get("tool0", {}).get("actual"),
            "nozzle_target_c": temps.get("tool0", {}).get("target"),
            "bed_c": temps.get("bed", {}).get("actual"),
            "bed_target_c": temps.get("bed", {}).get("target"),
            "print_error": 1 if flags.get("error") else 0,
            "sdcard": None,
            "age_seconds": round(time.time() - self._last_ts),
        }

    def ams_trays(self) -> list:
        return []

    def files(self) -> list:
        out = []
        data = self._get("/api/files?recursive=true", timeout=10)

        def walk(items):
            for it in items:
                if it.get("type") == "folder":
                    walk(it.get("children", []))
                elif it.get("name", "").lower().endswith((".gcode", ".bgcode")):
                    ana = it.get("gcodeAnalysis", {})
                    secs = ana.get("estimatedPrintTime")
                    fil = ana.get("filament", {}).get("tool0", {})
                    out.append({
                        "name": it.get("path", it["name"]),
                        "size": it.get("size", 0),
                        "modified": it.get("date"),
                        "print_seconds": secs,
                        "print_time": f"{int(secs)//3600}h{(int(secs)%3600)//60:02d}m" if secs else None,
                        "filament_mm": fil.get("length"),
                        "origin": it.get("origin", "local"),
                    })
        walk(data.get("files", []))
        return out

    def start_print(self, filename, ams_mapping=None, origin="local"):
        self._post(f"/api/files/{origin}/{filename}", {"command": "select", "print": True})

    def stop(self):   self._post("/api/job", {"command": "cancel"})
    def pause(self):  self._post("/api/job", {"command": "pause", "action": "pause"})
    def resume(self): self._post("/api/job", {"command": "pause", "action": "resume"})
