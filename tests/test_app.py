import os

os.environ["STORE"] = "memory"
os.environ["INGEST_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402

from app import geohash  # noqa: E402
from app.main import app, store  # noqa: E402
from app.scoring import classify, is_hotspot, region_of, score_window  # noqa: E402

client = TestClient(app)
GUWAHATI = dict(lat=26.1445, lon=91.7362)
KENT = dict(lat=51.36, lon=0.40)
H = {"X-Ingest-Key": "test-key"}


def win(**kw):
    base = dict(t=1.7e12, acc=8, speed=8.0, n=60, z_std=0.3, z_peak=0.8, z_p2p=1.4, jolts=0, **GUWAHATI)
    base.update(kw)
    return base


def post(trip, windows, device="d1"):
    return client.post("/api/ingest", headers=H, json={"device_id": device, "trip_id": trip, "windows": windows})


def test_geohash_known_value():
    # reference example from the geohash specification
    assert geohash.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_regions():
    assert region_of(**GUWAHATI) == "ne_india"
    assert region_of(**KENT) == "uk"
    assert region_of(40.7, -74.0) == "other"


def test_classes_differ_by_region():
    assert classify(0.5, "ne_india") == "good"
    assert classify(0.5, "uk") == "fair"
    assert classify(3.0, "ne_india") == "very_poor"


def test_filters():
    assert score_window(win(speed=0.3)).reason == "stopped"
    assert score_window(win(acc=80)).reason == "poor_gps"
    assert score_window(win(lat=None)).reason == "no_position"


def test_crawl_mode():
    # 5 km/h with no lurches: likely traffic, not evidence of a good road
    assert score_window(win(speed=1.4, jolts=0)).reason == "crawl_no_evidence"
    s = score_window(win(speed=1.4, jolts=2))
    assert s.usable and s.mode == "crawl" and s.condition == "poor"
    assert score_window(win(speed=1.4, jolts=4)).condition == "very_poor"


def test_speed_normalisation():
    slow = score_window(win(speed=4.0, z_std=1.0)).roughness
    fast = score_window(win(speed=20.0, z_std=1.0)).roughness
    assert slow > fast


def test_pothole_event():
    assert score_window(win(z_std=0.5, z_peak=6.0)).event
    assert not score_window(win(z_std=2.5, z_peak=5.0)).event
    # UK uses a lower jolt threshold
    assert score_window(win(z_std=0.3, z_peak=3.8, **KENT)).event


def test_hotspot_rule():
    assert not is_hotspot(10, 1)      # one swerve is noise
    assert is_hotspot(4, 2)
    assert not is_hotspot(20, 2)      # most drivers went straight through


def test_ingest_cells_and_hotspot():
    store.cells.clear()
    assert client.post("/api/ingest", json={"device_id": "d", "trip_id": "t", "windows": []},
                       headers={"X-Ingest-Key": "nope"}).status_code == 401
    r = post("t1", [win(), win(z_std=1.0, z_peak=7.0), win(speed=0.3)])
    assert r.json() == {"accepted": 2, "skipped": {"stopped": 1}}
    cells = client.get("/api/cells").json()
    assert len(cells) == 1 and cells[0]["n"] == 2 and cells[0]["events"] == 1
    assert not cells[0]["hotspot"]

    # two more trips swerve at the same spot, one of them while crawling with no lurch
    post("t2", [win(swerve=True)], device="d2")
    post("t3", [win(speed=1.5, jolts=0, swerve=True, swerve_brake=True)], device="d3")
    c = client.get("/api/cells").json()[0]
    assert c["trips"] == 3 and c["swerve_trips"] == 2 and c["hotspot"]
    assert c["n"] == 3  # the slow no-lurch swerve adds position evidence, not roughness


def test_config():
    cfg = client.get("/api/config").json()
    assert "uk" in cfg["regions"] and cfg["regions"]["uk"]["thresholds"][0] < cfg["regions"]["ne_india"]["thresholds"][0]


# --- open contribution with safeguards -------------------------------------
import time  # noqa: E402

from app import access  # noqa: E402
from app.main import limiter  # noqa: E402


def now_win(**kw):
    return win(t=time.time() * 1000, **kw)


def register():
    r = client.post("/api/register")
    assert r.status_code == 200
    return r.json()


def device_post(dev, windows, token=None):
    return client.post("/api/ingest", headers={"X-Device-Token": dev["token"] if token is None else token},
                       json={"device_id": dev["device_id"], "trip_id": "t-open", "windows": windows})


def test_register_and_upload_without_key():
    dev = register()
    assert dev["device_id"].startswith("p-") and len(dev["token"]) == 32
    r = device_post(dev, [now_win()])
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_token_is_bound_to_its_device():
    a, b = register(), register()
    assert device_post(a, [now_win()], token=b["token"]).status_code == 401
    assert device_post(a, [now_win()], token="").status_code == 401


def test_implausible_windows_dropped():
    dev = register()
    r = device_post(dev, [now_win(speed=80), win(t=time.time() * 1000 + 3600e3), win(t=1.0e12), now_win()])
    j = r.json()
    assert j["accepted"] == 1 and j["skipped"] == {"implausible": 3}


def test_block_and_unblock():
    dev = register()
    assert client.post("/api/admin/block", json={"device_id": dev["device_id"]}).status_code == 401
    assert client.post("/api/admin/block", headers=H, json={"device_id": dev["device_id"], "note": "spam"}).status_code == 200
    assert device_post(dev, [now_win()]).status_code == 403
    client.post("/api/admin/block", headers=H, json={"device_id": dev["device_id"], "blocked": False})
    assert device_post(dev, [now_win()]).status_code == 200
    assert dev["device_id"] not in client.get("/api/admin/devices", headers=H).json()["blocked"]


def test_upload_rate_limits():
    dev = register()
    for _ in range(access.REQUESTS_PER_MINUTE):
        assert device_post(dev, [now_win()]).status_code == 200
    assert device_post(dev, [now_win()]).status_code == 429
    l = access.Limiter()
    assert l.allow_upload("x", 2000, now=1000) == ""
    assert l.allow_upload("x", 2000, now=1001) == ""
    assert l.allow_upload("x", 2000, now=1002) == "too much data this hour"
    assert l.allow_upload("x", 2000, now=1000 + 3601) == ""


def test_registration_rate_limit():
    limiter.regs.clear()
    codes = [client.post("/api/register").status_code for _ in range(access.REGISTRATIONS_PER_HOUR_PER_IP + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {200}
    limiter.regs.clear()


# --- free trial, bot check and Google sign-in --------------------------------
from app import identity, main  # noqa: E402


def auth(dev):
    return {"X-Device-Id": dev["device_id"], "X-Device-Token": dev["token"]}


def test_trial_runs_out_then_sign_in_unlocks(monkeypatch):
    monkeypatch.setattr(main, "TRIAL_SECONDS", 5)
    dev = register()
    assert client.get("/api/me", headers=auth(dev)).json() == {"signed_in": None, "trial_left_s": 5}
    r = device_post(dev, [now_win() for _ in range(3)]).json()
    assert r["trial_left_s"] == 2 and r["unsent"] == 0
    r = device_post(dev, [now_win() for _ in range(4)]).json()   # only 2 seconds left
    assert r["accepted"] == 2 and r["unsent"] == 2 and r["trial_left_s"] == 0
    rr = device_post(dev, [now_win()])
    assert rr.status_code == 402 and rr.json()["detail"] == "trial_over"

    monkeypatch.setattr(identity, "verify_google", lambda cred: {"email": "driver@example.com", "name": "A Driver", "sub": "123"})
    r = client.post("/api/signin", headers=auth(dev), json={"credential": "x"})
    assert r.status_code == 200 and r.json()["signed_in"] == "driver@example.com"
    r = device_post(dev, [now_win() for _ in range(10)]).json()
    assert r["accepted"] == 10 and r["trial_left_s"] is None
    assert client.get("/api/me", headers=auth(dev)).json()["signed_in"] == "driver@example.com"
    users = client.get("/api/admin/devices", headers=H).json()["users"]
    assert any(u["email"] == "driver@example.com" and dev["device_id"] in u["devices"] for u in users)

    # blocking the account stops every phone linked to it
    client.post("/api/admin/block", headers=H, json={"email": "Driver@Example.com"})
    assert device_post(dev, [now_win()]).status_code == 403
    assert client.post("/api/signin", headers=auth(dev), json={"credential": "x"}).status_code == 403
    client.post("/api/admin/block", headers=H, json={"email": "driver@example.com", "blocked": False})


def test_signin_needs_valid_device_and_token(monkeypatch):
    def bad(cred):
        raise ValueError("bad token")
    monkeypatch.setattr(identity, "verify_google", bad)
    dev = register()
    assert client.post("/api/signin", headers=auth(dev), json={"credential": "x"}).status_code == 401
    assert client.post("/api/signin", headers={"X-Device-Id": dev["device_id"], "X-Device-Token": "no"},
                       json={"credential": "x"}).status_code == 401


def test_bot_check_on_register(monkeypatch):
    seen = {}
    def fake(token, action="register"):
        seen["token"] = token
        return token == "human"
    monkeypatch.setattr(identity, "human_ok", fake)
    limiter.regs.clear()
    assert client.post("/api/register", json={"captcha": "bot"}).status_code == 403
    assert client.post("/api/register", json={"captcha": "human"}).status_code == 200
    assert seen["token"] == "human"
    limiter.regs.clear()


def test_config_exposes_public_keys_only():
    cfg = client.get("/api/config").json()
    assert {"trial_seconds", "recaptcha_site_key", "google_client_id"} <= set(cfg)
    assert "INGEST_KEY" not in str(cfg) and "test-key" not in str(cfg)
