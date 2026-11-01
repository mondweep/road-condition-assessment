import os

os.environ["STORE"] = "memory"
os.environ["INGEST_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402

from app import geohash  # noqa: E402
from app.main import app  # noqa: E402
from app.scoring import classify, score_window  # noqa: E402

client = TestClient(app)


def win(**kw):
    base = dict(t=1.7e12, lat=26.1445, lon=91.7362, acc=8, speed=8.0, n=60, z_std=0.3, z_peak=0.8, z_p2p=1.4)
    base.update(kw)
    return base


def test_geohash_known_value():
    # reference example from the geohash specification (Jutland, Denmark)
    assert geohash.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_classes():
    assert classify(0.3) == "good"
    assert classify(1.0) == "fair"
    assert classify(2.0) == "poor"
    assert classify(3.0) == "very_poor"


def test_filters():
    assert score_window(win(speed=1.0)).reason == "too_slow"
    assert score_window(win(acc=80)).reason == "poor_gps"
    assert score_window(win(lat=None)).reason == "no_position"


def test_speed_normalisation():
    slow = score_window(win(speed=4.0, z_std=1.0)).roughness
    fast = score_window(win(speed=20.0, z_std=1.0)).roughness
    assert slow > fast  # same shaking at low speed means a rougher road


def test_pothole_event():
    assert score_window(win(z_std=0.5, z_peak=6.0)).event
    assert not score_window(win(z_std=2.5, z_peak=5.0)).event  # uniformly rough, no isolated jolt


def test_ingest_and_cells():
    assert client.post("/api/ingest", json={"device_id": "d", "trip_id": "t", "windows": []},
                       headers={"X-Ingest-Key": "nope"}).status_code == 401
    r = client.post("/api/ingest", headers={"X-Ingest-Key": "test-key"}, json={
        "device_id": "d1", "trip_id": "t1",
        "windows": [win(), win(z_std=1.0, z_peak=7.0), win(speed=0.5)],
    })
    assert r.status_code == 200
    assert r.json() == {"accepted": 2, "skipped": {"too_slow": 1}}
    cells = client.get("/api/cells").json()
    assert len(cells) == 1 and cells[0]["n"] == 2 and cells[0]["events"] == 1
