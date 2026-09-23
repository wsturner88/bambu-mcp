"""bambu-mcp — MCP server exposing a Bambu Lab printer fleet to any MCP agent.

Design rules (non-negotiable):
  * COLOR IS ALWAYS TEXT. Colorblind-safe by design — every tool answers with
    color NAMES ("GREEN", "ORANGE"), never bare hex or hue-only signals.
  * start_print is gated: the caller must pass the exact confirm string that
    suggest_mapping / preflight returned. An agent cannot start a job it has
    not accurately described to the human first.
  * One long-lived MQTT session per printer (P1 ESP32 broker hates churn).
"""

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import time

import yaml
from mcp.server.fastmcp import FastMCP

from bambu import (BambuPrinter, OctoPrintPrinter, extract_gcode_png,
                   extract_plate_png, parse_3mf_meta)

CFG_PATH = os.environ.get("BAMBU_MCP_CONFIG", os.path.join(os.path.dirname(__file__), "config.yml"))
CACHE_PATH = os.environ.get("BAMBU_MCP_CACHE", "/data/metacache.json")
# Plate-preview PNGs live beside the cache, one file per cache key (named by
# the key's sha1 so a filename with slashes/odd chars never touches a path).
THUMBS_DIR = os.path.join(os.path.dirname(CACHE_PATH), "thumbs")

with open(CFG_PATH) as fh:
    CFG = yaml.safe_load(fh)

PALETTE = {k.upper(): v.upper() for k, v in CFG["palette"].items()}
RULES = CFG.get("conventions", [])

# Bambu generic material profiles: info_idx, nozzle_temp_min, nozzle_temp_max.
# Order is the button order the dashboard renders (most-used first).
MATERIALS = {
    "PLA":  ("GFL99", 190, 240),
    "PETG": ("GFG99", 220, 270),
    "ABS":  ("GFB99", 240, 280),
    "ASA":  ("GFB98", 240, 280),
    "TPU":  ("GFU99", 200, 250),
}

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

# metacache.json is one shared file — serialize its read/mutate/write so two
# threads (a live /suggest and a background pre-warm) can't clobber each other.
_cache_lock = threading.Lock()

# Bambu printers tolerate ~one FTPS session at a time. One lock per printer,
# created lazily, so a pre-warm and a live /suggest never open two sessions
# to the same printer at once — but different printers can still run in parallel.
_ftps_locks: dict = {}
_ftps_locks_guard = threading.Lock()

def _ftps_lock(printer_name: str) -> threading.Lock:
    with _ftps_locks_guard:
        lock = _ftps_locks.get(printer_name)
        if lock is None:
            lock = _ftps_locks[printer_name] = threading.Lock()
        return lock

# OctoPrint is plain HTTP, not the one-session-at-a-time FTPS above — this
# lock just keeps two prewarm passes on the same printer from opening
# redundant header downloads; different printers still run in parallel.
_octo_locks: dict = {}
_octo_locks_guard = threading.Lock()

def _octo_lock(printer_name: str) -> threading.Lock:
    with _octo_locks_guard:
        lock = _octo_locks.get(printer_name)
        if lock is None:
            lock = _octo_locks[printer_name] = threading.Lock()
        return lock

def _thumb_path(basename: str) -> str:
    return os.path.join(THUMBS_DIR, basename)

def _save_thumb(key: str, png_bytes):
    """Save a plate-preview PNG for a cache key under THUMBS_DIR. Returns the
    basename to record in the cache entry, or None if the file had no plate
    image — recorded as None (not just absent) so _prewarm never retries it."""
    if not png_bytes:
        return None
    basename = hashlib.sha1(key.encode()).hexdigest() + ".png"
    os.makedirs(THUMBS_DIR, exist_ok=True)
    with open(_thumb_path(basename), "wb") as fh:
        fh.write(png_bytes)
    return basename

def _meta_for(prn: BambuPrinter, name: str, size: int, download_if_missing: bool, force: bool = False):
    """force=True re-downloads even if a cache entry already exists — used to
    backfill a thumbnail onto an entry cached before thumbnails existed."""
    key = f"{prn.name}|{name}|{size}"
    if not force:
        with _cache_lock:
            cache = _cache_load()
            if key in cache:
                return cache[key]
    if not download_if_missing:
        return None
    with _ftps_lock(prn.name):
        # someone else may have downloaded this exact file while we waited for the lock
        if not force:
            with _cache_lock:
                cache = _cache_load()
                if key in cache:
                    return cache[key]
        data = prn.sd_download(name)
        meta = parse_3mf_meta(data)
        meta["thumb"] = _save_thumb(key, extract_plate_png(data))
        with _cache_lock:
            cache = _cache_load()
            cache[key] = meta
            _cache_save(cache)
    return meta

