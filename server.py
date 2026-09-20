#!/usr/bin/env python3
"""HydroWatch flood-prediction node — main server.

Fuses the UNO Q ground-truth stream (distance to water, soil moisture,
DHT11 air data, IMU) with NASA POWER satellite precipitation and an
Open-Meteo short-term forecast, and exposes everything to the dashboard
over HTTP + Server-Sent Events.

Endpoints:
  GET /              -> dashboard
  GET /events        -> SSE: {"type": "sample"|"status"|"alert", ...}
  GET /api/status    -> full node status (sensors, engine, weather)
  GET /api/alerts    -> alert log
  POST /api/baseline -> {"distance_mm": int}  (or {"auto": true})
  GET /health        -> liveness
"""

import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial

from alert_engine import AlertEngine
import imerg
import nasa_precip
import open_meteo

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
SERIAL_CFG = CONFIG["serial"]
HTTP_PORT = CONFIG["http"]["port"]
TH = CONFIG["thresholds"]
COOLDOWN = CONFIG["alert_cooldown_s"]
STATIC_DIR = Path(__file__).parent / "static"

LINE_RE = re.compile(
    r"moisture=(-?\d+)\s*\|\s*distance_mm=(-?\d+)"
    r"(?:\s*\|\s*humidity=(-?\d+))?"
    r"(?:\s*\|\s*temp_c=(-?\d+))?"
    r"(?:\s*\|\s*ax=(-?\d+))?"
    r"(?:\s*\|\s*ay=(-?\d+))?"
    r"(?:\s*\|\s*az=(-?\d+))?"
    r"(?:\s*\|\s*p_flood=(-?\d+(?:\.\d+)?))?"
    r"(?:\s*\|\s*mcls=(-?\d+))?"
    r"(?:\s*\|\s*imu_peak=(-?\d+))?"
    r"(?:\s*\|\s*buzz=(on|off))?"
)

latest = {
    "moisture": None, "distance_mm": None, "humidity": None, "temp_c": None,
    "ax": None, "ay": None, "az": None, "t": None,
    "p_flood": None, "mcls": None, "imu_peak": None, "buzz": None,
}
serial_ok = False
ser_port = None          # shared handle so sky_writer can talk to the MCU
ser_write_lock = threading.Lock()
subscribers = set()
subs_lock = threading.Lock()

loc = CONFIG["node"]["location"]
engine = AlertEngine(TH, COOLDOWN)
weather = {"nasa": None, "forecast": None}

# ---- Manual weather override (for offline deployment: no WiFi) ----
MANUAL_SKY_FILE = Path(__file__).parent / "manual_sky.json"
MANUAL_TTL_H = CONFIG.get("manual_sky_ttl_h", 24)


