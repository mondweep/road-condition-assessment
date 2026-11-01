// Road Recorder: summarises accelerometer + GPS into 1-second windows,
// buffers them on the phone, and uploads in batches when online.
(() => {
  const $ = (id) => document.getElementById(id);
  const LS = { key: "rca.key", veh: "rca.veh", dev: "rca.device", q: "rca.queue" };
  const MAX_QUEUE = 25000;          // ~7 h of driving
  const FLUSH_MS = 20000;
  const BATCH = 500;
  const GRAVITY_ALPHA = 0.02;       // low-pass for gravity estimate (~1 s at 60 Hz)

  const store = {
    get: (k, d = null) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };

  // --- setup -----------------------------------------------------------
  const params = new URLSearchParams(location.search);
  if (params.get("key")) { store.set(LS.key, params.get("key")); history.replaceState(null, "", "/record"); }
  $("key").value = store.get(LS.key, "");
  $("veh").value = store.get(LS.veh, "");
  $("key").onchange = () => store.set(LS.key, $("key").value.trim());
  $("veh").onchange = () => store.set(LS.veh, $("veh").value.trim());
  let deviceId = store.get(LS.dev);
  if (!deviceId) { deviceId = "d-" + Math.random().toString(36).slice(2, 10); store.set(LS.dev, deviceId); }

  let queue = [];
  try { queue = JSON.parse(store.get(LS.q, "[]")) || []; } catch { queue = []; }
  const saveQueue = () => { if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE); store.set(LS.q, JSON.stringify(queue)); $("q").textContent = queue.length; };
  $("q").textContent = queue.length;

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

  // --- state -----------------------------------------------------------
  let running = false, tripId = null, wakeLock = null, geoWatch = null, tick = null, flusher = null;
  let g = null;                       // gravity estimate [x,y,z]
  let buf = [];                       // vertical accel samples in current window
  let fix = null, prevSpeed = null, prevFix = null;
  let uploaded = 0;

  function onMotion(e) {
    const a = e.accelerationIncludingGravity;
    if (!a || a.x === null) return;
    const v = [a.x, a.y, a.z];
    if (!g) g = v.slice();
    for (let i = 0; i < 3; i++) g[i] += GRAVITY_ALPHA * (v[i] - g[i]);
    const gm = Math.hypot(g[0], g[1], g[2]) || 9.81;
    // project onto gravity direction, remove static 1 g -> vertical dynamic accel
    buf.push((v[0] * g[0] + v[1] * g[1] + v[2] * g[2]) / gm - gm);
  }

  function onPos(p) {
    const c = p.coords;
    let speed = c.speed;
    if ((speed === null || isNaN(speed)) && prevFix) {
      const dt = (p.timestamp - prevFix.t) / 1000;
      if (dt > 0) speed = haversine(prevFix.lat, prevFix.lon, c.latitude, c.longitude) / dt;
    }
    prevFix = { lat: c.latitude, lon: c.longitude, t: p.timestamp };
    fix = { lat: c.latitude, lon: c.longitude, acc: c.accuracy, speed: speed ?? null, heading: c.heading ?? null, t: p.timestamp };
    $("gps").textContent = Math.round(c.accuracy) + " m";
  }

  function haversine(la1, lo1, la2, lo2) {
    const R = 6371000, r = Math.PI / 180;
    const dla = (la2 - la1) * r, dlo = (lo2 - lo1) * r;
    const h = Math.sin(dla / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dlo / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function closeWindow() {
    const s = buf; buf = [];
    $("hz").textContent = s.length + " Hz";
    const now = Date.now();
    const fresh = fix && now - fix.t < 4000 ? fix : null;
    if (s.length < 10) return;
    const mean = s.reduce((x, y) => x + y, 0) / s.length;
    let ss = 0, peak = 0, lo = Infinity, hi = -Infinity;
    for (const x of s) { const d = x - mean; ss += d * d; peak = Math.max(peak, Math.abs(d)); lo = Math.min(lo, x); hi = Math.max(hi, x); }
    const std = Math.sqrt(ss / s.length);
    const speed = fresh ? fresh.speed : null;
    const w = {
      t: now, lat: fresh?.lat ?? null, lon: fresh?.lon ?? null, acc: fresh?.acc ?? null,
      speed, heading: fresh?.heading ?? null, n: s.length,
      z_std: +std.toFixed(4), z_peak: +peak.toFixed(4), z_p2p: +(hi - lo).toFixed(4),
      dv: speed !== null && prevSpeed !== null ? +(speed - prevSpeed).toFixed(3) : null,
    };
    if (speed !== null) prevSpeed = speed;
    showLive(w);
    if (w.lat !== null) { queue.push(w); if (queue.length % 10 === 0) saveQueue(); else $("q").textContent = queue.length; }
  }

  // mirrors app/scoring.py so the driver sees roughly what the server will record
  function showLive(w) {
    const sp = w.speed ?? 0;
    $("spd").textContent = w.speed === null ? "–" : Math.round(sp * 3.6);
    if (sp < 2) { $("rough").textContent = "–"; $("cond").textContent = sp ? "too slow to judge" : "waiting for GPS"; $("cond").className = ""; return; }
    const r = w.z_std * Math.sqrt(30 / Math.max(sp * 3.6, 10));
    const c = r < 0.6 ? "good" : r < 1.2 ? "fair" : r < 2.5 ? "poor" : "very_poor";
    $("rough").textContent = r.toFixed(2);
    $("cond").textContent = c.replace("_", " ");
    $("cond").className = c;
  }

  async function flush() {
    const key = store.get(LS.key, "");
    if (!key || !queue.length || !navigator.onLine) return;
    while (queue.length) {
      const chunk = queue.slice(0, BATCH);
      try {
        const r = await fetch("/api/ingest", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Ingest-Key": key },
          body: JSON.stringify({ device_id: deviceId, trip_id: tripId || "offline", vehicle: store.get(LS.veh, "") || null, windows: chunk }),
        });
        if (r.status === 401) { msg("Upload rejected: check the ingest key."); return; }
        if (!r.ok) throw new Error(r.status);
        queue = queue.slice(chunk.length); uploaded += chunk.length;
        saveQueue();
        $("up").textContent = uploaded;
        $("lu").textContent = new Date().toLocaleTimeString();
      } catch { return; } // offline or server error: keep queue, retry later
    }
  }

  async function lockScreen() {
    try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
  }
  document.addEventListener("visibilitychange", () => {
    if (!running) return;
    if (document.visibilityState === "visible") lockScreen();
    else msg("Screen was hidden — sensors pause until you come back.");
  });
  window.addEventListener("online", flush);

  const msg = (t) => { $("msg").textContent = t; };

  async function start() {
    if (!store.get(LS.key, "")) { msg("Enter the ingest key first."); return; }
    if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
      try { if (await DeviceMotionEvent.requestPermission() !== "granted") { msg("Motion permission denied."); return; } }
      catch { msg("Motion permission failed."); return; }
    }
    if (!("geolocation" in navigator)) { msg("No GPS available in this browser."); return; }
    running = true; tripId = "t-" + Date.now().toString(36); g = null; buf = []; prevSpeed = null;
    window.addEventListener("devicemotion", onMotion);
    geoWatch = navigator.geolocation.watchPosition(onPos, (e) => msg("GPS: " + e.message), { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    tick = setInterval(closeWindow, 1000);
    flusher = setInterval(flush, FLUSH_MS);
    await lockScreen();
    $("go").textContent = "Stop recording"; $("go").classList.add("on"); $("setup").style.display = "none"; msg("");
  }

  function stop() {
    running = false;
    window.removeEventListener("devicemotion", onMotion);
    if (geoWatch !== null) navigator.geolocation.clearWatch(geoWatch);
    clearInterval(tick); clearInterval(flusher);
    try { wakeLock && wakeLock.release(); } catch {}
    saveQueue(); flush();
    $("go").textContent = "Start recording"; $("go").classList.remove("on"); $("setup").style.display = "";
  }

  $("go").onclick = () => (running ? stop() : start());
  flush();
})();