def _octo_thumb_for(prn: OctoPrintPrinter, name: str, size: int):
    """Read-only cache lookup for an OctoPrint file's plate thumbnail. Never
    downloads — a live file listing must not block on the printer's network;
    the actual header pull only happens in _prewarm."""
    key = f"{prn.name}|{name}|{size}"
    with _cache_lock:
        cache = _cache_load()
    entry = cache.get(key)
    return entry.get("thumb") if entry else None

def _octo_meta_for(prn: OctoPrintPrinter, name: str, size: int):
    """Download just the gcode header and cache its PrusaSlicer-embedded plate
    thumbnail, if it has one. Unlike Bambu's _meta_for there's no slice_info
    to parse here — OctoPrint's own file API already supplies print time/
    filament — so the cache entry holds only {"thumb": ...}. Only called from
    _prewarm, off the request path."""
    key = f"{prn.name}|{name}|{size}"
    with _octo_lock(prn.name):
        # someone else may have finished this exact file while we waited for the lock
        with _cache_lock:
            cache = _cache_load()
            if "thumb" in cache.get(key, {}):
                return cache[key]
        data = prn.download_head(name)
        thumb = _save_thumb(key, extract_gcode_png(data))
        with _cache_lock:
            cache = _cache_load()
            cache[key] = {"thumb": thumb}
            _cache_save(cache)
    return {"thumb": thumb}

def _cache_purge(printer_name: str, filename: str):
    """Drop every cached metadata entry for a printer+filename (any cached
    size) and its thumb PNG, if any — called after a successful SD delete so
    a stale entry doesn't outlive the file it describes."""
    prefix = f"{printer_name}|{filename}|"
    with _cache_lock:
        cache = _cache_load()
        keys = [k for k in cache if k.startswith(prefix)]
        if not keys:
            return
        for k in keys:
            thumb = cache.pop(k, {}).get("thumb")
            if thumb:
                try:
                    os.remove(_thumb_path(thumb))
                except OSError:
                    pass
        _cache_save(cache)

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

# one background pre-warm at a time per printer — repeated dashboard polls
# (fleet refresh, tapping into a printer) must not stack up duplicate threads
_prewarm_running: set = set()
_prewarm_guard = threading.Lock()

def _prewarm_bambu(prn: BambuPrinter):
    state = prn.snapshot().get("state")
    if state in ("RUNNING", "PREPARE", "PAUSE", "PAUSED"):
        return  # don't pull a big file off a printer that's mid-job
    files = prn.sd_list()
    with _cache_lock:
        cache = _cache_load()
    # (file, force) — force=True means "already cached, but from before
    # thumbnails existed: re-download once to backfill the thumb". An
    # entry with "thumb": null was already tried and had no plate image,
    # so it's left alone rather than retried forever.
    todo = []
    for f in files:
        entry = cache.get(f"{prn.name}|{f['name']}|{f['size']}")
        if entry is None:
            todo.append((f, False))
        elif "thumb" not in entry:
            todo.append((f, True))
    if not todo:
        return
    print(f"[prewarm] {prn.name}: warming {len(todo)} file(s)", flush=True)
    for f, force in todo:
        # re-check every file: a print may have started since we began, and
        # a long FTPS pull during a job is exactly what this guard exists to avoid
        if prn.snapshot().get("state") in ("RUNNING", "PREPARE", "PAUSE", "PAUSED"):
            print(f"[prewarm] {prn.name}: job started — stopping early", flush=True)
            return
        try:
            _meta_for(prn, f["name"], f["size"], True, force=force)
            print(f"[prewarm] {prn.name}: cached {f['name']}", flush=True)
        except Exception as e:
            print(f"[prewarm] {prn.name}: failed on {f['name']}: {e}", flush=True)
        time.sleep(1)  # give a waiting user request a chance at the FTPS lock
    print(f"[prewarm] {prn.name}: done", flush=True)


