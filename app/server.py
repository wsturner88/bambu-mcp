"""bambu-mcp — MCP server exposing a Bambu Lab printer fleet to any MCP agent.

Design rules (non-negotiable):
  * COLOR IS ALWAYS TEXT. Colorblind-safe by design — every tool answers with
    color NAMES ("GREEN", "ORANGE"), never bare hex or hue-only signals.
  * start_print is gated: the caller must pass the exact confirm string that
    suggest_mapping / preflight returned. An agent cannot start a job it has
    not accurately described to the human first.
  * One long-lived MQTT session per printer (P1 ESP32 broker hates churn).
"""

import json
import math
import os
import re
import time

import yaml
from mcp.server.fastmcp import FastMCP

from bambu import BambuPrinter, OctoPrintPrinter, parse_3mf_meta

CFG_PATH = os.environ.get("BAMBU_MCP_CONFIG", os.path.join(os.path.dirname(__file__), "config.yml"))
CACHE_PATH = os.environ.get("BAMBU_MCP_CACHE", "/data/metacache.json")

with open(CFG_PATH) as fh:
    CFG = yaml.safe_load(fh)

PALETTE = {k.upper(): v.upper() for k, v in CFG["palette"].items()}
RULES = CFG.get("conventions", [])

PRINTERS: dict = {}
for p in CFG["printers"]:
    if p.get("type") == "octoprint":
        key = os.environ.get(p["api_key_env"], "")
        prn = OctoPrintPrinter(p["name"], p["url"], key, p.get("model", ""))
    else:
        code = os.environ.get(p["access_code_env"], "")
        prn = BambuPrinter(p["name"], p["ip"], p["serial"], code, p.get("model", ""))
    PRINTERS[p["name"].lower()] = prn
    prn.connect()

def _is_octo(prn):
    return getattr(prn, "kind", "") == "octoprint"

# ------------------------------------------------------------- color helpers

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def color_name(hex_color: str) -> str:
    """Nearest palette name — so every color leaves this server as a WORD."""
    if not hex_color:
        return "UNKNOWN"
    r, g, b = _hex_to_rgb(hex_color)
    best, dist = "UNKNOWN", 1e9
    for name, h in PALETTE.items():
        pr, pg, pb = _hex_to_rgb(h)
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < dist:
            best, dist = name, d
    return best

def convention_for(filename: str):
    for rule in RULES:
        if re.search(rule["match"], filename, re.I):
            return rule
    return None

# ------------------------------------------------------------- metadata cache

