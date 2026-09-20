"""HydroWatch — sensor simulation v2 for training-data augmentation.

v2 changes (driven by model-probing findings on the live node):
  - f_imu_peak_mg replaces the 8 s mean: a debris strike is a transient,
    and averaging dilutes a 1500 mg spike to ~120 mg. Peak preserves it.
  - f_sky_rain splits into f_sat_rain (NASA-observed, confirmation channel)
    and f_fc_rain (forecast, lead-time channel). rain_approach is keyed on
    forecast ONLY; flood_rise requires observed rain agreement.
  - debris_impact is defined by physics: water rising AND a strike transient
    INSIDE the extracted window (the old simulator decayed the shake before
    the feature window, so the class was learnable only by artifacts).
  - New dry_bump scenario (labeled normal): violent motion without water —
    the model must learn coincidence, not "motion = danger".
  - Fault augmentation: dead IMU (firmware reports peak 0), dead DHT
    (nominal 22 C / 45 %), stale ToF (frozen distance), noisy probe.

All moisture values are raw ADC counts (dry ~ 850-1023, wetter = lower on
this capacitive probe); distance is mm from the ToF to the water surface.
"""

from __future__ import annotations

import numpy as np

RNG = np.random.default_rng(20260919)

MOISTURE_DRY_RANGE = (840.0, 1023.0)
MOISTURE_WET_FLOOR = 180.0
DIST_EMPTY_MM = 877.0
NOISE = {"moisture": 6.0, "distance": 3.0, "temp_c": 0.8, "humidity": 3.0, "imu": 25.0}
GRAVITY_MG = 1000.0
WIN_S = 8.0          # feature window length in seconds (matches firmware)
SAMPLE_S = 0.5

FEATURE_ORDER = [
    "f_moisture_wet", "f_moisture_slope", "f_dist_mm", "f_water_rise_mm_s",
    "f_temp_c", "f_humidity", "f_imu_peak_mg", "f_sat_rain", "f_fc_rain",
]
CLASS_NAMES = ["normal", "rain_approach", "flood_rise", "debris_impact"]


def _imu_stream(rng, n, fault=False):
    """Acceleration magnitude deviation stream in mg."""
    if fault:                       # dead IMU: firmware reports peak 0
        return np.zeros(n)
    dev = np.abs(rng.normal(0, NOISE["imu"], n)) + 10.0
    return dev


def _strike(rng, n, start, amp_mg, decay_s=1.2):
    """Impact transient: sharp rise, ~1 s decay, superimposed on the stream."""
    t = np.arange(n) * SAMPLE_S
    env = np.zeros(n)
    idx = t >= start
    env[idx] = amp_mg * np.exp(-(t[idx] - start) / decay_s)
    return env


def _tof_stream(rng, n, rise_total, fault=False, spray=False):
    """Distance stream: level falls as water rises (dist = range to water)."""
    if fault:                       # stale ToF: frozen reading
        return np.full(n, DIST_EMPTY_MM - 0.3 * rise_total)
    base = DIST_EMPTY_MM - np.linspace(0, rise_total, n)
    noise = rng.normal(0, NOISE["distance"], n)
    out = base + noise
    if spray:                       # droplets in the beam: wild spikes
        spikes = rng.random(n) < 0.04
        out[spikes] += rng.uniform(-80, 80, spikes.sum())
    return out


def _moisture_stream(rng, n, soak_frac, fault=False):
    if fault:                       # disconnected probe: wild float
        return np.clip(np.cumsum(rng.normal(0, 120, n)) % 1024, 0, 1023)
    wet_base = 1023.0 - rng.uniform(*MOISTURE_DRY_RANGE)
    wet = wet_base + (1023.0 - MOISTURE_WET_FLOOR - wet_base) * soak_frac
    return np.clip(1023.0 - wet + rng.normal(0, NOISE["moisture"], n), 0, 1023)


