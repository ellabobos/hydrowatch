# HydroWatch — System Overview, Model & Training

An open-source flood-prediction node that fuses **edge hardware** (Arduino UNO Q +
Modulino sensors) with **space telemetry** (NASA satellite precipitation, weather
forecasts) to detect rising water in real time and issue verified flood alerts.

---

## 1. Why HydroWatch exists

In late August 2026, a rock and glacier collapse on Langtang Lirung (Nepal/Tibet)
triggered debris flows and flash floods that killed over 1,400 people. Traditional
warning systems are too slow for hyper-fast events like this — the water arrives
minutes after the trigger, and the decision to evacuate is stuck in bureaucracy.

HydroWatch attacks that gap at the source: a low-cost node placed along a high-risk
waterway measures the water **physically** (distance to the surface, soil wetness,
impact vibrations) and cross-checks it against **satellite-observed and forecast
precipitation**. Only when the physical and sky signals *agree* does the node
escalate to an automatic, verified emergency declaration — cutting false alarms
while removing the human latency between "water is rising" and "people are warned."

### The design principle: coincidence

Every dangerous state in HydroWatch requires **two independent signal families to
agree**. A violent knock with no water is ignored. Rain upstream with no local
response is a WATCH, not an alarm. Rising water with a bone-dry sky is treated as
suspicious. This rule is enforced three times over — in the rule engine, in the
trained model, and in the buzzer's arming policy.

---

## 2. Hardware

| Component | Connection | Role |
|---|---|---|
| Arduino UNO Q | — | Dual-core board: real-time MCU core + Linux core |
| Soil moisture probe | `A3` (analog) | Water contact / ground wetness (ADC 0–1023; wetter = lower) |
| DHT11 | `D2` (digital) | Air temperature + humidity (storm context) |
| Modulino Distance (VL53L0X ToF) | QWIIC (I2C) | Distance to water surface in mm — the ground-truth level channel |
| Modulino Movement (IMU) | QWIIC (I2C, chained) | Impact transients — the debris-strike channel |
| Passive/active buzzer | `D7` | On-MCU alarm (works with Linux/network dead) |
| USB serial | COM6 @ 115200 | Sensor line up, `SKY` weather + `BUZZ TEST` commands down |

The two Modulinos share one QWIIC chain; they use no discrete header pins at all.

---

## 3. Software architecture