def _prewarm_octo(prn: OctoPrintPrinter):
    # No idle gate here, unlike the Bambu pass: this reads 1 MB of a file off
    # the OctoPrint Pi's disk over HTTP — the printer itself never sees it, so
    # it's safe mid-job (and MK4 jobs run 9h+; waiting would leave rows blank
    # for most of a day).
    files = prn.files()
    with _cache_lock:
        cache = _cache_load()
    # unlike Bambu there's no "backfill an old entry" case — the cache entry
    # for an OctoPrint file only ever holds "thumb", so missing the key
    # entirely and missing "thumb" on it are the same condition
    todo = [f for f in files if "thumb" not in cache.get(f"{prn.name}|{f['name']}|{f['size']}", {})]
    if not todo:
        return
    print(f"[prewarm] {prn.name}: warming {len(todo)} file(s)", flush=True)
    for f in todo:
        try:
            _octo_meta_for(prn, f["name"], f["size"])
            print(f"[prewarm] {prn.name}: cached {f['name']}", flush=True)
        except Exception as e:
            print(f"[prewarm] {prn.name}: failed on {f['name']}: {e}", flush=True)
        time.sleep(1)  # give a waiting user request a chance at the header download
    print(f"[prewarm] {prn.name}: done", flush=True)


def _prewarm(printer_name: str):
    """Background: quietly download+cache metadata for a printer's uncached
    files (SD-card .3mf metadata for Bambu, gcode-header plate thumbnails for
    OctoPrint), so a later /suggest or file listing never has to eat a slow
    download itself. Never raises — a failure here must not affect any live
    request."""
    with _prewarm_guard:
        if printer_name in _prewarm_running:
            return
        _prewarm_running.add(printer_name)
    try:
        prn = PRINTERS.get(printer_name.lower())
        if not prn:
            return
        if _is_octo(prn):
            _prewarm_octo(prn)
        else:
            _prewarm_bambu(prn)
    except Exception as e:
        print(f"[prewarm] {printer_name}: aborted: {e}", flush=True)
    finally:
        with _prewarm_guard:
            _prewarm_running.discard(printer_name)

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
        files = prn.files()
        for f in files:
            f["thumb"] = _octo_thumb_for(prn, f["name"], f["size"])
        return json.dumps(files, indent=2)
    files = prn.sd_list()
    out = []
    for f in files:
        meta = _meta_for(prn, f["name"], f["size"], refresh_metadata)
        entry = {"name": f["name"], "size_mb": round(f["size"] / 1048576, 2),
                 "modified": f["modified"], "thumb": meta.get("thumb") if meta else None,
                 **_describe_meta(meta)}
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


