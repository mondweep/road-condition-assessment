// Road Recorder: summarises motion + GPS into 1-second windows, detects
// swerves, hides trip start/end, buffers on the phone and uploads when online.
(() => {
  const $ = (id) => document.getElementById(id);
  const S = window.Signals;
  const LS = { key: "rca.key", tok: "rca.token", veh: "rca.veh", dev: "rca.device", q: "rca.queue", onb: "rca.onboarded", cfg: "rca.config" };
  const MAX_QUEUE = 25000;          // ~7 h of driving
  const FLUSH_MS = 20000;
  const BATCH = 500;
  const GRAVITY_ALPHA = 0.02;       // low-pass for gravity estimate (~1 s at 60 Hz)
  const FALLBACK_CFG = { regions: {}, default: { thresholds: [0.6, 1.2, 2.5] }, min_speed_ms: 0.8, crawl_speed_ms: 2.8, crawl_unit: 1.2 };

  const store = {
    get: (k, d = null) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };
  const msg = (t) => { $("msg").textContent = t; };

  // --- setup -----------------------------------------------------------
  const params = new URLSearchParams(location.search);
  if (params.get("key")) { store.set(LS.key, params.get("key")); history.replaceState(null, "", "/record"); }
  $("veh").value = store.get(LS.veh, "");
  $("veh").onchange = () => store.set(LS.veh, $("veh").value.trim());

  // Access: every phone registers itself and gets its own upload token.
  // An admin/invite key (?key=...) still works and skips registration.
  let deviceId = store.get(LS.dev);
  let token = store.get(LS.tok, "");
  const adminKey = () => store.get(LS.key, "");
  let registering = null;
  async function ensureDevice() {
    if (adminKey()) {
      if (!deviceId) { deviceId = "d-" + Math.random().toString(36).slice(2, 10); store.set(LS.dev, deviceId); }
      return true;
    }
    if (token && deviceId) return true;
    if (!navigator.onLine) return false;
    registering = registering || fetch("/api/register", { method: "POST" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => { deviceId = d.device_id; token = d.token; store.set(LS.dev, deviceId); store.set(LS.tok, token); return true; })
      .catch(() => false)
      .finally(() => { registering = null; });
    return registering;
  }
  ensureDevice();

  let cfg = FALLBACK_CFG;
  try { cfg = JSON.parse(store.get(LS.cfg)) || FALLBACK_CFG; } catch {}
  fetch("/api/config").then((r) => r.json()).then((c) => { cfg = c; store.set(LS.cfg, JSON.stringify(c)); }).catch(() => {});

  let queue = [];
  try { queue = JSON.parse(store.get(LS.q, "[]")) || []; } catch { queue = []; }
  const saveQueue = () => { if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE); store.set(LS.q, JSON.stringify(queue)); $("q").textContent = queue.length; };
  $("q").textContent = queue.length;

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

  // --- onboarding --------------------------------------------------------
  const steps = [...document.querySelectorAll(".step")];
  let stepIdx = 0;
  function showStep(i) {
    stepIdx = i;
    steps.forEach((s, j) => (s.hidden = j !== i));
    $("onb-dots").textContent = steps.map((_, j) => (j === i ? "●" : "○")).join(" ");
    $("onb-back").style.visibility = i === 0 ? "hidden" : "visible";
    $("onb-next").textContent = i === steps.length - 1 ? "Got it" : "Next";
  }
  function openGuide() { showStep(0); $("onb").hidden = false; }
  function closeGuide() { $("onb").hidden = true; store.set(LS.onb, "1"); }
  $("onb-next").onclick = () => (stepIdx === steps.length - 1 ? closeGuide() : showStep(stepIdx + 1));
  $("onb-back").onclick = () => showStep(Math.max(0, stepIdx - 1));
  $("onb-skip").onclick = closeGuide;
  $("help").onclick = openGuide;
  if (!store.get(LS.onb)) openGuide();

  // --- state -----------------------------------------------------------
  let running = false, tripId = null, wakeLock = null, geoWatch = null, tick = null, flusher = null;
  let g = null, bufZ = [], bufYaw = [];
  let fix = null, prevSpeed = null, prevFix = null;
  let uploaded = 0, swerves = 0, motionSeen = false;
  let detector = null, trimmer = null;

  function onMotion(e) {
    const a = e.accelerationIncludingGravity;
    if (!a || a.x === null) return;
    motionSeen = true;
    const v = [a.x, a.y, a.z];
    if (!g) g = v.slice();
    for (let i = 0; i < 3; i++) g[i] += GRAVITY_ALPHA * (v[i] - g[i]);
    const gm = Math.hypot(g[0], g[1], g[2]) || 9.81;
    // vertical dynamic acceleration: project onto gravity, remove the static 1 g
    bufZ.push((v[0] * g[0] + v[1] * g[1] + v[2] * g[2]) / gm - gm);
    // yaw rate: rotation about the gravity axis. rotationRate is deg/s about
    // device z (alpha), x (beta), y (gamma).
    const r = e.rotationRate;
    if (r && r.alpha !== null && r.beta !== null && r.gamma !== null) {
      bufYaw.push((r.beta * g[0] + r.gamma * g[1] + r.alpha * g[2]) / gm);
    } else bufYaw.push(null);
  }

  function onPos(p) {
    const c = p.coords;
    let speed = c.speed;
    if ((speed === null || isNaN(speed)) && prevFix) {
      const dt = (p.timestamp - prevFix.t) / 1000;
      if (dt > 0) speed = S.haversine(prevFix.lat, prevFix.lon, c.latitude, c.longitude) / dt;
    }
    prevFix = { lat: c.latitude, lon: c.longitude, t: p.timestamp };
    fix = { lat: c.latitude, lon: c.longitude, acc: c.accuracy, speed: speed ?? null, heading: c.heading ?? null, t: p.timestamp };
    $("gps").textContent = Math.round(c.accuracy) + " m";
    if ($("msg").textContent.startsWith("Waiting for GPS")) msg("");
  }

  function closeWindow() {
    const z = bufZ, yaw = bufYaw; bufZ = []; bufYaw = [];
    $("hz").textContent = z.length + " Hz" + (yaw.some((v) => v !== null) ? " · gyro on" : " · no gyro");
    const sum = S.summarise(z, yaw);
    if (!sum) return;
    const now = Date.now();
    const f = fix && now - fix.t < 4000 ? fix : null;
    const speed = f ? f.speed : null;
    const w = {
      t: now, lat: f?.lat ?? null, lon: f?.lon ?? null, acc: f?.acc ?? null, speed, heading: f?.heading ?? null,
      ...sum, dv: speed !== null && prevSpeed !== null ? +(speed - prevSpeed).toFixed(3) : null,
      swerve: false, swerve_brake: false,
    };
    if (speed !== null) prevSpeed = speed;

    const hit = detector.push(w);
    if (hit) { hit.window.swerve = true; hit.window.swerve_brake = hit.brake; swerves++; $("sw").textContent = swerves; }

    showLive(w);
    const ready = trimmer.push(w);
    $("trim").textContent = trimmer.status() === "on" ? "recording" : "hidden until you're 300 m from the start";
    if (ready.length) { queue.push(...ready); if (queue.length % 10 < ready.length) saveQueue(); else $("q").textContent = queue.length; }
  }

  // mirrors app/scoring.py so the driver sees roughly what the server will record
  function showLive(w) {
    const sp = w.speed;
    $("spd").textContent = sp === null ? "–" : Math.round(sp * 3.6);
    const setCond = (t, c = "") => { $("cond").textContent = t; $("cond").className = c; };
    if (sp === null) { $("rough").textContent = "–"; return setCond("waiting for GPS"); }
    if (sp < cfg.min_speed_ms) { $("rough").textContent = "–"; return setCond("stopped"); }
    const reg = S.regionOf(w.lat, w.lon, cfg.regions);
    const th = (cfg.regions[reg] || cfg.default).thresholds;
    let r;
    if (sp < cfg.crawl_speed_ms) {
      if (!w.jolts) { $("rough").textContent = "–"; return setCond("crawling, smooth"); }
      r = Math.min(cfg.crawl_unit * w.jolts, 4);
    } else r = w.z_std * Math.sqrt(30 / Math.max(sp * 3.6, 10));
    const c = S.classify(r, th);
    $("rough").textContent = r.toFixed(2);
    setCond(c.replace("_", " ") + (sp < cfg.crawl_speed_ms ? " (crawling)" : ""), c);
  }

  async function flush() {
    if (!queue.length || !navigator.onLine) return;
    if (!(await ensureDevice())) return;
    const key = adminKey();
    const auth = key ? { "X-Ingest-Key": key } : { "X-Device-Token": token };
    while (queue.length) {
      const chunk = queue.slice(0, BATCH);
      const trip = chunk[0]._trip || tripId || "offline";
      const same = chunk.filter((w) => (w._trip || trip) === trip);
      try {
        const r = await fetch("/api/ingest", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...auth },
          body: JSON.stringify({ device_id: deviceId, trip_id: trip, vehicle: store.get(LS.veh, "") || null,
                                 windows: same.map(({ _trip, ...w }) => w) }),
        });
        if (r.status === 401) {
          if (key) { msg("Upload rejected: the invite key in your link is no longer valid."); store.set(LS.key, ""); }
          token = ""; store.set(LS.tok, "");   // re-register on the next attempt
          return;
        }
        if (r.status === 403) { msg("Uploads from this phone have been paused. Contact the project team if this is a mistake."); return; }
        if (r.status === 429) return;          // over the limit: keep readings, try again later
        if (!r.ok) throw new Error(r.status);
        queue = queue.slice(same.length); uploaded += same.length;
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

  async function start() {
    ensureDevice();   // fine if offline: readings are kept and uploaded once registered
    // iOS requires an explicit grant; some other browsers expose the same call.
    // Don't block on its answer: check below that motion data actually arrives.
    if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
      try { await DeviceMotionEvent.requestPermission(); } catch {}
    }
    setTimeout(() => {
      if (running && !motionSeen) msg("No motion data from this phone. On iPhone: allow Motion & Orientation access and reload. Otherwise try Chrome or Safari.");
    }, 4000);
    if (!("geolocation" in navigator)) { msg("No GPS available in this browser."); return; }
    running = true; tripId = "t-" + Date.now().toString(36); g = null; bufZ = []; bufYaw = []; prevSpeed = null; swerves = 0;
    detector = new S.SwerveDetector(); trimmer = new S.TripTrimmer(300, 400);
    const origPush = trimmer.push.bind(trimmer);
    trimmer.push = (w) => origPush(w).map((x) => ((x._trip = tripId), x));
    $("sw").textContent = 0;
    window.addEventListener("devicemotion", onMotion);
    geoWatch = navigator.geolocation.watchPosition(onPos, (e) => msg(e.code === 1 ? "Location permission denied — allow location for this site and press Start again." : "Waiting for GPS signal…"), { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    tick = setInterval(closeWindow, 1000);
    flusher = setInterval(flush, FLUSH_MS);
    await lockScreen();
    $("go").textContent = "Stop recording"; $("go").classList.add("on"); $("setup").hidden = true; msg("");
  }

  function stop() {
    running = false;
    window.removeEventListener("devicemotion", onMotion);
    if (geoWatch !== null) navigator.geolocation.clearWatch(geoWatch);
    clearInterval(tick); clearInterval(flusher);
    try { wakeLock && wakeLock.release(); } catch {}
    const dropped = trimmer ? trimmer.stop() : 0;
    saveQueue(); flush();
    $("trim").textContent = dropped ? `last ${dropped} s of the trip discarded for privacy` : "–";
    $("go").textContent = "Start recording"; $("go").classList.remove("on"); $("setup").hidden = false;
  }

  $("go").onclick = () => (running ? stop() : start());
  flush();
})();
