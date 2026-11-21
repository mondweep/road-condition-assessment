import hmac
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import geohash
from .scoring import classify, is_hotspot, public_config, region_of, score_window
from .store import make_store

STATIC = Path(__file__).parent / "static"
INGEST_KEY = os.environ.get("INGEST_KEY", "")
CELL_PRECISION = 8
VERSION_CHECK_S = 20

app = FastAPI(title="Road Condition Assessment")
store = make_store(os.environ.get("STORE", "firestore"))
_cache = {"checked": 0.0, "version": None, "data": None}


class Window(BaseModel):
    t: float                        # epoch ms at window end
    lat: Optional[float] = None
    lon: Optional[float] = None
    acc: Optional[float] = None     # GPS accuracy, m
    speed: Optional[float] = None   # m/s
    heading: Optional[float] = None
    n: int = 0                      # accelerometer samples in window
    z_std: float = 0.0              # std of vertical accel, m/s^2
    z_peak: float = 0.0             # max |vertical accel - mean|, m/s^2
    z_p2p: float = 0.0
    jolts: Optional[int] = None     # distinct vertical lurches in window
    dv: Optional[float] = None      # speed change over window, m/s
    yaw_mean: Optional[float] = None  # gyro yaw rate, deg/s (signed)
    yaw_max: Optional[float] = None
    yaw_min: Optional[float] = None
    swerve: bool = False            # phone detected an out-and-back steer here
    swerve_brake: bool = False      # ...with braking just before it


class Batch(BaseModel):
    device_id: str = Field(max_length=64)
    trip_id: str = Field(max_length=64)
    vehicle: Optional[str] = Field(default=None, max_length=64)
    windows: List[Window] = Field(max_length=2000)


def _cell_inc(cell_updates, cell):
    return cell_updates.setdefault(cell, {
        "n": 0, "sum_rough": 0.0, "events": 0, "swerves": 0, "crawl": 0,
        "sum_lat": 0.0, "sum_lon": 0.0, "last_seen": 0,
        "trips": set(), "swerve_trips": set(),
    })


@app.post("/api/ingest")
def ingest(batch: Batch, x_ingest_key: str = Header(default="")):
    if not INGEST_KEY or not hmac.compare_digest(x_ingest_key, INGEST_KEY):
        raise HTTPException(401, "bad ingest key")

    trip = f"{batch.device_id}:{batch.trip_id}"
    stored, cell_updates, skipped = [], {}, {}
    for w in batch.windows:
        raw = w.model_dump()
        s = score_window(raw)
        # A swerve is evidence even when the window itself can't be scored
        # for roughness (e.g. a slow dodge with no lurch).
        if not s.usable and not (w.swerve and s.reason in ("crawl_no_evidence",)):
            skipped[s.reason] = skipped.get(s.reason, 0) + 1
            continue
        cell = geohash.encode(w.lat, w.lon, CELL_PRECISION)
        stored.append({
            **raw, "device_id": batch.device_id, "trip_id": batch.trip_id,
            "vehicle": batch.vehicle, "cell": cell, "region": region_of(w.lat, w.lon),
            "roughness": s.roughness if s.usable else None,
            "condition": s.condition or None, "event": s.event, "mode": s.mode or "swerve_only",
        })
        inc = _cell_inc(cell_updates, cell)
        inc["trips"].add(trip)
        inc["sum_lat"] += w.lat
        inc["sum_lon"] += w.lon
        inc["last_seen"] = max(inc["last_seen"], w.t)
        if s.usable:
            inc["n"] += 1
            inc["sum_rough"] += s.roughness
            inc["events"] += int(s.event)
            inc["crawl"] += int(s.mode == "crawl")
        else:
            inc["pos_only"] = inc.get("pos_only", 0) + 1
        if w.swerve:
            inc["swerves"] += 1
            inc["swerve_trips"].add(trip)

    if stored:
        store.write(stored, cell_updates)
        _cache["data"] = None
    return {"accepted": len(stored), "skipped": skipped}


def _build_cells():
    out = []
    for c in store.list_cells():
        npos = (c.get("n") or 0) + (c.get("pos_only") or 0)
        if not npos:
            continue
        lat, lon = c["sum_lat"] / npos, c["sum_lon"] / npos
        region = region_of(lat, lon)
        n = int(c.get("n") or 0)
        avg = c["sum_rough"] / n if n else None
        trips = len(c.get("trips") or [])
        swerve_trips = len(c.get("swerve_trips") or [])
        out.append({
            "id": c["id"], "lat": round(lat, 6), "lon": round(lon, 6), "region": region,
            "n": n, "roughness": round(avg, 3) if avg is not None else None,
            "condition": classify(avg, region) if avg is not None else None,
            "events": int(c.get("events") or 0), "crawl": int(c.get("crawl") or 0),
            "swerves": int(c.get("swerves") or 0), "trips": trips, "swerve_trips": swerve_trips,
            "hotspot": is_hotspot(trips, swerve_trips), "last_seen": c.get("last_seen"),
        })
    return out


@app.get("/api/cells")
def cells():
    now = time.time()
    if _cache["data"] is None or now - _cache["checked"] > VERSION_CHECK_S:
        v = store.version()
        if _cache["data"] is None or v != _cache["version"]:
            _cache["data"] = _build_cells()
            _cache["version"] = v
        _cache["checked"] = now
    return JSONResponse(_cache["data"], headers={"Cache-Control": "public, max-age=30"})


@app.get("/api/config")
def config():
    return JSONResponse(public_config(), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/health")  # /healthz is reserved by Cloud Run's frontend
def health():
    return {"ok": True}


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/record")
def record():
    return FileResponse(STATIC / "record.html")


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
