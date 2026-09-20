"""HydroWatch — GPM IMERG Early-run half-hourly precipitation ingestion.

Replaces the POWER daily confirmation channel with satellite rain at
half-hourly resolution. The Early run publishes each 30-min granule about
4 hours after observation time — still ~20x fresher than POWER daily,
which lags by 1-3 days.

One-time setup (free):
  1. Create an account at https://urs.earthdata.nasa.gov
  2. Put the credentials in imerg_creds.json next to this file:
         {"user": "your-username", "pass": "your-password"}
     (git-ignored; alternatively curl's _netrc also works)

Design:
  - latest available granule = now - latency, truncated to a 30-min slot
  - granules are netCDF4, downloaded with curl (handles the Earthdata
    redirect/auth dance) into a small rolling cache dir
  - only the node's single 0.1 deg pixel is read with h5py — no heavy
    geo stack needed
  - sat = min(1, mean_rate_over_window / SAT_ANCHOR_MM_H). The anchor
    mirrors the trained regime gap in sensor_sim.py (normal <= 0.15,
    rainy regimes >= 0.7): drizzle ~0.5 mm/h must read "dry", moderate
    rain ~4.5 mm/h must read "raining".
  - every failure degrades gracefully; server.py falls back to POWER.
"""

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
IMERG = CONFIG["imerg"]
LAT = CONFIG["node"]["location"]["lat"]
LON = CONFIG["node"]["location"]["lon"]

BASE_URL = IMERG["base_url"]              # PPS early-run directory
VERSIONS = IMERG["versions"]              # tried newest first, e.g. V08A, V07B
LATENCY_MIN = IMERG["granule_latency_min"]
WINDOW_H = IMERG["window_h"]
SAT_ANCHOR_MM_H = IMERG["sat_anchor_mm_h"]
TIMEOUT_S = IMERG["timeout_s"]
CACHE_DIR = Path(__file__).parent / IMERG["cache_dir"]
CREDS_FILE = Path(__file__).parent / "imerg_creds.json"
MAX_CACHED = 16

# IMERG 0.1 deg grid: centers at -179.95..179.95 (lon), -89.95..89.95 (lat)
def _grid_index() -> tuple[int, int]:
    ilon = int(round((LON + 179.95) / 0.1))
    ilat = int(round((LAT + 89.95) / 0.1))
    return max(0, min(3599, ilon)), max(0, min(1799, ilat))

_ILON, _ILAT = _grid_index()