```
                 ┌────────────────── UNO Q board ──────────────────┐
   sensors ────► │  MCU core (Zephyr, C++/Arduino)                 │
  A3 / D2 /      │  0.5 s loop: sample → 8 s feature windows →     │
  QWIIC chain    │  quantized-forest inference → p_flood, mcls     │──► serial line
                 │  buzzer alarm state machine (D7)                │
                 └────────────▲────────────────────────────────────┘
                              │ "SKY sat=X fc=Y" down (60 s)
┌─────────────────────────────┴──────────── Linux core (Python) ───────────┐
│ server.py                                                                │
│  serial_reader → alert engine (water level, rise rate, WATCH/WARN/CRIT)  │
│  weather_loop  → NASA POWER daily + Open-Meteo forecast + GPM IMERG      │
│  sky_writer    → normalizes rain into 0–1 model features → MCU           │
│  HTTP: dashboard (SSE live stream) + REST API                            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Firmware (`hydrowatch.ino`)
- 500 ms loop samples all sensors; maintains circular 8 s (moisture, IMU, rise)
  and 2.5 s (distance) windows mirroring the training feature extractor.
- Runs the **quantized forest on the MCU itself** (`firmware/model.h`) — inference
  needs no network, no Linux, no cloud.
- ToF dropouts (`-1`) hold the last good reading (the model was trained on this
  stale-ToF fault state); an all-zero IMU vector is reported as deviation 0 —
  the trained dead-IMU state, never a fake "1 g impact".
- Parses `SKY sat=… fc=…` from the Linux side (until then: dry sky 0/0, the safe
  default) and `BUZZ TEST` for the sounder self-test.

### 3.2 Buzzer alarm policy (on the MCU)
A siren that fires on probability alone would howl all day on a bench. The arm
rule requires **all three**:
1. `p_flood ≥ 0.70` (flood-family vote share),
2. sustained 4 s continuously,
3. **corroborated** — water actually moving (`rise ≥ 0.1 mm/s`) or a debris class.

Release is hysteretic (silence only below 0.55). Patterns encode severity:
debris = continuous siren, flood rise = 1 s/0.5 s urgent beeps, otherwise a slow
advisory beep. Warm-up (`p_flood = -1`) is silent.

### 3.3 Weather layers (`nasa_precip.py`, `open_meteo.py`, `imerg.py`)
| Layer | Product | Cadence / latency | Model channel |
|---|---|---|---|
| Observed rain (preferred) | GPM **IMERG Early**, half-hourly granules, node's 0.1° cell | ~4 h publication latency | `sat` — confirmation |
| Observed rain (fallback) | NASA **POWER** daily `PRECTOTCORR`, 6 h cache | 1–3 day publication lag | `sat` — confirmation |
| Forecast rain | **Open-Meteo** hourly, 15 min cache | real-time | `fc` — lead time |
| Manual override | dashboard / `POST /api/manual-sky` | offline mode, 24 h TTL | wins over all auto layers |

All layers degrade gracefully: fill values (`-999`) are skipped, stale caches are
used during outages, and IMERG falls back to POWER without credentials. `sat` is
always "most recent day with real data", normalized by a heavy-rain anchor
(15 mm/day; IMERG: 6 mm/h over a trailing 3 h window).

### 3.4 Alert engine (`alert_engine.py`)
- Baseline ("empty streambed" distance) persists in `baseline.json` — survives
  reboots; water level = `baseline − current distance`, clamped at 0.
- Rise rate from a 2-minute level history.
- **WATCH** = sky wet (forecast ≥ 10 mm/24 h, or observed ≥ 15 mm on the latest
  real day, or 7-day total high) · **WARNING** = rapid rise, sky quiet ·
  **CRITICAL** = rise ≥ 2 mm/s **and** heavy precipitation signal.
- The engine is failure-isolated: an exception in `ingest()` is logged, never
  allowed to kill the serial data path.

### 3.5 Dashboard & API (`static/`, `server.py`)
Live SSE stream at `/events`, REST at `/api/status`, `/api/alerts`,
`POST /api/baseline`, `POST /api/manual-sky[/clear]`, `POST /api/buzz-test`,
`GET /health`. The dashboard shows every sensor, the sky source *and its
observation date* (staleness is always visible), the ML card with `p_flood` and
buzzer state, the manual sky-input card for offline operation, and charts.

---

## 4. The model

### 4.1 Task
A **4-class classifier over 8-second windows** of sensor features:

| Class | Meaning |
|---|---|
| `normal` | Quiet streambed; no storm signal |
| `rain_approach` | Sky says rain is coming **or just fell upstream** — WATCH state |
| `flood_rise` | Observed rain **and** water physically rising — alarm state |
| `debris_impact` | Flood rise **plus** an impact strike inside the window — the debris-flow signature |

### 4.2 Features (9, in fixed order)
| # | Feature | Window | Physical role |
|---|---|---|---|
| 0 | `f_moisture_wet` | 8 s mean | Soil wetness (water contact conditioning) |
| 1 | `f_moisture_slope` | 8 s slope | Wetting *right now* |
| 2 | `f_dist_mm` | 2.5 s mean | The **gate**: is there water near the sensor at all? |
| 3 | `f_water_rise_mm_s` | 8 s, ≥ 0 | Rising water — the primary alarm trigger |
| 4 | `f_temp_c` | last valid | Storm context (cool + humid = stormy) |
| 5 | `f_humidity` | last valid | Storm context |
| 6 | `f_imu_peak_mg` | 8 s **peak** | Impact transients — peak preserves a strike that a mean would dilute |
| 7 | `f_sat_rain` | bridged 0–1 | NASA-observed rain — **confirmation** channel |
| 8 | `f_fc_rain` | bridged 0–1 | Forecast rain — **lead-time** channel |

Measured behavior (feature sweeps on the deployed model):
- `dist_mm` is the gate — no high probability without water near the sensor.
- `water_rise` is a *conditioned* trigger: rise with a dry sky is distrusted
  (the unphysical regime); the same rise with observed rain slams to ~1.0.
- `imu_peak` barely moves `p_flood` — it **reallocates** votes from `flood_rise`
  to `debris_impact` above ~700 mg. It confirms *character*, not *existence*.
- In an idle context, no single feature swept to any extreme exceeds `p_flood`
  0.05 — false alarms require coincidence, by construction.

### 4.3 Architecture
A random forest, small enough to run on the MCU: **24 trees, depth 10**, over the
9 features. On the MCU, `p_flood = (votes[flood_rise] + votes[debris_impact]) /
total votes`.

**Deployment** (`export_model.py` → `firmware/model.h`): every tree is quantized
to `uint8` comparisons via a per-feature affine transform over the training
range — the hot path has zero floating point. The export is *validated before
emitting*: the exact quantized traversal is replayed in Python against fresh
test draws and must match sklearn ≥ 99% (it passes at 100%). Tie-breaking
matches sklearn exactly (argmax, first-index-wins).

---

## 5. The training process

### 5.1 Why simulation
There is no labeled dataset of "distance + soil + IMU + sky during a real debris
flow" — and you cannot ethically wait for one at a desk. HydroWatch trains on a
**physics-based scenario simulator** (`sensor_sim.py`) that generates realistic
sensor *streams* (not just feature rows), then extracts windows exactly the way
the firmware does. Real satellite/forecast data is used at *runtime* through the
same 0–1 normalization the simulator trains on (anchors mirrored in
`server.py`), so the model's sky inputs are live data, not placeholders.

### 5.2 Scenario library
Six scenario generators, each a physical story over a 96 s stream
(192 samples @ 2 Hz):

| Scenario | Physical content | Label |
|---|---|---|
| `normal` | Dry/quiet, rain < 0.15 | `normal` |
| `dry_bump` | 800–2500 mg strike, **no water** | `normal` — the anti-"motion = danger" test |
| `rain_approach` | Forecast high (0.6–1.0), observed low, water steady | `rain_approach` |
| `rain_fallout` | Observed high, water **not** risen here yet, ground soaking | `rain_approach` — fills the "rain upstream, water hasn't arrived" regime |
| `flood_rise` | Observed high **and** 120–500 mm rise, calm IMU, occasional spray | `flood_rise` |
| `debris_impact` | Flood rise + strike transient **inside the window** + spray | strike windows → `debris_impact`; post-strike windows → `flood_rise` |

Two details matter most:

- **Per-window labeling.** A debris *stream* contains a ~2 s strike followed by
  ordinary flooding. Labeling every window "debris" teaches the model
  "flood-that-might-lack-impact" — which is exactly how dead-DHT flood rows
  leaked into the debris class. Labeling each window by its content (strike
  present → debris, flooded-but-calm → flood) makes the coincidence rule
  literally learnable.
- **Fault augmentation (fault-invariance).** Four fault channels — dead IMU
  (peak 0), dead DHT (frozen 22 °C/45 %), stale ToF (frozen distance), floating
  moisture probe — are injected at per-channel rates *and* in **balanced
  rounds**: extra draws where a fixed fault appears with *every* scenario/label,
  so a dead sensor can never skew any class.

### 5.3 Split discipline
Train = 300 scenario draws/class; test = 80 draws/class from an **independent
RNG seed** — fresh scenarios, never rows of training scenarios (the simulation
analogue of a time-based split, preventing augmentation leakage). Yields
25,920 train / 10,080 test windows (12 windows per stream).

### 5.4 Two-stage training (`train_model.py`)
1. **Teacher** — 300-tree unbounded-depth random forest learns the regimes from
   simulation. Test accuracy **99.79 %**. Stays on the Linux side.
2. **Student** — the deployable 24×depth-10 forest, trained two ways:
   - *distilled*: on features + teacher probabilities (richer targets),
   - *standalone*: on the 9 raw features only.
   Macro-F1: distilled 0.99835 vs standalone **0.99865** — the standalone wins,
   so the deployable variant is also the best variant. It is the one exported.

### 5.5 Acceptance testing
Beyond aggregate accuracy, the model is held to **architectural** requirements,
evaluated per scenario×fault slice (fresh draws per slice):
- a violent dry bump (≥ 1500 mg, no water) must classify `normal`;
- `debris_impact` must require strike + rising water together — never one alone;
- every scenario × fault slice (17 combinations) must reach ≥ 90 % slice accuracy,
  including the compound-fault cases that motivated fault-invariance (e.g.
  flood_rise with a dead DHT: 76.5 % → **99.2 %** after per-window relabeling).

### 5.6 Export and firmware validation (`export_model.py`)
`student_standalone.joblib` → quantized C arrays in `firmware/model.h`
(~90 KB text), then replay-validated against sklearn on fresh draws (must be
≥ 99 % agreement; passes at 100 %). The sketch consumes a single function:
`HW_MODEL_predict(features, votes_out) → class`.

### 5.7 Reproducing the chain
```bash
.venv-w/Scripts/python.exe sensor_sim.py     # dataset smoke test + ranges
.venv-w/Scripts/python.exe train_model.py    # teacher + students → model/
.venv-w/Scripts/python.exe export_model.py   # quantize → firmware/model.h
# then (server stopped — it holds COM6):
"C:/Program Files/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe" \
  compile --fqbn arduino:zephyr:unoq .