def manual_sky() -> dict | None:
    """Active manual weather override, or None (absent or expired)."""
    try:
        m = json.loads(MANUAL_SKY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - m.get("set_at", 0) > MANUAL_TTL_H * 3600:
        return None
    return m


def effective_weather() -> tuple[dict, dict]:
    """(nasa, forecast) dicts as the engine + sky should see them: the
    manual override wins while active, auto layers otherwise."""
    m = manual_sky()
    if m:
        obs = m.get("observed_mm_day") or 0.0
        today = time.strftime("%Y%m%d")
        return (
            {"last_7d_mm": obs, "days": {today: obs}},
            {"rain_next_6h_mm": m.get("forecast_mm_6h") or 0.0,
             "rain_next_24h_mm": m.get("forecast_mm_24h") or 0.0},
        )
    return weather["nasa"] or {}, weather["forecast"] or {}


def broadcast(msg: dict) -> None:
    with subs_lock:
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            subscribers.discard(q)


def serial_reader() -> None:
    global serial_ok, ser_port
    buf = b""
    while True:
        try:
            with serial.Serial(SERIAL_CFG["port"], SERIAL_CFG["baud"], timeout=1) as ser:
                with ser_write_lock:
                    ser_port = ser
                serial_ok = True
                print(f"[serial] connected to {SERIAL_CFG['port']} @ {SERIAL_CFG['baud']}")
                while True:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        m = LINE_RE.search(raw.decode("ascii", "ignore"))
                        if not m:
                            continue
                        sample = {
                            "moisture": int(m.group(1)),
                            "distance_mm": int(m.group(2)),
                            "humidity": int(m.group(3)) if m.group(3) is not None else None,
                            "temp_c": int(m.group(4)) if m.group(4) is not None else None,
                            "ax": int(m.group(5)) if m.group(5) is not None else None,
                            "ay": int(m.group(6)) if m.group(6) is not None else None,
                            "az": int(m.group(7)) if m.group(7) is not None else None,
                            "p_flood": float(m.group(8)) if m.group(8) is not None else None,
                            "mcls": int(m.group(9)) if m.group(9) is not None else None,
                            "imu_peak": int(m.group(10)) if m.group(10) is not None else None,
                            "buzz": (m.group(11) == "on") if m.group(11) is not None else None,
                            "t": time.time(),
                        }
                        latest.update(sample)
                        nasa_eff, fc_eff = effective_weather()
                        try:
                            engine.ingest(sample, nasa_eff, fc_eff)
                        except Exception as e:
                            # a bug in the engine must never kill the data stream
                            print(f"[engine] ingest error: {e}")
                        broadcast({"type": "sample", **sample, **engine_status_public()})
        except (serial.SerialException, OSError) as e:
            serial_ok = False
            ser_port = None
            print(f"[serial] {SERIAL_CFG['port']} unavailable ({e}); retrying in 3 s")
            time.sleep(3)


def engine_status_public() -> dict:
    st = engine.status()
    return {
        "alert_level": st["level"],
        "alert_reasons": st["reasons"],
        "water_level_mm": st["water_level_mm"],
        "rise_rate_mm_s": st["rise_rate_mm_s"],
        "sky": latest.get("sky"),
    }


# Normalization anchors — must mirror sensor_sim.py's rain feature scaling
HEAVY_DAY_MM = 15.0   # observed satellite rain: 15 mm/day -> sat = 1.0
HEAVY_6H_MM = 10.0    # forecast rain: 10 mm in 6 h -> fc = 1.0


def compute_sky() -> dict:
    """Normalize the weather channels into the 0-1 features the MCU model
    was trained on: sat = observed (confirmation), fc = forecast (lead time).

    Preferred: GPM IMERG Early half-hourly (~4 h latency). Fallback: NASA
    POWER daily (1-3 day publication lag)."""
    f = weather["forecast"] or {}
    n6 = f.get("rain_next_6h_mm") or 0.0
    fc = round(min(1.0, max(0.0, float(n6) / HEAVY_6H_MM)), 3)

    # Manual override (offline deployment) beats every auto layer
    m = manual_sky()
    if m:
        obs = float(m.get("observed_mm_day") or 0.0)
        f6 = float(m.get("forecast_mm_6h") or 0.0)
        f24 = float(m.get("forecast_mm_24h") or 0.0)
        return {
            "sat": round(min(1.0, max(0.0, obs / HEAVY_DAY_MM)), 3),
            "fc": round(min(1.0, max(0.0, max(f6 / HEAVY_6H_MM, f24 / (HEAVY_6H_MM * 4)))), 3),
            "sat_src": "MANUAL",
            "sat_time": time.strftime("%H:%M", time.localtime(m.get("set_at", time.time()))),
            "sat_asof": None,
        }

    im = imerg.current_sat()
    if im is not None:
        return {
            "sat": im["sat"],
            "fc": fc,
            "sat_src": "IMERG 30-min",
            "sat_time": im["obs_hhmm"],
            "sat_asof": None,
        }

    n = weather["nasa"] or {}
    days = n.get("days") or {}
    recent = 0.0
    asof = None
    # POWER daily lags: the newest keys can be fill values (-999 = no data
    # published yet). Read the most recent day that actually has data —
    # otherwise the confirmation channel silently reports "dry" during
    # every publication lag.
    for key in sorted(days.keys(), reverse=True):
        v = days.get(key)
        if isinstance(v, (int, float)) and v >= 0:
            recent = float(v)
            asof = key
            break
    return {
        "sat": round(min(1.0, max(0.0, recent / HEAVY_DAY_MM)), 3),
        "fc": fc,
        "sat_src": "POWER daily",
        "sat_time": None,
        "sat_asof": asof,   # YYYYMMDD of the observation sat was derived from
    }


def sky_writer() -> None:
    """Push normalized rain features down the serial line to the MCU
    every 60 s (also re-primes a freshly rebooted board)."""
    while True:
        sky = compute_sky()
        latest["sky"] = sky
        ser = ser_port
        if ser is not None:
            try:
                with ser_write_lock:
                    ser.write(f"SKY sat={sky['sat']} fc={sky['fc']}\n".encode())
            except (serial.SerialException, OSError):
                pass
        time.sleep(60)


def weather_loop() -> None:
    """Refresh satellite + forecast layers on their own cadence."""
    while True:
        weather["nasa"] = nasa_precip.fetch_nasa_precip(loc["lat"], loc["lon"])
        weather["forecast"] = open_meteo.fetch_forecast(loc["lat"], loc["lon"])
        try:
            r = imerg.fetch_latest()
            if not r.get("ok"):
                print(f"[imerg] {r.get('error')}")
        except Exception as e:   # never break the weather loop
            print(f"[imerg] fetch failed: {e}")
        broadcast({"type": "status", "weather": weather_public(), **engine_status_public()})
        time.sleep(300)  # NASA cache 6 h, forecast cache 15 min — poll cheaply


def weather_public() -> dict:
    n = weather["nasa"] or {}
    f = weather["forecast"] or {}
    return {
        "nasa": {
            "last_7d_mm": n.get("last_7d_mm"),
            "days": n.get("days", {}),
            "cached": n.get("cached", False),
            "error": n.get("error"),
            "source": n.get("source"),
        },
        "forecast": {
            "rain_next_6h_mm": f.get("rain_next_6h_mm"),
            "rain_next_24h_mm": f.get("rain_next_24h_mm"),
            "cached": f.get("cached", False),
            "error": f.get("error"),
            "source": f.get("source"),
        },
        "imerg": imerg.status(),
        "manual": manual_sky(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/events":
            self.handle_events()
        elif self.path == "/api/status":
            body = json.dumps({
                "node": {**CONFIG["node"], "serial_ok": serial_ok},
                "sensors": latest,
                "engine": engine.status(),
                "weather": weather_public(),
            }).encode()
            self.send_json(body)
        elif self.path == "/api/alerts":
            body = json.dumps({"alerts": engine.status()["alerts"]}).encode()
            self.send_json(body)
        elif self.path == "/health":
            self.send_json(json.dumps({"ok": True}).encode())
        elif self.path in ("/", "/index.html"):
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self.serve_file(STATIC_DIR / "app.js", "application/javascript")
        elif self.path == "/style.css":
            self.serve_file(STATIC_DIR / "style.css", "text/css")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/baseline":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                self.send_json(json.dumps({"error": "bad json"}).encode(), 400)
                return
            if payload.get("auto") and latest["distance_mm"]:
                result = engine.set_baseline(float(latest["distance_mm"]))
            elif "distance_mm" in payload:
                result = engine.set_baseline(float(payload["distance_mm"]))
            else:
                self.send_json(json.dumps({"error": "need distance_mm or auto"}).encode(), 400)
                return
            broadcast({"type": "status", "weather": weather_public(), **engine_status_public()})
            self.send_json(json.dumps(result).encode())
        elif self.path == "/api/manual-sky":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, ValueError):
                self.send_json(json.dumps({"error": "bad json"}).encode(), 400)
                return

            def num(key: str) -> float:
                try:
                    return max(0.0, float(payload.get(key) or 0))
                except (TypeError, ValueError):
                    return 0.0

            entry = {
                "observed_mm_day": num("observed_mm_day"),
                "forecast_mm_6h": num("forecast_mm_6h"),
                "forecast_mm_24h": num("forecast_mm_24h"),
                "set_at": time.time(),
            }
            MANUAL_SKY_FILE.write_text(json.dumps(entry, indent=2))
            broadcast({"type": "status", "weather": weather_public(), **engine_status_public()})
            self.send_json(json.dumps({"ok": True, **entry}).encode())
        elif self.path == "/api/manual-sky/clear":
            MANUAL_SKY_FILE.unlink(missing_ok=True)
            broadcast({"type": "status", "weather": weather_public(), **engine_status_public()})
            self.send_json(json.dumps({"ok": True, "cleared": True}).encode())
        elif self.path == "/api/buzz-test":
            ser = ser_port
            if ser is None:
                self.send_json(json.dumps({"error": "serial not connected"}).encode(), 503)
                return
            try:
                with ser_write_lock:
                    ser.write(b"BUZZ TEST\n")
                self.send_json(json.dumps({"ok": True, "sent": "BUZZ TEST"}).encode())
            except (serial.SerialException, OSError) as e:
                self.send_json(json.dumps({"error": str(e)}).encode(), 500)
        else:
            self.send_error(404)

    def handle_events(self) -> None:
        q: queue.Queue = queue.Queue(maxsize=200)
        with subs_lock:
            subscribers.add(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            hello = {"type": "hello", **engine_status_public(), "weather": weather_public()}
            self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with subs_lock:
                subscribers.discard(q)

    def send_json(self, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=serial_reader, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=sky_writer, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    print(f"[http] HydroWatch node at http://127.0.0.1:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
