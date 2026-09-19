"""HydroWatch — NASA GPM/POWER satellite precipitation ingestion.

Queries NASA POWER (a GPM-IMPG-aligned daily satellite precipitation
product) for a node's coordinates, with on-disk caching so the node
stays polite to the API and keeps working through network outages.
"""

import json
import ssl
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

import certifi

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
NASA = CONFIG["nasa_power"]
BASE = NASA["base_url"]  # e.g. https://power.larc.nasa.gov/api/temporal
CACHE_HOURS = NASA.get("cache_hours", 6)
TIMEOUT = NASA.get("timeout_s", 20)
CACHE_FILE = Path(__file__).parent / ".nasa_precip_cache.json"


def _last_n_days_yyyymmdd(n: int):
    """Return (start, end) as YYYYMMDD strings covering the last n days."""
    import datetime as dt
    end = dt.date.today()
    start = end - dt.timedelta(days=n)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _fetch_json(url: str) -> dict:
    req = urlreq.Request(url, headers={"User-Agent": "HydroWatch/1.0 (open-source flood node)"})
    with urlreq.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def fetch_nasa_precip(lat: float, lon: float, use_cache: bool = True) -> dict:
    """Return {"source", "lat", "lon", "last_7d_mm", "days", "fetched_at",
              "cached", "error"} for the given coordinates."""
    now = time.time()
    if use_cache and CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if now - cached.get("fetched_at", 0) < CACHE_HOURS * 3600:
                cached["cached"] = True
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    # NASA POWER daily: PRECTOTCORR over the last 7 days at this point
    start, end = _last_n_days_yyyymmdd(8)
    params = {
        "parameters": "PRECTOTCORR",
        "community": "AG",
        "longitude": f"{lon}",
        "latitude": f"{lat}",
        "start": start,
        "end": end,
        "format": "JSON",
    }
    url = f"{BASE}/daily/point?{urlencode(params)}"
    try:
        data = _fetch_json(url)
        prop = data.get("properties", {}).get("parameter", {}).get("PRECTOTCORR", {})
        # {"20260901": 12.3, ...} — daily mm, last values are most recent
        days = sorted(prop.keys())[-7:]
        last7 = [prop[d] for d in days]
        valid = [v for v in last7 if isinstance(v, (int, float)) and v >= 0]
        result = {
            "source": "NASA POWER (GPM IMPG) daily precipitation",
            "lat": lat,
            "lon": lon,
            "last_7d_mm": round(sum(valid), 2),
            "days": dict(zip(days, last7)),
            "fetched_at": now,
            "cached": False,
            "error": None,
        }
        try:
            CACHE_FILE.write_text(json.dumps(result))
        except OSError:
            pass
        return result
    except (URLError, HTTPError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
        # Stale cache is better than nothing during a network outage
        if CACHE_FILE.exists():
            try:
                stale = json.loads(CACHE_FILE.read_text())
                stale["cached"] = True
                stale["error"] = f"fetch failed ({e}); using stale cache"
                return stale
            except (json.JSONDecodeError, OSError):
                pass
        return {"source": NASA["base_url"], "lat": lat, "lon": lon, "last_7d_mm": None,
                "days": {}, "fetched_at": now, "cached": False, "error": str(e)}


if __name__ == "__main__":
    loc = CONFIG["node"]["location"]
    print(json.dumps(fetch_nasa_precip(loc["lat"], loc["lon"]), indent=2))