def _creds() -> tuple[str, str] | None:
    try:
        c = json.loads(CREDS_FILE.read_text())
        if c.get("user") and c.get("pass"):
            return str(c["user"]), str(c["pass"])
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _slot_utc(now: datetime | None = None, steps_back: int = 0) -> datetime:
    """UTC slot boundary: now - latency, truncated to 30 min, minus steps."""
    now = now or datetime.now(timezone.utc)
    avail = now - timedelta(minutes=LATENCY_MIN)
    slot = avail.replace(minute=(avail.minute // 30) * 30, second=0, microsecond=0)
    return slot - timedelta(minutes=30 * steps_back)


def _granule_name(slot: datetime, version: str) -> str:
    start = slot.strftime("%H%M%S")
    end = (slot + timedelta(minutes=29, seconds=59)).strftime("%H%M%S")
    mins = slot.hour * 60 + slot.minute
    return (f"3B-HHR-E.MS.MRG.3IMERG.{slot:%Y%m%d}-S{start}-E{end}."
            f"{mins:03d}.{version}.nc4")


def _candidate_urls(slot: datetime) -> list[str]:
    """URLs to try in order (directory-layout variants, then version)."""
    names = []
    for v in VERSIONS:
        names.append(f"{BASE_URL}/{slot:%Y}/{slot:%m}/{slot:%d}/{_granule_name(slot, v)}")
    for v in VERSIONS:
        names.append(f"{BASE_URL}/{slot:%Y}/{slot:%j}/{_granule_name(slot, v)}")
    for v in VERSIONS:
        names.append(f"{BASE_URL}/{slot:%Y-%m}/{_granule_name(slot, v)}")
    return names


def _cache_path(slot: datetime) -> Path:
    return CACHE_DIR / f"imerg_{slot:%Y%m%d_%H%M}.nc4"


def _download(url: str, dest: Path) -> bool:
    creds = _creds()
    CACHE_DIR.mkdir(exist_ok=True)
    jar = str(CACHE_DIR / ".cookies")
    cmd = ["curl", "-sS", "-L", "--max-time", str(TIMEOUT_S), "-o", str(dest)]
    if creds:
        cmd += ["-u", f"{creds[0]}:{creds[1]}"]
    cmd += ["-b", jar, "-c", jar, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S + 15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 100_000:
        dest.unlink(missing_ok=True)
        return False
    return True


def _read_pixel(path: Path) -> float | None:
    """Read the node's pixel (mm/h) from a granule, regardless of axis order."""
    try:
        import h5py
        with h5py.File(path, "r") as f:
            if "precipitation" not in f:
                return None
            arr = f["precipitation"]
            shape = arr.shape          # (time, lon, lat) or (time, lat, lon)
            if shape[1] == 3600:
                val = arr[0, _ILON, _ILAT]
            else:
                val = arr[0, _ILAT, _ILON]
            val = float(val)
            return val if 0.0 <= val < 1000.0 else None   # -9999 fill etc.
    except (OSError, KeyError, IndexError, ValueError, ImportError):
        return None


def _prune() -> None:
    files = sorted(CACHE_DIR.glob("imerg_*.nc4"))
    for old in files[:-MAX_CACHED]:
        old.unlink(missing_ok=True)


def fetch_latest(max_back: int = 4) -> dict:
    """Ensure the newest available granule (and window predecessors) are cached."""
    if _creds() is None:
        return {"ok": False, "error": "no Earthdata credentials (imerg_creds.json)"}
    newest = None
    for back in range(max_back + 1):
        slot = _slot_utc(steps_back=back)
        dest = _cache_path(slot)
        if dest.exists() and dest.stat().st_size > 100_000:
            newest = slot
            break
        for url in _candidate_urls(slot):
            if _download(url, dest):
                newest = slot
                break
        if newest is not None:
            break
    if newest is None:
        return {"ok": False, "error": "no granule downloadable (auth/path/latency)"}
    _prune()
    return {"ok": True, "newest": newest.strftime("%Y-%m-%d %H:%M UTC")}


def current_sat() -> dict | None:
    """sat from the trailing window, or None if IMERG is unavailable/stale.

    Freshness rule: the newest cached granule must be within 8 h of the
    latest available slot, and at least 2 window granules must exist."""
    slots = [_slot_utc(steps_back=b) for b in range(WINDOW_H * 2)]
    rates = []
    for slot in slots:
        p = _cache_path(slot)
        if p.exists():
            v = _read_pixel(p)
            if v is not None:
                rates.append(v)
    if len(rates) < 2:
        return None
    # Freshness: the newest cached granule must be within 8 h of the latest
    # available slot — older than that, POWER fallback is more honest.
    newest_data_slot = None
    for slot in slots:
        if _cache_path(slot).exists():
            newest_data_slot = slot
            break
    if newest_data_slot is None or (_slot_utc() - newest_data_slot) > timedelta(hours=8):
        return None
    mean_rate = sum(rates) / len(rates)
    mm_1h = sum(rates[:2]) / 2          # two slots = most recent hour
    sat = round(min(1.0, max(0.0, mean_rate / SAT_ANCHOR_MM_H)), 3)
    return {
        "sat": sat,
        "mm_1h": round(mm_1h, 2),
        "mm_window": round(mean_rate * WINDOW_H, 2),
        "obs_hhmm": newest_data_slot.strftime("%H:%M") if newest_data_slot else None,
        "obs_date": newest_data_slot.strftime("%Y%m%d") if newest_data_slot else None,
        "pixel": f"({LON:.2f},{LAT:.2f}) cell 0.1deg",
    }


def status() -> dict:
    creds = _creds() is not None
    sat = current_sat()
    return {
        "credentials": creds,
        "active": sat is not None,
        "grid_index": [_ILON, _ILAT],
        "window_h": WINDOW_H,
        "anchor_mm_h": SAT_ANCHOR_MM_H,
        **({"sat": sat["sat"], "mm_1h": sat["mm_1h"], "obs_hhmm": sat["obs_hhmm"]} if sat else {}),
    }
