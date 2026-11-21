// Run: node --test tests/
const test = require("node:test");
const assert = require("node:assert");
const S = require("../app/static/signals.js");

const W = (yaw_mean, yaw_max, yaw_min, extra = {}) => ({ yaw_mean, yaw_max, yaw_min, speed: 6, dv: 0, ...extra });
const run = (ws) => { const d = new S.SwerveDetector(); return ws.map((w) => d.push(w)).filter(Boolean); };

test("dodge on a straight road is detected, with braking", () => {
  const hits = run([W(0, 1, -1), W(0, 1, -1, { dv: -1.2 }), W(6, 12, 0), W(-6, 0, -12), W(0, 1, -1)]);
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].brake, true);
});

test("steady bend is not a swerve", () => {
  assert.strictEqual(run([W(0, 1, -1), W(10, 12, 8), W(10, 12, 8), W(10, 12, 8), W(10, 12, 8)]).length, 0);
});

test("wide S-bend is not a swerve", () => {
  assert.strictEqual(run([W(0, 1, -1), W(15, 18, 12), W(15, 18, 12), W(-15, -12, -18), W(-15, -12, -18), W(0, 1, -1)]).length, 0);
});

test("straight driving and missing gyro produce nothing", () => {
  assert.strictEqual(run([W(0, 2, -2), W(0.5, 2, -1), W(-0.5, 1, -2), W(0, 2, -2)]).length, 0);
  assert.strictEqual(run([W(null, null, null), W(null, null, null), W(null, null, null)]).length, 0);
});

test("dodge at walking pace is ignored", () => {
  const slow = { speed: 1.0 };
  assert.strictEqual(run([W(0, 1, -1, slow), W(6, 12, 0, slow), W(-6, 0, -12, slow)]).length, 0);
});

test("summarise counts distinct lurches", () => {
  const z = new Array(60).fill(0);
  z[10] = 3; z[11] = 3.2; z[40] = -2.5;     // two lurches (10/11 is one)
  const s = S.summarise(z, new Array(60).fill(0));
  assert.strictEqual(s.jolts, 2);
  assert.strictEqual(s.yaw_mean, 0);
  assert.strictEqual(S.summarise([1, 2], []), null);
});

test("trimmer hides trip start and end", () => {
  const t = new S.TripTrimmer(300, 600);
  const step = 0.0009; // ~100 m of latitude
  let out = [];
  for (let i = 0; i <= 20; i++) out = out.concat(t.push({ lat: 26 + i * step, lon: 92, i }));
  // first ~300 m dropped, last ~600 m held
  assert.ok(out.length > 0);
  assert.ok(out[0].i >= 3, "start trimmed");
  assert.ok(out[out.length - 1].i <= 20 - 6, "end held back");
  const discarded = t.stop();
  assert.ok(discarded >= 6);
});

test("classify and region lookup", () => {
  assert.strictEqual(S.classify(0.5, [0.6, 1.2, 2.5]), "good");
  const regions = { uk: { bbox: [49.8, 61, -8.7, 2.1] } };
  assert.strictEqual(S.regionOf(51.4, 0.4, regions), "uk");
  assert.strictEqual(S.regionOf(26, 92, regions), "other");
});