def _derive_windows(s, win=16, label_fn=None):
    """Non-overlapping 8 s windows — mirrors the firmware's snapshot logic.

    label_fn(window_imu_peak_mg, default_label) -> label lets multi-phase
    scenarios label each window by its physical content: a debris scenario's
    strike windows are debris_impact, but its post-strike windows are plain
    flood_rise — teaching the model that debris = strike PRESENT IN WINDOW,
    not 'part of a debris-y stream'."""
    if label_fn is None:
        label_fn = lambda peak, base: base
    rows = []
    labels = []
    for w0 in range(0, len(s["moisture"]) - win + 1, win):
        sl = slice(w0, w0 + win)
        wet = 1023.0 - s["moisture"][sl]
        wet_mean = wet.mean()
        wet_slope = (wet[-1] - wet[0]) / WIN_S
        dist = s["distance_mm"][sl]
        rise = max(0.0, (dist[0] - dist[-1]) / WIN_S)
        imu_peak = s["imu_dev"][sl].max()
        rows.append([
            wet_mean, np.clip(wet_slope, -100, 100), dist.mean(), rise,
            s["temp_c"][sl].mean(), s["humidity"][sl].mean(), imu_peak,
            s["sat_rain"], s["fc_rain"],
        ])
        labels.append(label_fn(imu_peak, s["base_label"]))
    return rows, labels


def _windows_to_xy(rows_list, labels_list):
    X = np.concatenate([np.array(r) for r in rows_list], axis=0)
    y = np.concatenate([np.array(l, dtype=np.int64) for l in labels_list])
    order = RNG.permutation(len(X))
    return X[order], y[order]


def scenario_normal(rng, n, faults):
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0, 0.05), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(0, 5), faults.get("tof")),
        "imu_dev": _imu_stream(rng, n, faults.get("imu")),
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(22, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(45, 6, n), 5, 100),
        "sat_rain": rng.uniform(0, 0.15),
        "fc_rain": rng.uniform(0, 0.15),
        "base_label": 0,
    }
    return _derive_windows(s)


def scenario_dry_bump(rng, n, faults):
    """Violent motion, NO water: must stay normal (coincidence test)."""
    dev = _imu_stream(rng, n, faults.get("imu"))
    if not faults.get("imu"):
        dev = dev + _strike(rng, n, rng.uniform(2, 20), rng.uniform(800, 2500))
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0, 0.05), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(0, 5), faults.get("tof")),
        "imu_dev": dev,
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(22, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(45, 6, n), 5, 100),
        "sat_rain": rng.uniform(0, 0.2),
        "fc_rain": rng.uniform(0, 0.2),
        "base_label": 0,
    }
    return _derive_windows(s)


def scenario_rain_fallout(rng, n, faults):
    """Heavy rain OBSERVED (satellite), water not risen here yet, ground
    wetting. Fills the regime gap: sat high + no rise must NOT read as
    flood_rise. Labeled rain_approach (the WATCH state)."""
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0.1, 0.45), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(0, 12), faults.get("tof")),
        "imu_dev": _imu_stream(rng, n, faults.get("imu")),
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(20, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(85, 8, n), 5, 100),
        "sat_rain": rng.uniform(0.7, 1.0),
        "fc_rain": rng.uniform(0.0, 1.0),
        "base_label": 1,
    }
    return _derive_windows(s)


def scenario_rain_approach(rng, n, faults):
    """Forecast says heavy rain is COMING; observed still low; water steady."""
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0, 0.15), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(0, 10), faults.get("tof")),
        "imu_dev": _imu_stream(rng, n, faults.get("imu")),
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(21, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(72, 10, n), 5, 100),
        "sat_rain": rng.uniform(0, 0.3),
        "fc_rain": rng.uniform(0.6, 1.0),
        "base_label": 1,
    }
    return _derive_windows(s)