... arduino-cli.exe upload -p COM6 --fqbn arduino:zephyr:unoq .
```
If you change the simulator's rain normalization, change `HEAVY_DAY_MM` /
`HEAVY_6H_MM` in `server.py` to match — the model's input scale depends on both.

---

## 6. Repository layout

```
hydrowatch.ino        firmware: sensing, features, on-MCU inference, buzzer
firmware/model.h      quantized forest (generated — do not edit)
sensor_sim.py         scenario simulator + fault augmentation (training data)
train_model.py        two-stage training → model/*.joblib, metrics.json
export_model.py       quantization → firmware/model.h + validation
server.py             Linux-side node: serial, weather, engine, HTTP/SSE
alert_engine.py       baseline/level/rise + WATCH/WARNING/CRITICAL fusion
nasa_precip.py        NASA POWER daily precipitation (cached, fill-aware)
open_meteo.py         Open-Meteo forecast (cached)
imerg.py              GPM IMERG Early half-hourly (optional; POWER fallback)
imerg_creds.json      (local only) Earthdata credentials for IMERG
config.json           node identity, serial/HTTP ports, thresholds, anchors
static/               dashboard (vanilla JS, SSE)
model/                trained artifacts + metrics.json
baseline.json         persisted streambed reference (local state)
```

---

## 7. Honest limitations

1. **Simulation-only training.** Every label comes from the physics simulator;
   the sim-to-real gap is unmeasured until nodes log real floods. Real-world
   data logging is the next milestone.
2. **Sky channels lag.** POWER publishes daily (1–3 day lag); IMERG Early is
   half-hourly but ~4 h behind the sky. The forecast channel is the only true
   lead-time signal. Gridded cells ≠ rain gauges.
3. **ToF geometry.** The VL53L0X ranges 30 mm–2 m; mounting height defines the
   measurement window, and droplets in the beam cause dropouts (the firmware
   and model both tolerate this, but optics still need to stay clean).
4. **One node is one point.** The mesh-network milestone (upstream nodes
   relaying surge velocity downstream node-to-node) is what turns this from a
   prototype into a valley-scale early-warning grid.
