/* HydroWatch flood node dashboard: SSE client + fusion display */
(() => {
  "use strict";

  const WINDOW_MS = 60000;
  const MAX_POINTS = 600;

  const el = (id) => document.getElementById(id);
  const statusEl = el("status");
  const statusText = el("status-text");
  const banner = el("alert-banner");
  const bannerLevel = el("alert-banner-level");
  const bannerText = el("alert-banner-text");
  const alertLog = el("alert-log");

  const moistureVal = el("moisture");
  const distanceVal = el("distance");
  const humidityVal = el("humidity");
  const tempVal = el("temp");
  const waterVal = el("water-level");
  const riseRateEl = el("rise-rate");
  const baselineEl = el("baseline");
  const axVal = el("ax"), ayVal = el("ay"), azVal = el("az");
  const nasa7dEl = el("nasa-7d");
  const nasaDetail = el("nasa-detail");
  const fc24El = el("fc-24h");
  const fc6hEl = el("fc-6h");
  const manualStatus = el("manual-status");
  const countEl = el("count");
  const lastUpdateEl = el("last-update");
  const moistureBar = el("moisture-bar");
  const distanceBar = el("distance-bar");
  const pFloodEl = el("p-flood");
  const mclsEl = el("mcls");
  const buzzEl = el("buzz");
  const imuPeakEl = el("imu-peak");
  const mlBar = el("ml-bar");

  const moisturePoints = [];
  const distancePoints = [];
  const humidityPoints = [];
  const tempPoints = [];
  const motionPoints = [];
  const waterPoints = [];
  const mlPoints = [];

  let sampleCount = 0;
  let currentLevel = "NORMAL";
  let currentReasons = [];
  let lastAlertT = 0; // newest alert timestamp we've rendered
  let lastSkySource = null;

  function setStatus(cls, text) {
    statusEl.className = "status " + cls;
    statusText.textContent = text;
  }

  function pushPoint(arr, t, v) {
    if (!isFinite(v)) return; // never chart NaN/Infinity
    arr.push({ t, v });
    if (arr.length > MAX_POINTS) arr.shift();
    const cutoff = t - WINDOW_MS;
    while (arr.length > 1 && arr[0].t < cutoff) arr.shift();
  }

  // ---------- banner + log ----------
  const LEVEL_ORDER = { NORMAL: 0, WATCH: 1, WARNING: 2, CRITICAL: 3 };

  function renderBanner(level, reasons) {
    if (level === "NORMAL") {
      banner.className = "banner hidden level-NORMAL";
      return;
    }
    banner.className = "banner level-" + level;
    bannerLevel.textContent = level + ":";
    bannerText.textContent = reasons.join(" · ") || "conditions developing";
  }

  function renderAlertLog(alerts) {
    if (!alerts || !alerts.length) return;
    alertLog.innerHTML = "";
    for (const a of alerts.slice(0, 20)) {
      const div = document.createElement("div");
      div.className = "alert-entry lv-" + a.level;
      const when = new Date(a.t * 1000).toLocaleString();
      div.innerHTML =
        `<strong>${a.level}</strong> — water ${a.water_level_mm} mm, ` +
        `rise ${a.rise_rate_mm_s} mm/s, 24h rain ${a.rain_24h_mm ?? "–"} mm` +
        `<div class="when">${when} · ${a.reasons.join(" · ")}</div>`;
      alertLog.appendChild(div);
    }
  }

  // ---------- charts ----------
  function drawChart(canvas, points, opts) {
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const padL = 52, padR = 12, padT = 10, padB = 22;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const now = performance.timeOrigin + performance.now();
    const t0 = now - WINDOW_MS;

    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = "#30363d";
    ctx.lineWidth = 1;
    ctx.strokeRect(padL, padT, plotW, plotH);

    let lo = opts.fixedMin, hi = opts.fixedMax;
    if (lo === undefined || !isFinite(hi)) {
      lo = Infinity; hi = -Infinity;
      for (const p of points) { if (p.v < lo) lo = p.v; if (p.v > hi) hi = p.v; }
      if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
      if (hi - lo < 10) { const m = (hi + lo) / 2; lo = m - 5; hi = m + 5; }
      const pad = (hi - lo) * 0.1;
      lo -= pad; hi += pad;
      if (opts.fixedMin !== undefined) lo = opts.fixedMin;
    }

    ctx.fillStyle = "#8b949e";
    ctx.font = "11px 'Segoe UI', sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const val = lo + ((hi - lo) * i) / 4;
      const y = padT + plotH - (plotH * i) / 4;
      ctx.strokeStyle = "#21262d";
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(opts.format(val), padL - 6, y);
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i <= 3; i++) {
      const secAgo = Math.round((WINDOW_MS / 1000) * (3 - i) / 3);
      const x = padL + (plotW * i) / 3;
      ctx.fillText(secAgo + "s ago", x, padT + plotH + 6);
    }

    ctx.strokeStyle = opts.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const p of points) {
      const x = padL + ((p.t - t0) / WINDOW_MS) * plotW;
      const y = padT + plotH - ((p.v - lo) / (hi - lo)) * plotH;
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    if (points.length) {
      const last = points[points.length - 1];
      const x = padL + ((last.t - t0) / WINDOW_MS) * plotW;
      const y = padT + plotH - ((last.v - lo) / (hi - lo)) * plotH;
      ctx.fillStyle = opts.color;
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function render() {
    drawChart(el("water-chart"), waterPoints, {
      color: "#bc8cff",
      fixedMin: 0,
      format: (v) => Math.round(v) + "mm",
    });
    drawChart(el("distance-chart"), distancePoints, {
      color: "#d29922",
      format: (v) => Math.round(v) + "mm",
    });
    drawChart(el("moisture-chart"), moisturePoints, {
      color: "#58a6ff",
      fixedMin: 0,
      fixedMax: 1023,
      format: (v) => Math.round(v),
    });
    drawChart(el("motion-chart"), motionPoints, {
      color: "#58a6ff",
      fixedMin: 0,
      fixedMax: 2000,
      format: (v) => Math.round(v) + "mg",
    });
    drawChart(el("humidity-chart"), humidityPoints, {
      color: "#3fb950",
      fixedMin: 0,
      fixedMax: 100,
      format: (v) => Math.round(v) + "%",
    });
    drawChart(el("temp-chart"), tempPoints, {
      color: "#f85149",
      format: (v) => Math.round(v) + "°C",
    });
    drawChart(el("ml-chart"), mlPoints, {
      color: "#f778ba",
      fixedMin: 0,
      fixedMax: 100,
      format: (v) => Math.round(v) + "%",
    });
  }
  setInterval(render, 250);

  // ---------- message handling ----------
  function handleEngineState(d) {
    if (d.alert_level === undefined) return;
    currentLevel = d.alert_level;
    currentReasons = d.alert_reasons || [];
    renderBanner(currentLevel, currentReasons);

    if (d.water_level_mm !== undefined && d.water_level_mm !== null) {
      waterVal.textContent = d.water_level_mm;
      pushPoint(waterPoints, (d.t ?? Date.now() / 1000) * 1000, d.water_level_mm);
    }
    if (d.rise_rate_mm_s !== undefined) {
      riseRateEl.textContent = d.rise_rate_mm_s;
    }
    if (d.baseline_mm !== undefined) {
      baselineEl.textContent = d.baseline_mm ?? "not set";
    }
  }

  function handleWeather(w) {
    if (!w) return;
    if (w.manual) {
      const ageMin = Math.max(0, Math.round((Date.now() / 1000 - w.manual.set_at) / 60));
      manualStatus.textContent =
        `MANUAL ACTIVE · ${ageMin} min ago · obs ${w.manual.observed_mm_day} mm / fc ${w.manual.forecast_mm_24h} mm (24h)`;
      manualStatus.classList.add("warn");
    } else if (lastSkySource) {
      manualStatus.textContent = "auto: " + lastSkySource;
      manualStatus.classList.remove("warn");
    }
    if (w.nasa) {
      nasa7dEl.textContent = w.nasa.last_7d_mm ?? "–";
      const bits = [];
      if (w.nasa.error) bits.push("⚠ " + w.nasa.error);
      if (w.nasa.cached) bits.push("cached");
      bits.push(w.nasa.source || "NASA POWER");
      nasaDetail.textContent = bits.join(" · ");
    }
    if (w.forecast) {
      fc24El.textContent = w.forecast.rain_next_24h_mm ?? "–";
      fc6hEl.textContent = w.forecast.rain_next_6h_mm ?? "–";
    }
  }

  function connect() {
    setStatus("connecting", "connecting…");
    const es = new EventSource("/events");

    es.onopen = () => setStatus("live", "live");
    es.onerror = () => {
      setStatus("reconnecting", "reconnecting…");
      es.close();
      setTimeout(connect, 2000);
    };

    es.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch { return; }

      if (d.type === "hello" || d.type === "status") {
        handleEngineState(d);
        handleWeather(d.weather);
        if (d.type === "hello") {
          // prime the alert log with history
          fetch("/api/alerts").then(r => r.json()).then(j => renderAlertLog(j.alerts)).catch(() => {});
        }
        return;
      }
      if (d.type !== "sample") return;

      sampleCount++;
      countEl.textContent = sampleCount;
      lastUpdateEl.textContent = new Date(d.t * 1000).toLocaleTimeString();

      if (d.moisture !== null) {
        moistureVal.textContent = d.moisture;
        moistureBar.style.width = Math.max(0, Math.min(100, (d.moisture / 1023) * 100)) + "%";
        pushPoint(moisturePoints, d.t * 1000, d.moisture);
      }
      if (d.distance_mm !== null) {
        distanceVal.textContent = d.distance_mm;
        distanceBar.style.width = Math.max(0, Math.min(100, (d.distance_mm / 4000) * 100)) + "%";
        pushPoint(distancePoints, d.t * 1000, d.distance_mm);
      }
      if (d.humidity !== null && d.humidity >= 0) {
        humidityVal.textContent = d.humidity;
        pushPoint(humidityPoints, d.t * 1000, d.humidity);
      }
      if (d.temp_c !== null && d.temp_c > -999) {
        tempVal.textContent = d.temp_c;
        pushPoint(tempPoints, d.t * 1000, d.temp_c);
      }
      if (d.ax !== null && d.ay !== null && d.az !== null) {
        axVal.textContent = d.ax;
        ayVal.textContent = d.ay;
        azVal.textContent = d.az;
        const mag = Math.round(Math.sqrt(d.ax * d.ax + d.ay * d.ay + d.az * d.az));
        pushPoint(motionPoints, d.t * 1000, mag);
      }

      if (d.p_flood !== null && d.p_flood !== undefined) {
        pFloodEl.textContent = Math.round(d.p_flood * 100);
        mlBar.style.width = Math.max(0, Math.min(100, d.p_flood * 100)) + "%";
        pushPoint(mlPoints, d.t * 1000, d.p_flood * 100);
      }
      if (d.imu_peak !== null && d.imu_peak !== undefined) {
        imuPeakEl.textContent = d.imu_peak;
      }
      if (d.buzz !== null && d.buzz !== undefined) {
        buzzEl.textContent = d.buzz ? "ON — ALARM" : "off";
        buzzEl.style.color = d.buzz ? "#f85149" : "";
      }
      if (d.sky && d.sky.sat_src && d.sky.sat_src !== lastSkySource) {
        lastSkySource = d.sky.sat_src;
        if (d.sky.sat_src !== "MANUAL") {
          manualStatus.textContent = "auto: " + d.sky.sat_src;
          manualStatus.classList.remove("warn");
        }
      }
      if (d.mcls !== null && d.mcls !== undefined) {
        const names = ["normal", "rain approach", "flood rise", "debris impact"];
        let sub = names[d.mcls] ?? String(d.mcls);
        if (d.sky && typeof d.sky.sat === "number") {
          sub += ` · sky sat ${d.sky.sat.toFixed(2)} / fc ${d.sky.fc.toFixed(2)}`;
          if (d.sky.sat_src === "IMERG 30-min" && d.sky.sat_time) {
            sub += ` (IMERG obs ${d.sky.sat_time}Z)`;
          } else if (d.sky.sat_asof) {
            sub += ` (observed ${d.sky.sat_asof.slice(4, 6)}-${d.sky.sat_asof.slice(6, 8)})`;
          }
        } else if (d.mcls >= 0) {
          sub += " · sky: no bridge";
        }
        mclsEl.textContent = sub;
      }

      handleEngineState(d);
    };
  }

  el("btn-baseline").addEventListener("click", () => {
    fetch("/api/baseline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto: true }),
    }).catch(() => {});
  });

  el("btn-buzz-test").addEventListener("click", () => {
    fetch("/api/buzz-test", { method: "POST" }).catch(() => {});
  });

  el("btn-manual-apply").addEventListener("click", () => {
    const body = {
      observed_mm_day: parseFloat(el("m-obs").value) || 0,
      forecast_mm_6h: parseFloat(el("m-fc6").value) || 0,
      forecast_mm_24h: parseFloat(el("m-fc24").value) || 0,
    };
    fetch("/api/manual-sky", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {});
  });

  el("btn-manual-clear").addEventListener("click", () => {
    fetch("/api/manual-sky/clear", { method: "POST" }).catch(() => {});
  });

  connect();
})();
