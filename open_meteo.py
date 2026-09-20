"""HydroWatch — Open-Meteo real-time weather + short-term rain forecast.

Complements the NASA satellite layer: satellite data says what already
fell over the watershed; the forecast says what is about to fall.
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
OM = CONFIG["open_meteo"]
CACHE_MINUTES = OM.get("cache_minutes", 15)
TIMEOUT = OM.get("timeout_s", 15)
CACHE_FILE = Path(__file__).parent / ".open_meteo_cache.json"


def _fetch_json(url: str) -> dict:
    req = urlreq.Request(url, headers={"User-Agent": "HydroWatch/1.0 (open-source flood node)"})
    with urlreq.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def fetch_forecast(lat: float, lon: float, use_cache: bool = True) -> dict:
    """Return {"source", "rain_next_24h_mm", "rain_next_6h_mm", "hourly",
              "fetched_at", "cached", "error"}."""
    now = time.time()
    if use_cache and CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if now - cached.get("fetched_at", 0) < CACHE_MINUTES * 60:
                cached["cached"] = True
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "forecast_days": 2,
        "past_hours": 24,
    }
    url = f"{OM['base_url']}?{urlencode(params)}"
    try:
        data = _fetch_json(url)
        series = data["hourly"]["precipitation"]  # mm per hour, past 24h + next 48h
        next6 = sum(v for v in series[24:30] if isinstance(v, (int, float)))
        next24 = sum(v for v in series[24:48] if isinstance(v, (int, float)))
        result = {
            "source": "Open-Meteo forecast",
            "rain_next_6h_mm": round(next6, 2),
            "rain_next_24h_mm": round(next24, 2),
            "hourly": series,
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
        if CACHE_FILE.exists():
            try:
                stale = json.loads(CACHE_FILE.read_text())
                stale["cached"] = True
                stale["error"] = f"fetch failed ({e}); using stale cache"
                return stale
            except (json.JSONDecodeError, OSError):
                pass
        return {"source": OM["base_url"], "rain_next_6h_mm": None, "rain_next_24h_mm": None,
                "hourly": [], "fetched_at": now, "cached": False, "error": str(e)}


if __name__ == "__main__":
    loc = CONFIG["node"]["location"]
    print(json.dumps(fetch_forecast(loc["lat"], loc["lon"]), indent=2))
