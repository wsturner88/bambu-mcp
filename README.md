# bambu-mcp 🖨️🤖

**Give any AI agent hands on your Bambu Lab printers — over your own LAN, no cloud.**

An [MCP](https://modelcontextprotocol.io) server that lets Claude, or any
MCP-capable agent, browse the jobs already on your printer's SD card, read live
AMS spool state, and **start prints with explicit spool selection** — the thing
Bambu gates behind its cloud, and that most third-party screens (looking at you,
Panda Touch) can't do because they can't send an AMS mapping table.

```
You:    what can I print?
Agent:  9 jobs on the P1S. Heads up — "Ryobi 40V-Hook" is sliced WHITE but your
        convention for Ryobi parts is GREEN.
You:    print it in green
Agent:  Tray 0 (GREEN PETG), 8h06m, 224g. Plate clear?
You:    yes
Agent:  ✅ RUNNING.
```

Runs anywhere Docker runs (Raspberry Pi included — multi-arch image), speaks
streamable-HTTP MCP on port 8271. Pair it with Tailscale and you can start
prints from anywhere on earth without exposing anything to the internet.

## Features

- 📋 **`list_sd_files`** — every `.3mf` on the card, enriched with print time,
  weight, and filament colors parsed from the file itself (cached after first read)
- 🧵 **`ams_state`** — live spool readout, **colors as names** ("Tray 0: GREEN PETG")
- 🎯 **`suggest_mapping`** — matches a job's filaments to your loaded spools,
  applies your part-color conventions, flags slicing errors
- 🖨️ **`start_print`** — from SD card, with `ams_mapping` spool override
- ⏯️ `pause_print` / `resume_print` / `cancel_print` / `chamber_light`
- 🔄 **`set_tray`** — re-register a tray after a spool swap, by color *name*
- 🗑️ `delete_sd_file`, `printer_status`, `list_printers`

### Designed-in safety
- `start_print` **refuses** unless the agent passes the exact confirm string that
  `suggest_mapping` produced — an agent cannot start a job it hasn't accurately
  described to a human first. Busy printers refuse starts outright.
- **Colorblind-safe:** every color leaves the server as a NAME, never a bare hex
  or hue. `set_tray` takes names; the server owns the hex palette.
- One long-lived MQTT session per printer with gentle reconnects — the P1-series
  ESP32 broker destabilizes (and kicks other clients like OctoEverywhere
  companions) when clients churn connections.

## ⚠️ Firmware compatibility — read this first

Bambu has been progressively locking down local control ("Authorization Control").
What you need for **print-start** to work over local MQTT:

| Printer | Firmware | What's required |
|---|---|---|
| P1P/P1S | ≤ 01.07.x | **LAN-only mode** (no Developer Mode toggle exists — LAN mode alone = full control). In cloud mode these firmwares accept status/light/stop but **silently ignore print-start**. |
| P1P/P1S | ≥ 01.08.03 | LAN-only mode **+ Developer Mode** |
| A1 / A1 mini | ~01.08.x (pre-auth-control) | Works even in **cloud mode** (verified on 01.08.01.00) |
| X1 series / newer fw | with Authorization Control | LAN-only mode **+ Developer Mode** |

Monitoring/read tools generally work in any mode. If `start_print` reports
"sent" but the state never changes, your printer is gating the command — check
the table. After toggling LAN mode, give the printer a minute to settle (the
first command right after a toggle is often ignored). Toggling LAN/Developer
mode **may regenerate your access code** — update `.env` (and anything else
using it) if so.

Good news: OctoEverywhere companions, a Panda Touch, and this server can all
share a printer simultaneously — verified live.

## Quick start

```bash
mkdir bambu-mcp && cd bambu-mcp
curl -LO https://raw.githubusercontent.com/wsturner88/bambu-mcp/main/docker-compose.ghcr.yml
curl -Lo config.yml https://raw.githubusercontent.com/wsturner88/bambu-mcp/main/app/config.example.yml
curl -Lo .env https://raw.githubusercontent.com/wsturner88/bambu-mcp/main/.env.example
# edit config.yml (your printers' IPs + serials) and .env (access codes), then:
docker compose -f docker-compose.ghcr.yml up -d
```

The prebuilt image is `ghcr.io/wsturner88/bambu-mcp:latest` (amd64 + arm64).
Building from source instead: clone the repo, copy the two config files the same
way, and `docker compose up -d --build`.

### Finding your printer's details
- **Access code:** printer screen → Settings → WLAN.
- **Serial:** printer screen → Settings → Device, or — no login required —
  read it off the printer's TLS certificate:
  `echo | openssl s_client -connect <printer-ip>:8883 2>/dev/null | openssl x509 -noout -subject`
- **IP:** your router's client list. Set a DHCP reservation — Bambu printers
  don't do static IPs well.

### Connect an agent
```bash
# Claude Code
claude mcp add --transport http bambu-fleet http://<host>:8271/mcp
```
Any other MCP client: streamable-http transport, same URL. Over Tailscale,
use the host's tailnet address and print from anywhere.

## How it works

Pure local protocols, no Bambu account, no cloud:
- **MQTT (TLS :8883)** — `bblp` + access code; topics `device/<serial>/report|request`.
  Print start is the `project_file` command with `"url": "ftp:///<file>.gcode.3mf"`
  (three slashes = SD root) and an `ams_mapping` array of tray IDs.
- **FTPS (implicit TLS :990)** — lists/downloads the SD card; `.3mf` metadata
  (time, weight, filament colors) parsed from `Metadata/slice_info.config`.

## Disclaimer

Not affiliated with or endorsed by Bambu Lab. Uses the printers' local network
interfaces, which Bambu may change in any firmware update. **A remotely started
print on an unattended printer with something on the build plate can damage your
machine — the confirm-gate exists for a reason; keep a human in the loop.**
MIT licensed; you assume all risk.

---
*Built in an afternoon of live protocol archaeology on a real P1S (fw 01.07,
LAN mode) and A1 (fw 01.08.01, cloud mode) — every recipe in this repo was
verified on actual hardware before it was written down.*