# Pure mapping logic, pulled out of suggest_mapping so it's independently testable
# (no printer/network calls inside — just filaments + trays + a convention rule).
def _map_filaments(filaments: list, trays: list, rule) -> tuple:
    """Assign each USED slicer filament (by its 1-based `id`) to a free AMS tray.

    Match order per filament, never reusing a tray already claimed by another
    filament in this job: (1) same type + target color, (2) same type any
    color (with a note), (3) nothing of that type free at all — single-filament
    jobs fall back to the first tray (today's behavior, the user can tap a
    different spool on the dashboard); multi-filament jobs are unsafe to guess
    at, so that filament is left unmapped (-1) and `blocked` is set.

    The part-color convention only steers the PRIMARY filament (largest
    used_g, or the first filament if none have used_g) — a support/interface
    filament keeps the file's own color and gets no convention warning.

    Returns (mapping, clauses, notes, blocked):
      mapping  — 0-based tray ids, list length = max slicer filament id,
                 indexed by id-1 (`ams_mapping` for project_file), -1 = unused/unmatched
      clauses  — one human line per used filament, e.g. "filament 1 PETG → slot 2 (GREEN PETG)"
      notes    — ⚠ warnings to surface to the human
      blocked  — ⛔ string if the job cannot be safely started as-is, else None
    """
    if not filaments:
        return [], [], [], None

    used_gs = [f.get("used_g") for f in filaments]
    if any(g is not None for g in used_gs):
        primary = max(filaments, key=lambda f: f.get("used_g") or 0)
    else:
        primary = filaments[0]  # no used_g anywhere (old cache entry, or slicer omitted it)

    max_id = max((f.get("id") or i + 1) for i, f in enumerate(filaments))
    mapping = [-1] * max_id
    clauses, notes, blocked_msgs = [], [], []
    claimed: set = set()
    multi = len(filaments) > 1

    for i, fl in enumerate(filaments):
        fid = fl.get("id") or (i + 1)  # shouldn't happen, but fall back to list position
        slot_idx = fid - 1
        want_type = fl["type"]
        want_color = color_name(fl["color"])
        target_colors = [want_color]
        if rule and fl is primary:
            conv = rule["color"].upper().split("|")
            if want_color not in conv:
                notes.append(f'⚠ file wants {want_color} but convention says {rule["color"]} '
                             f'— recommending the CONVENTION color')
                target_colors = conv

        chosen, note = None, None
        # (1) same type + target color, unclaimed
        for tc in target_colors:
            for t in trays:
                if t["tray"] not in claimed and t["type"] == want_type and color_name(t["color"]) == tc:
                    chosen = t
                    break
            if chosen:
                break
        # (2) same type, any color, unclaimed
        if not chosen:
            for t in trays:
                if t["tray"] not in claimed and t["type"] == want_type:
                    chosen = t
                    note = (f'⚠ no {want_type} in {"/".join(target_colors)} loaded — using slot '
                            f'{t["slot_1based"]} ({color_name(t["color"])} {want_type})')
                    break
        # (3) no tray of that type free at all
        if not chosen:
            if multi:
                blocked_msgs.append(
                    f'⛔ this job needs {want_type} ({"/".join(target_colors)}) but no free '
                    f'{want_type} spool is registered in the AMS — load it and set the '
                    f"slot's material with Change")
            elif trays:
                chosen = trays[0]
                note = (f'⚠ no {want_type} in {"/".join(target_colors)} loaded — '
                        f'falling back to tray {chosen["tray"]} '
                        f'({color_name(chosen["color"])}); swap spools if that is wrong')
        if note:
            notes.append(note)

        if chosen:
            claimed.add(chosen["tray"])
            mapping[slot_idx] = chosen["tray"]
            clauses.append(f'filament {fid} {want_type} → slot {chosen["slot_1based"]} '
                           f'({color_name(chosen["color"])} {want_type})')
        else:
            clauses.append(f'filament {fid} {want_type} → UNASSIGNED')

    blocked = " | ".join(blocked_msgs) if blocked_msgs else None
    return mapping, clauses, notes, blocked


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
            "blocked": None,
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

    mapping, clauses, notes, blocked = _map_filaments(meta.get("filaments", []), trays, rule)
    mapping_described = "\n".join(clauses)
    confirm = "" if blocked else f"{filename}|{prn.name}|{','.join(map(str, mapping))}"
    if blocked:
        next_step = f"BLOCKED — {blocked} Resolve that first; do not call start_print."
    else:
        next_step = ("Ask the human: (1) is the build plate clear? (2) approve "
                     f"printing with: {'; '.join(clauses)}? Then call start_print with this confirm_string.")
    return json.dumps({
        "file": filename, "printer": prn.name,
        "print_time": meta.get("print_time"), "weight_g": meta.get("weight_g"),
        "ams_mapping": mapping, "mapping_described": mapping_described,
        "notes": notes,
        "blocked": blocked,
        "confirm_string": confirm,
        "next_step": next_step,
    }, indent=2)


@mcp.tool()
def start_print(printer: str, filename: str, ams_mapping: list[int], confirm: str) -> str:
    """START A PRINT from the SD card. `confirm` MUST be the exact confirm_string
    from suggest_mapping — this proves the job was described to the human first.
    Never call this without explicit human approval and a plate-clear check."""
    prn = _pick(printer)
    # OctoPrint printers legitimately send an empty mapping + a confirm string
    # ending in "|" (no AMS to map). A Bambu printer never should — an empty
    # confirm or an all -1 mapping means suggest_mapping blocked this job.
    if not _is_octo(prn) and (not confirm or not any(t >= 0 for t in ams_mapping)):
        return ("REFUSED: empty confirm string or no valid tray in ams_mapping — "
                "suggest_mapping blocked this job (see its notes). Load the missing "
                "filament and re-run suggest_mapping.")
    expected = f"{filename}|{prn.name}|{','.join(map(str, ams_mapping))}"
    if confirm != expected:
        return (f"REFUSED: confirm string mismatch.\nExpected: {expected}\nGot:      {confirm}\n"
                "Run suggest_mapping and relay its output to the human first.")
    s = prn.snapshot()
    if s["state"] in ("RUNNING", "PREPARE", "PAUSE"):
        return f"REFUSED: {prn.name} is {s['state']} on '{s['job']}'. Cancel or wait first."
    if not s.get("connected") or s["state"] in ("UNKNOWN", "DISCONNECTED"):
        return (f"REFUSED: {prn.name} is not connected (state {s['state']}). "
                "Connect the printer first, then retry.")
    try:
        prn.start_print(filename, ams_mapping)
    except Exception as e:
        return f"REFUSED: {e}"
    time.sleep(8)
    s2 = prn.snapshot()
    return json.dumps({"sent": True, "state_after_8s": s2["state"],
                       "job": s2["job"],
                       "note": "PREPARE/RUNNING = accepted. If state unchanged, the printer "
                               "may not be in LAN mode (P1S requires it) or is settling — "
                               "check printer_status in 30s."}, indent=2)