def _cache_load() -> dict:
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _cache_save(c: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as fh:
        json.dump(c, fh)

def _meta_for(prn: BambuPrinter, name: str, size: int, download_if_missing: bool):
    cache = _cache_load()
    key = f"{prn.name}|{name}|{size}"
    if key in cache:
        return cache[key]
    if not download_if_missing:
        return None
    meta = parse_3mf_meta(prn.sd_download(name))
    cache[key] = meta
    _cache_save(cache)
    return meta

def _pick(name: str):
    prn = PRINTERS.get(name.lower())
    if not prn:
        raise ValueError(f"Unknown printer '{name}'. Known: {', '.join(PRINTERS)}")
    return prn

def _describe_meta(meta: dict) -> dict:
    if not meta:
        return {"metadata": "not cached — call with refresh_metadata=true (slow: full download)"}
    fil = [{"type": f["type"], "color_name": color_name(f["color"]), "hex": f["color"]}
           for f in meta.get("filaments", [])]
    return {"print_time": meta.get("print_time"), "weight_g": meta.get("weight_g"),
            "sliced_for": meta.get("printer_model_id"), "filaments": fil}

# ------------------------------------------------------------- MCP tools

mcp = FastMCP("bambu-fleet",
              host=os.environ.get("BAMBU_MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("BAMBU_MCP_PORT", "8271")))


@mcp.tool()
def list_printers() -> str:
    """List all printers with live state (idle/printing, job, progress)."""
    out = []
    for prn in PRINTERS.values():
        s = prn.snapshot()
        line = f'{s["printer"]}: {s["state"]}'
        if s["state"] == "RUNNING":
            line += f' — "{s["job"]}" {s["percent"]}% (~{s["remaining_min"]} min left)'
        if not s["connected"]:
            line += "  [MQTT DISCONNECTED]"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def printer_status(printer: str) -> str:
    """Full live status for one printer: state, temps, job, progress, errors."""
    prn = _pick(printer)
    prn.refresh(wait=2.5)
    return json.dumps(prn.snapshot(), indent=2)


@mcp.tool()
def ams_state(printer: str) -> str:
    """AMS tray readout. Colors are given as NAMES, never hues (colorblind-safe)."""
    prn = _pick(printer)
    if _is_octo(prn):
        return f"{prn.name} has no AMS — it prints the loaded filament."
    rows = []
    for t in prn.ams_trays():
        if t["type"]:
            rows.append(f'Tray {t["tray"]} (slot {t["slot_1based"]}): '
                        f'{color_name(t["color"])} {t["type"]}  [{t["color"]}]')
        else:
            rows.append(f'Tray {t["tray"]} (slot {t["slot_1based"]}): EMPTY')
    return "\n".join(rows) or "No AMS data yet — try printer_status first."


@mcp.tool()
def list_sd_files(printer: str, refresh_metadata: bool = False) -> str:
    """List printable .3mf jobs on the printer's SD card. With
    refresh_metadata=true, downloads uncached files to extract print time,
    weight and filament colors (slow — ~30s per file on the P1S)."""
    prn = _pick(printer)
    if _is_octo(prn):
        return json.dumps(prn.files(), indent=2)
    files = prn.sd_list()
    out = []
    for f in files:
        meta = _meta_for(prn, f["name"], f["size"], refresh_metadata)
        entry = {"name": f["name"], "size_mb": round(f["size"] / 1048576, 2),
                 "modified": f["modified"], **_describe_meta(meta)}
        rule = convention_for(f["name"])
        if rule and meta:
            wanted = {color_name(fl["color"]) for fl in meta.get("filaments", [])}
            allowed = set(rule["color"].upper().split("|"))
            if wanted and not (wanted & allowed):
                entry["convention_warning"] = (
                    f'file is sliced {"/".join(sorted(wanted))} but convention for this part '
                    f'is {rule["color"]} — probable slicing error; use ams_mapping to fix at start')
        out.append(entry)
    return json.dumps(out, indent=2)


@mcp.tool()
def suggest_mapping(printer: str, filename: str) -> str:
    """Work out which AMS tray(s) a job should use — applying the user's part-color
    conventions — and return the exact confirm string start_print requires."""
    prn = _pick(printer)
    if _is_octo(prn):
        files = {f["name"]: f for f in prn.files()}
        if filename not in files:
            return f"'{filename}' not on {prn.name}."
        f = files[filename]
        return json.dumps({
            "file": filename, "printer": prn.name,
            "print_time": f.get("print_time"), "weight_g": None,
            "ams_mapping": [], "mapping_described": "the loaded filament (no AMS on this printer)",
            "notes": ["This printer prints with whatever filament is physically loaded — check it."],
            "confirm_string": f"{filename}|{prn.name}|",
            "next_step": ("Ask the human: (1) is the build plate clear? (2) is the right "
                          "filament loaded? Then call start_print with this confirm_string."),
        }, indent=2)
    files = {f["name"]: f for f in prn.sd_list()}
    if filename not in files:
        return f"'{filename}' not on {prn.name}'s SD card."
    meta = _meta_for(prn, filename, files[filename]["size"], True)
    trays = [t for t in prn.ams_trays() if t["type"]]
    rule = convention_for(filename)

    mapping, notes = [], []
    for fl in meta.get("filaments", []):
        want_type = fl["type"]
        want_color = color_name(fl["color"])
        target_colors = [want_color]
        if rule:
            conv = rule["color"].upper().split("|")
            if want_color not in conv:
                notes.append(f'⚠ file wants {want_color} but convention says {rule["color"]} '
                             f'— recommending the CONVENTION color')
                target_colors = conv
        chosen = None
        for tc in target_colors:
            for t in trays:
                if t["type"] == want_type and color_name(t["color"]) == tc:
                    chosen = t
                    break
            if chosen:
                break
        if not chosen and trays:
            chosen = trays[0]
            notes.append(f'⚠ no {want_type} in {"/".join(target_colors)} loaded — '
                         f'falling back to tray {chosen["tray"]} '
                         f'({color_name(chosen["color"])}); swap spools if that is wrong')
        mapping.append(chosen["tray"] if chosen else 0)

    tray_desc = ", ".join(f'tray {t} ({color_name(next(x["color"] for x in trays if x["tray"] == t))})'
                          for t in mapping)
    confirm = f"{filename}|{prn.name}|{','.join(map(str, mapping))}"
    return json.dumps({
        "file": filename, "printer": prn.name,
        "print_time": meta.get("print_time"), "weight_g": meta.get("weight_g"),
        "ams_mapping": mapping, "mapping_described": tray_desc,
        "notes": notes,
        "confirm_string": confirm,
        "next_step": ("Ask the human: (1) is the build plate clear? (2) approve "
                      f"printing with {tray_desc}? Then call start_print with this confirm_string."),
    }, indent=2)


@mcp.tool()
def start_print(printer: str, filename: str, ams_mapping: list[int], confirm: str) -> str:
    """START A PRINT from the SD card. `confirm` MUST be the exact confirm_string
    from suggest_mapping — this proves the job was described to the human first.
    Never call this without explicit human approval and a plate-clear check."""
    prn = _pick(printer)
    expected = f"{filename}|{prn.name}|{','.join(map(str, ams_mapping))}"
    if confirm != expected:
        return (f"REFUSED: confirm string mismatch.\nExpected: {expected}\nGot:      {confirm}\n"
                "Run suggest_mapping and relay its output to the human first.")
    s = prn.snapshot()
    if s["state"] in ("RUNNING", "PREPARE", "PAUSE"):
        return f"REFUSED: {prn.name} is {s['state']} on '{s['job']}'. Cancel or wait first."
    prn.start_print(filename, ams_mapping)
    time.sleep(8)
    s2 = prn.snapshot()
    return json.dumps({"sent": True, "state_after_8s": s2["state"],
                       "job": s2["job"],
                       "note": "PREPARE/RUNNING = accepted. If state unchanged, the printer "
                               "may not be in LAN mode (P1S requires it) or is settling — "
                               "check printer_status in 30s."}, indent=2)


@mcp.tool()
def cancel_print(printer: str) -> str:
    """Cancel the current job. (Post-cancel state reads FAILED — that is Bambu's
    normal 'aborted by user', not an error.)"""
    prn = _pick(printer)
    prn.stop()
    time.sleep(4)
    return f"stop sent — state now: {prn.snapshot()['state']}"


@mcp.tool()
def pause_print(printer: str) -> str:
    """Pause the current job."""
    _pick(printer).pause()
    return "pause sent"


@mcp.tool()
def resume_print(printer: str) -> str:
    """Resume a paused job."""
    _pick(printer).resume()
    return "resume sent"


@mcp.tool()
def chamber_light(printer: str, on: bool) -> str:
    """Turn the chamber light on/off."""
    _pick(printer).light(on)
    return f"light {'on' if on else 'off'} sent"


@mcp.tool()
def set_tray(printer: str, tray_id: int, material: str, color: str) -> str:
    """Register a tray after a spool swap. `color` is a palette NAME (GREEN,
    ORANGE, RED, BLUE, BLACK, WHITE...) — the server supplies the exact hex;
    nobody ever has to pick or verify a hue by eye."""
    if _is_octo(_pick(printer)):
        return f"{printer} has no AMS trays to set."
    name = color.upper()
    if name not in PALETTE:
        return f"Unknown color '{color}'. Palette: {', '.join(PALETTE)}"
    prn = _pick(printer)
    prn.set_tray(tray_id, material.upper(), PALETTE[name])
    time.sleep(3)
    return f"tray {tray_id} set to {name} {material.upper()} — verify:\n" + ams_state(printer)


@mcp.tool()
def delete_sd_file(printer: str, filename: str, confirm: str) -> str:
    """Delete a file from the SD card. confirm must equal the filename."""
    if confirm != filename:
        return "REFUSED: confirm must equal filename exactly."
    _pick(printer).sd_delete(filename)
    return f"deleted {filename}"


# ------------------------------------------------------------- dashboard web UI
# Same brain, second face: a touch dashboard served from this container.
# All write paths reuse the exact gates the MCP tools enforce.

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

_DASH_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


@mcp.custom_route("/", methods=["GET"])
async def _root(request: Request):
    return HTMLResponse('<meta http-equiv="refresh" content="0; url=/dashboard">')


@mcp.custom_route("/dashboard", methods=["GET"])
async def _dashboard(request: Request):
    with open(_DASH_PATH) as fh:
        return HTMLResponse(fh.read())


@mcp.custom_route("/api/ui-version", methods=["GET"])
async def _api_ui_version(request: Request):
    return JSONResponse({"v": str(os.path.getmtime(_DASH_PATH))})


@mcp.custom_route("/api/fleet", methods=["GET"])
async def _api_fleet(request: Request):
    out = []
    for prn in PRINTERS.values():
        s = prn.snapshot()
        out.append(s)
    return JSONResponse(out)


@mcp.custom_route("/api/printer/{name}", methods=["GET"])
async def _api_printer(request: Request):
    prn = _pick(request.path_params["name"])
    trays = []
    for t in prn.ams_trays():
        trays.append({**t, "color_name": color_name(t["color"]) if t["color"] else None})
    return JSONResponse({"snapshot": prn.snapshot(), "trays": trays,
                         "palette": sorted(PALETTE.keys())})


@mcp.custom_route("/api/printer/{name}/files", methods=["GET"])
async def _api_files(request: Request):
    refresh = request.query_params.get("refresh") == "1"
    raw = list_sd_files(request.path_params["name"], refresh_metadata=refresh)
    return JSONResponse(json.loads(raw))


@mcp.custom_route("/api/printer/{name}/suggest", methods=["POST"])
async def _api_suggest(request: Request):
    body = await request.json()
    raw = suggest_mapping(request.path_params["name"], body["filename"])
    try:
        return JSONResponse(json.loads(raw))
    except Exception:
        return JSONResponse({"error": raw}, status_code=400)


@mcp.custom_route("/api/printer/{name}/start", methods=["POST"])
async def _api_start(request: Request):
    body = await request.json()
    if not body.get("plate_clear"):
        return JSONResponse({"error": "plate_clear must be confirmed"}, status_code=400)
    raw = start_print(request.path_params["name"], body["filename"],
                      body["ams_mapping"], body["confirm"])
    if raw.startswith("REFUSED"):
        return JSONResponse({"error": raw}, status_code=409)
    return JSONResponse(json.loads(raw))


@mcp.custom_route("/api/printer/{name}/action", methods=["POST"])
async def _api_action(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    act = body.get("action")
    if act == "cancel":
        return JSONResponse({"result": cancel_print(name)})
    if act == "pause":
        return JSONResponse({"result": pause_print(name)})
    if act == "resume":
        return JSONResponse({"result": resume_print(name)})
    if act in ("light_on", "light_off"):
        return JSONResponse({"result": chamber_light(name, act == "light_on")})
    return JSONResponse({"error": f"unknown action {act}"}, status_code=400)


@mcp.custom_route("/api/printer/{name}/tray", methods=["POST"])
async def _api_tray(request: Request):
    body = await request.json()
    res = set_tray(request.path_params["name"], int(body["tray_id"]),
                   body["material"], body["color"])
    return JSONResponse({"result": res})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
