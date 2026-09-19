# HydroWatch — Open-Source Flood Prediction Node

HydroWatch fuses **edge hardware** (Arduino UNO Q + Modulino sensors) with
**space telemetry** (NASA satellite precipitation) to detect rising water in
real time and issue verified flood alerts before flash floods hit — built for
the kind of hyper-fast climate events (like the August 2026 Langtang Lirung
glacier-collapse debris flows) that traditional warning systems can't outrun.

## Architecture

```
┌─────────────────────────  Arduino UNO Q  ─────────────────────────┐
│                                                                    │
│  MCU (Zephyr / C++)                    Linux core (this app)       │
│  ┌──────────────────────┐   USB CDC    ┌─────────────────────────┐  │
│  │ hydrowatch.ino       │ ───────────► │ server.py  (this node)  │  │
│  │  • Modulino Distance │  115200 8N1  │  ├── alert_engine.py    │  │
│  │  • Modulino Movement │              │  ├── nasa_precip.py     │  │
│  │  • DHT11 (D2)        │              │  ├── open_meteo.py      │  │
│  │  • Moisture (A3)     │              │  └── config.json        │  │
│  └──────────────────────┘              │      ▲                  │  │
│                                        │      │ HTTPS            │  │
└────────────────────────────────────────│──────┼──────────────────┘  │
                                         │  NASA POWER (GPM IMPG)
                                         │  Open-Meteo forecast
                                         ▼
                               ┌────────────────────┐
                               │ Live dashboard     │
                               │ (SSE, any browser) │
                               └────────────────────┘
```

## Fusion logic (the core idea)

A flood alert requires **two independent signals to agree** — ground truth
from the sensor node, and sky truth from satellites/models:

| Level | Trigger | Meaning |
|---|---|---|
| `NORMAL` | — | All quiet |
| `WATCH` | Satellite 7-day precip or forecast 6/24 h rain above threshold | Sky says risk is building |
| `WARNING` | Water column rising ≥ `rise_rate_alert_mm_per_s`, sky quiet | Ground says something is happening upstream |
| `CRITICAL` | Rapid rise **AND** heavy precipitation signal | Verified flash-flood alert — automated emergency declaration |

Water level is computed as `baseline_distance − current_distance` (the
Modulino Distance ToF sensor points down at the waterway; rising water closes
the gap). Rise rate is a rolling 2-minute slope. The engine debounces and
rate-limits alerts (`alert_cooldown_s`).

## Files

| File | Role |
|---|---|
| `hydrowatch.ino` | MCU sketch: samples all sensors, prints one JSON-ish line per 500 ms |
| `server.py` | Node backend: serial reader, weather layers, engine, HTTP + SSE API |
| `alert_engine.py` | Thresholds, water level/rise rate, fusion, alert log |
| `nasa_precip.py` | NASA POWER daily precipitation (GPM-aligned), cached, offline-tolerant |
| `open_meteo.py` | Open-Meteo hourly forecast (next 6/24 h rain), cached |
| `config.json` | Node identity, location, serial/HTTP ports, all thresholds |
| `static/` | Dashboard (vanilla HTML/JS/CSS, canvas charts, no build step) |

## Run it

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyserial
.venv/bin/python server.py
# dashboard: http://127.0.0.1:8765
```

Upload the sketch with arduino-cli (or Arduino App Lab):

```bash
arduino-cli compile --fqbn arduino:zephyr:unoq hydrowatch.ino
arduino-cli upload -p COM6 --fqbn arduino:zephyr:unoq hydrowatch.ino
```

## Configure

Edit `config.json`:
- `node.location` — deployment coordinates (drives both weather layers)
- `thresholds.distance_empty_mm` — "no water" distance for the baseline
- `thresholds.rise_rate_alert_mm_per_s` — what counts as a rapid rise
- `thresholds.forecast_rain_24h_mm` / `heavy_rain_mm_per_day` — sky-truth bars
- `alert_cooldown_s` — min seconds between re-raised alerts

On the dashboard, press **Capture baseline** with the sensor over the dry
waterway to zero the water-column measurement.

## API

- `GET /api/status` — full node state (sensors, engine, weather)
- `GET /api/alerts` — alert log
- `POST /api/baseline` — `{"auto": true}` or `{"distance_mm": 850}`
- `GET /events` — SSE stream: `{"type": "sample"|"status"|"alert", ...}`

## Roadmap (from the project description)

- Mesh networking: multiple nodes along a river valley, upstream nodes
  relaying surge velocity downstream node-to-node (LoRa/ESP-NOW), resilient
  to cell/power/internet outages.
- Decentralized verified alerts: sign alert payloads node-side so downstream
  relays and civic endpoints can verify provenance.
- Solar + supercap power for multi-week unattended operation.
