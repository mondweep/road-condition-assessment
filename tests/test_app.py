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