def scenario_flood_rise(rng, n, faults):
    """Observed rain high (it rained / is raining) AND water rising. Calm IMU."""
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0.3, 0.9), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(120, 500), faults.get("tof"),
                                   spray=rng.random() < 0.3),
        "imu_dev": _imu_stream(rng, n, faults.get("imu")),
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(20, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(88, 7, n), 5, 100),
        "sat_rain": rng.uniform(0.7, 1.0),
        "fc_rain": rng.uniform(0, 1.0),
        "base_label": 2,
    }
    return _derive_windows(s)


def scenario_debris_impact(rng, n, faults):
    """Flood rise + strike transient INSIDE the window + spray. The real thing."""
    dev = _imu_stream(rng, n, faults.get("imu"))
    if not faults.get("imu"):
        dev = dev + _strike(rng, n, rng.uniform(0.5, 6), rng.uniform(800, 2500))
    s = {
        "moisture": _moisture_stream(rng, n, rng.uniform(0.5, 1.0), faults.get("probe")),
        "distance_mm": _tof_stream(rng, n, rng.uniform(200, 600), faults.get("tof"), spray=True),
        "imu_dev": dev,
        "temp_c": np.full(n, 22.0) if faults.get("dht") else rng.normal(19, NOISE["temp_c"], n),
        "humidity": np.full(n, 45.0) if faults.get("dht") else np.clip(rng.normal(92, 6, n), 5, 100),
        "sat_rain": rng.uniform(0.85, 1.0),
        "fc_rain": rng.uniform(0.4, 1.0),
        "base_label": 3,
    }
    # Per-window truth: a window counts as debris_impact only if the strike
    # is INSIDE it (peak >= 700 mg); post-strike windows are plain flood_rise.
    # This teaches the coincidence rule the architecture demands. With a dead
    # IMU (peak 0) the whole stream is flood_rise — correct fault-invariance.
    return _derive_windows(s, label_fn=lambda peak, base: 3 if peak >= 700 else 2)


SCENARIOS = [scenario_normal, scenario_dry_bump, scenario_rain_approach,
             scenario_rain_fallout, scenario_flood_rise, scenario_debris_impact]

FAULT_KEYS = ["imu", "dht", "tof", "probe"]


FAULT_RATES = {"imu": 0.12, "dht": 0.30, "tof": 0.10, "probe": 0.10}


def make_dataset(scenarios_per_class=150, warmup=64, win=16,
                 fault_rounds=60):
    """Draw scenarios; inject per-channel faults at FAULT_RATES, plus
    balanced fault rounds: fault_rounds extra draws per scenario with a
    fixed channel fault so the model sees that fault with EVERY label
    (fault-invariance — prevents dead-DHT rows from skewing a class)."""
    X, y = [], []
    for fn in SCENARIOS:
        for round_idx in range(scenarios_per_class + fault_rounds):
            n = warmup + win * 8
            faults = {}
            if round_idx >= scenarios_per_class:
                # balanced rounds: cycle one fixed fault per draw
                faults = {FAULT_KEYS[(round_idx - scenarios_per_class) % 4]: True}
            else:
                for k in FAULT_KEYS:
                    if RNG.random() < FAULT_RATES[k]:
                        faults[k] = True
            rows, lab = fn(RNG, n, faults)
            X.append(np.array(rows))
            y.append(np.full(len(rows), lab, dtype=np.int64))
    X = np.concatenate(X, axis=0)
    y = np.concatenate(y)
    order = RNG.permutation(len(X))
    return X[order], y[order]


if __name__ == "__main__":
    X, y = make_dataset(scenarios_per_class=10)
    print(f"dataset: {X.shape[0]} samples x {X.shape[1]} features")
    for c, name in enumerate(CLASS_NAMES):
        n = (y == c).sum()
        print(f"  class {c} ({name}): {n} rows")
    for i, name in enumerate(FEATURE_ORDER):
        col = X[:, i]
        print(f"  {name:<20} min={col.min():9.2f} max={col.max():9.2f} mean={col.mean():9.2f}")