@mcp.tool()
def connect_printer(printer: str) -> str:
    """Ask an OctoPrint-driven printer to open its serial connection to the
    printer (saved port/baud). Bambu printers connect automatically over MQTT
    and don't need this."""
    prn = _pick(printer)
    if not _is_octo(prn):
        return f"{prn.name}: not applicable — Bambu printers connect automatically."
    try:
        return prn.connect_serial()
    except Exception as e:
        return f"REFUSED: {e}"


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
    """Register a tray after a spool swap. `material` is PLA/PETG/ABS/ASA/TPU;
    `color` is a palette NAME (GREEN, ORANGE, RED, BLUE, BLACK, WHITE...) — the
    server supplies the exact hex and the material's generic temp profile;
    nobody ever has to pick or verify a hue by eye."""
    if _is_octo(_pick(printer)):
        return f"{printer} has no AMS trays to set."
    name = color.upper()
    if name not in PALETTE:
        return f"Unknown color '{color}'. Palette: {', '.join(PALETTE)}"
    mat = material.upper()
    if mat not in MATERIALS:
        return f"Unknown material '{material}'. Materials: {', '.join(MATERIALS)}"
    info_idx, temp_min, temp_max = MATERIALS[mat]
    prn = _pick(printer)
    prn.set_tray(tray_id, mat, PALETTE[name],
                 temp_min=temp_min, temp_max=temp_max, info_idx=info_idx)
    time.sleep(3)
    return f"tray {tray_id} set to {name} {mat} — verify:\n" + ams_state(printer)


@mcp.tool()
def delete_sd_file(printer: str, filename: str, confirm: str) -> str:
    """Delete a file from the SD card. confirm must equal the filename.
    Refuses while a job is active, and if filename isn't on the printer's
    current file list (guards against typos deleting the wrong/no file)."""
    if confirm != filename:
        return "REFUSED: confirm must equal filename exactly."
    prn = _pick(printer)
    s = prn.snapshot()
    if s["state"] in ("RUNNING", "PREPARE", "PAUSE"):
        return f"REFUSED: {prn.name} is printing — delete is not allowed while a job is active"
    files = prn.files() if _is_octo(prn) else prn.sd_list()
    if filename not in {f["name"] for f in files}:
        return f"REFUSED: '{filename}' is not on {prn.name}'s current file list."
    prn.sd_delete(filename)
    _cache_purge(prn.name, filename)
    return f"deleted {filename}"


# ------------------------------------------------------------- dashboard web UI
# Same brain, second face: a touch dashboard served from this container.
# All write paths reuse the exact gates the MCP tools enforce.

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

_DASH_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")
# thumb basenames are always sha1-of-cache-key + ".png" (see _save_thumb) —
# enforce that shape so the path param can never walk out of THUMBS_DIR.
_THUMB_NAME_RE = re.compile(r"^[0-9a-f]{40}\.png$")


async def _run(fn, *a, **kw):
    """Run a synchronous (blocking) printer call off the event loop, so a slow
    or stuck printer call (e.g. the 8s settle in start_print) doesn't freeze
    fleet polling / other requests."""
    return await asyncio.to_thread(fn, *a, **kw)


def _safe_route(fn):
    """Wrap a custom_route handler so ANY exception inside it becomes a JSON
    error body with a 500 status — never a bare/non-JSON error page. The
    dashboard's api() helper always gets JSON back, even on a crash."""
    async def wrapper(request):
        try:
            return await fn(request)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    wrapper.__name__ = getattr(fn, "__name__", "route")
    return wrapper


@mcp.custom_route("/", methods=["GET"])
@_safe_route
async def _root(request: Request):
    return HTMLResponse('<meta http-equiv="refresh" content="0; url=/dashboard">')


