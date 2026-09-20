"""HydroWatch — alert engine.

Fuses the local ground-truth stream (distance to water, soil moisture,
IMU) with the satellite/forecast weather layers into typed alert levels.

Fusion logic (both signals must agree, per the project description):
  - WATCH    = satellite/forecast says heavy rain is coming or fell
  - WARNING  = physical water rise observed, but sky is quiet
  - CRITICAL (flood alert) = water rising fast AND heavy precipitation
               observed/forecast — automatically "verified emergency"
"""

import time


class AlertEngine:
    def __init__(self, thresholds: dict, cooldown_s: int = 60):
        self.t = thresholds
        self.cooldown_s = cooldown_s
        self.baseline_mm = None          # "empty streambed" reference distance
        self.water_level_mm = 0.0        # water column height above baseline
        self.rise_rate_mm_s = 0.0
        self._level = "NORMAL"
        self._reasons = []
        self._last_alert_at = 0.0
        self._alerts = []                # alert log (newest first)
        self._history = []               # (t, water_level_mm) for rise-rate

    # ---------- ground station side ----------
    def set_baseline(self, mm: float) -> dict:
        self.baseline_mm = mm
        return {"baseline_mm": self.baseline_mm}

    def _water_level(self, distance_mm: float) -> float:
        """Water column = baseline distance minus current distance."""
        if self.baseline_mm is None:
            return 0.0
        return max(0.0, self.baseline_mm - distance_mm)

    def _update_rise_rate(self, t: float, level_mm: float) -> None:
        self._history.append((t, level_mm))
        # keep ~2 minutes of samples
        cutoff = t - 120
        while len(self._history) > 2 and self._history[0][0] < cutoff:
            self._history.pop(0)
        old_t, old_v = self._history[0]
        dt = t - old_t
        if dt >= 1.0:
            self.rise_rate_mm_s = (level_mm - old_v) / dt

    def ingest(self, sample: dict, nasa: dict, forecast: dict) -> None:
        t = sample.get("t") or time.time()
        dist = sample.get("distance_mm")
        if dist is not None and dist > 0:
            self.water_level_mm = self._water_level(float(dist))
            self._update_rise_rate(t, self.water_level_mm)

        th = self.t
        rain_window = (nasa.get("last_7d_mm") or 0.0)
        rain_24h = (forecast.get("rain_next_24h_mm") or 0.0)
        rain_6h = (forecast.get("rain_next_6h_mm") or 0.0)

        sky_wet = (
            rain_24h >= th["forecast_rain_24h_mm"]
            or rain_6h >= th["forecast_rain_24h_mm"] / 4
            or rain_window >= th["heavy_rain_mm_per_day"] * th["rains_days_window"] / 2
        )

        rising_fast = (
            self.baseline_mm is not None
            and self.rise_rate_mm_s >= th["rise_rate_alert_mm_per_s"]
        )
        water_present = (
            sample.get("moisture") is not None
            and sample["moisture"] <= th["moisture_wet_mm"]
        )
        object_close = dist is not None and 0 < dist <= th["immo_dist_alert_mm"]

        reasons = []
        level = "NORMAL"

        if rising_fast and sky_wet:
            level = "CRITICAL"
            reasons.append(
                f"water rising {self.rise_rate_mm_s:.1f} mm/s AND heavy precipitation signal"
            )
        elif rising_fast:
            level = "WARNING"
            reasons.append(f"rapid water rise {self.rise_rate_mm_s:.1f} mm/s (sky quiet)")
        elif sky_wet:
            level = "WATCH"
            reasons.append("satellite/forecast precipitation above threshold")

        if water_present:
            reasons.append("soil moisture indicates water contact")
        if object_close:
            reasons.append(f"water surface {dist:.0f} mm from sensor")

        if level != self._level:
            self._last_alert_at = t
            if level != "NORMAL":
                self._alerts.insert(0, {
                    "level": level,
                    "reasons": reasons,
                    "water_level_mm": round(self.water_level_mm, 1),
                    "rise_rate_mm_s": round(self.rise_rate_mm_s, 2),
                    "rain_24h_mm": rain_24h,
                    "rain_7d_mm": rain_window,
                    "t": t,
                })
                self._alerts = self._alerts[:100]
        elif level != "NORMAL" and t - self._last_alert_at > self.cooldown_s:
            # refresh the top alert with current numbers
            self._alerts[0].update({
                "reasons": reasons,
                "water_level_mm": round(self.water_level_mm, 1),
                "rise_rate_mm_s": round(self.rise_rate_mm_s, 2),
                "t": t,
            })
            self._last_alert_at = t

        self._level = level
        self._reasons = reasons

    # ---------- status side ----------
    def status(self) -> dict:
        return {
            "level": self._level,
            "reasons": self._reasons,
            "baseline_mm": self.baseline_mm,
            "water_level_mm": round(self.water_level_mm, 1),
            "rise_rate_mm_s": round(self.rise_rate_mm_s, 2),
            "alerts": self._alerts,
        }
