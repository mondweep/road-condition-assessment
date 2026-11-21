// Pure signal-processing helpers shared by the recorder and the tests.
// Works in the browser (window.Signals) and in Node (module.exports).
(function (root) {
  const JOLT_MS2 = 2.0;          // vertical excursion counted as a lurch
  const JOLT_REFRACTORY = 0.25;  // s; one lurch can't be counted twice

  // Summarise one second of samples. z: vertical dynamic accel (m/s^2),
  // yaw: rotation about the gravity axis (deg/s), may contain nulls.
  function summarise(z, yaw) {
    const n = z.length;
    if (n < 10) return null;
    const mean = z.reduce((a, b) => a + b, 0) / n;
    let ss = 0, peak = 0, lo = Infinity, hi = -Infinity, jolts = 0, last = -1e9;
    const refr = Math.max(1, Math.round(JOLT_REFRACTORY * n));
    for (let i = 0; i < n; i++) {
      const d = z[i] - mean;
      ss += d * d; peak = Math.max(peak, Math.abs(d));
      lo = Math.min(lo, z[i]); hi = Math.max(hi, z[i]);
      if (Math.abs(d) >= JOLT_MS2 && i - last >= refr) { jolts++; last = i; }
    }
    const out = {
      n, z_std: +Math.sqrt(ss / n).toFixed(4), z_peak: +peak.toFixed(4), z_p2p: +(hi - lo).toFixed(4), jolts,
      yaw_mean: null, yaw_max: null, yaw_min: null,
    };
    const y = (yaw || []).filter((v) => v !== null && !isNaN(v));
    if (y.length >= n / 2) {
      out.yaw_mean = +(y.reduce((a, b) => a + b, 0) / y.length).toFixed(3);
      out.yaw_max = +Math.max(...y).toFixed(3);
      out.yaw_min = +Math.min(...y).toFixed(3);
    }
    return out;
  }

  // Detects an out-and-back steer (a dodge) from a stream of 1-s windows.
  // A dodge: straight before, a quick steer one way and back within 2-4 s,
  // ending on roughly the same heading, with a small total swing. A road
  // bend changes heading for good; an S-bend swings much further.
  const SW = {
    YAW: 7,             // deg/s each way
    STRAIGHT: 3,        // deg/s mean yaw before the manoeuvre
    MAX_NET: 8,         // deg heading change after vs before
    MIN_SWING: 3,       // deg peak deviation
    MAX_SWING: 20,      // deg peak deviation (bigger = road bend)
    MIN_SPEED: 1.5,     // m/s (~5 km/h)
    BRAKE: -0.7,        // m/s per s
    COOLDOWN: 4,        // windows
  };

  function SwerveDetector() {
    this.hist = [];
    this.cool = 0;
  }
  // Feed windows in order. Returns null, or {window, brake} for the window
  // in the middle of a detected dodge (the caller flags it).
  SwerveDetector.prototype.push = function (w) {
    this.hist.push(w);
    if (this.hist.length > 6) this.hist.shift();
    if (this.cool > 0) { this.cool--; return null; }
    if (w.yaw_mean === null || w.yaw_mean === undefined) return null;
    const h = this.hist;
    for (let L = 2; L <= 4; L++) {
      if (h.length < L + 1) break;
      const pre = h[h.length - L - 1];
      const span = h.slice(-L);
      if (pre.yaw_mean === null || Math.abs(pre.yaw_mean) > SW.STRAIGHT) continue;
      if (span.some((s) => s.yaw_mean === null || (s.speed ?? 0) < SW.MIN_SPEED)) continue;
      const hiY = Math.max(...span.map((s) => s.yaw_max));
      const loY = Math.min(...span.map((s) => s.yaw_min));
      if (hiY < SW.YAW || loY > -SW.YAW) continue;
      let cum = 0, swing = 0;
      for (const s of span) { cum += s.yaw_mean; swing = Math.max(swing, Math.abs(cum)); }
      // the steer can cancel within one window, so also use half the per-window extreme
      swing = Math.max(swing, Math.min(hiY, -loY) * 0.5);
      if (Math.abs(cum) > SW.MAX_NET || swing < SW.MIN_SWING || swing > SW.MAX_SWING) continue;
      const brake = [pre, ...span].some((s) => s.dv !== null && s.dv !== undefined && s.dv <= SW.BRAKE);
      this.cool = SW.COOLDOWN;
      return { window: span[Math.floor((L - 1) / 2)], brake };
    }
    return null;
  };

  function haversine(la1, lo1, la2, lo2) {
    const R = 6371000, r = Math.PI / 180;
    const dla = (la2 - la1) * r, dlo = (lo2 - lo1) * r;
    const a = Math.sin(dla / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dlo / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  // Privacy: hides the start and end of each trip.
  // Start: nothing is released until the vehicle is TRIM_M in a straight line
  // from where recording began. End: windows are held back until the vehicle
  // has driven a further HOLD_M; whatever is still held when the trip stops
  // is discarded.
  function TripTrimmer(trimM = 300, holdM = 600) {
    this.trimM = trimM; this.holdM = holdM;
    this.start = null; this.started = false; this.dist = 0; this.last = null; this.held = [];
  }
  // Returns windows that are now safe to upload.
  TripTrimmer.prototype.push = function (w) {
    if (w.lat === null || w.lat === undefined) return [];
    if (this.last) this.dist += haversine(this.last.lat, this.last.lon, w.lat, w.lon);
    this.last = { lat: w.lat, lon: w.lon };
    if (!this.start) this.start = { lat: w.lat, lon: w.lon };
    if (!this.started) {
      if (haversine(this.start.lat, this.start.lon, w.lat, w.lon) < this.trimM) return [];
      this.started = true;
    }
    this.held.push({ w, d: this.dist });
    const out = [];
    while (this.held.length && this.dist - this.held[0].d >= this.holdM) out.push(this.held.shift().w);
    return out;
  };
  TripTrimmer.prototype.stop = function () { const n = this.held.length; this.held = []; return n; };
  TripTrimmer.prototype.status = function () {
    return this.started ? "on" : "waiting";
  };

  function classify(roughness, thresholds) {
    const labels = ["good", "fair", "poor", "very_poor"];
    for (let i = 0; i < 3; i++) if (roughness < thresholds[i]) return labels[i];
    return labels[3];
  }

  function regionOf(lat, lon, regions) {
    for (const [k, r] of Object.entries(regions || {})) {
      const [a, b, c, d] = r.bbox;
      if (lat >= a && lat <= b && lon >= c && lon <= d) return k;
    }
    return "other";
  }

  const api = { summarise, SwerveDetector, TripTrimmer, haversine, classify, regionOf, SW, JOLT_MS2 };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Signals = api;
})(typeof window !== "undefined" ? window : globalThis);