@mcp.custom_route("/dashboard", methods=["GET"])
@_safe_route
async def _dashboard(request: Request):
    with open(_DASH_PATH) as fh:
        return HTMLResponse(fh.read())


@mcp.custom_route("/api/ui-version", methods=["GET"])
async def _api_ui_version(request: Request):
    return JSONResponse({"v": str(os.path.getmtime(_DASH_PATH))})


@mcp.custom_route("/api/thumb/{basename}", methods=["GET"])
@_safe_route
async def _api_thumb(request: Request):
    basename = request.path_params["basename"]
    if not _THUMB_NAME_RE.match(basename):
        return JSONResponse({"error": "invalid thumbnail name"}, status_code=404)
    path = _thumb_path(basename)

    def work():
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    data = await _run(work)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@mcp.custom_route("/api/fleet", methods=["GET"])
@_safe_route
async def _api_fleet(request: Request):
    def work():
        return [prn.snapshot() for prn in PRINTERS.values()]
    out = await _run(work)
    return JSONResponse(out)


@mcp.custom_route("/api/printer/{name}", methods=["GET"])
@_safe_route
async def _api_printer(request: Request):
    name = request.path_params["name"]

    def work():
        prn = _pick(name)
        trays = []
        for t in prn.ams_trays():
            trays.append({**t, "color_name": color_name(t["color"]) if t["color"] else None})
        return {"snapshot": prn.snapshot(), "trays": trays,
                "palette": sorted(PALETTE.keys()), "materials": list(MATERIALS),
                "kind": prn.kind}
    return JSONResponse(await _run(work))


@mcp.custom_route("/api/printer/{name}/files", methods=["GET"])
@_safe_route
async def _api_files(request: Request):
    refresh = request.query_params.get("refresh") == "1"
    name = request.path_params["name"]
    raw = await _run(list_sd_files, name, refresh_metadata=refresh)
    out = json.loads(raw)
    # quietly cache metadata for anything still uncached, so the next tap of
    # PRINT on this printer doesn't have to eat a full FTPS download itself
    threading.Thread(target=_prewarm, args=(name,), daemon=True).start()
    return JSONResponse(out)


@mcp.custom_route("/api/printer/{name}/suggest", methods=["POST"])
@_safe_route
async def _api_suggest(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    raw = await _run(suggest_mapping, name, body["filename"])
    try:
        return JSONResponse(json.loads(raw))
    except Exception:
        return JSONResponse({"error": raw}, status_code=400)


@mcp.custom_route("/api/printer/{name}/start", methods=["POST"])
@_safe_route
async def _api_start(request: Request):
    body = await request.json()
    if not body.get("plate_clear"):
        return JSONResponse({"error": "plate_clear must be confirmed"}, status_code=400)
    name = request.path_params["name"]
    raw = await _run(start_print, name, body["filename"], body["ams_mapping"], body["confirm"])
    if raw.startswith("REFUSED"):
        return JSONResponse({"error": raw}, status_code=409)
    return JSONResponse(json.loads(raw))


@mcp.custom_route("/api/printer/{name}/action", methods=["POST"])
@_safe_route
async def _api_action(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    act = body.get("action")
    if act == "cancel":
        res = await _run(cancel_print, name)
    elif act == "pause":
        res = await _run(pause_print, name)
    elif act == "resume":
        res = await _run(resume_print, name)
    elif act in ("light_on", "light_off"):
        res = await _run(chamber_light, name, act == "light_on")
    elif act == "connect":
        res = await _run(connect_printer, name)
    else:
        return JSONResponse({"error": f"unknown action {act}"}, status_code=400)
    if isinstance(res, str) and res.startswith("REFUSED"):
        return JSONResponse({"error": res}, status_code=409)
    return JSONResponse({"result": res})


@mcp.custom_route("/api/printer/{name}/tray", methods=["POST"])
@_safe_route
async def _api_tray(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    res = await _run(set_tray, name, int(body["tray_id"]), body["material"], body["color"])
    return JSONResponse({"result": res})


@mcp.custom_route("/api/printer/{name}/delete", methods=["POST"])
@_safe_route
async def _api_delete(request: Request):
    body = await request.json()
    name = request.path_params["name"]
    raw = await _run(delete_sd_file, name, body["filename"], body["confirm"])
    if raw.startswith("REFUSED"):
        return JSONResponse({"error": raw}, status_code=409)
    return JSONResponse({"result": raw})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
