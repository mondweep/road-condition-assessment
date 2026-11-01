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
from .scoring import classify, score_window
from .store import make_store

STATIC = Path(__file__).parent / "static"
INGEST_KEY = os.environ.get("INGEST_KEY", "")
CELL_PRECISION = 8

app = FastAPI(title="Road Condition Assessment")
store = make_store()
_cells_cache = {"t": 0.0, "data": None}


class Window(BaseModel):
    t: float                      # epoch ms at window end
    lat: Optional[float] = None
    lon: Optional[float] = None
    acc: Optional[float] = None   # GPS accuracy, m
    speed: Optional[float] = None # m/s
    heading: Optional[float] = None
    n: int = 0                    # accelerometer samples in window
    z_std: float = 0.0            # std of vertical accel, m/s^2
    z_peak: float = 0.0           # max |vertical accel|, m/s^2
    z_p2p: float = 0.0            # peak-to-peak vertical accel
    dv: Optional[float] = None    # speed change over window, m/s


class Batch(BaseModel):
    device_id: str = Field(max_length=64)
    trip_id: str = Field(max_length=64)
    vehicle: Optional[str] = Field(default=None, max_length=64)
    windows: List[Window] = Field(max_length=2000)


@app.post("/api/ingest")
def ingest(batch: Batch, x_ingest_key: str = Header(default="")):
    if not INGEST_KEY or not hmac.compare_digest(x_ingest_key, INGEST_KEY):
        raise HTTPException(401, "bad ingest key")

    stored, cell_updates, skipped = [], {}, {}
    for w in batch.windows:
        raw = w.model_dump()
        s = score_window(raw)
        if not s.usable:
            skipped[s.reason] = skipped.get(s.reason, 0) + 1
            continue
        cell = geohash.encode(w.lat, w.lon, CELL_PRECISION)
        stored.append({
            **raw, "device_id": batch.device_id, "trip_id": batch.trip_id,
            "vehicle": batch.vehicle, "cell": cell, "roughness": s.roughness,
            "condition": s.condition, "event": s.event,
        })
        inc = cell_updates.setdefault(cell, {"n": 0, "sum_rough": 0.0, "events": 0,
                                             "sum_lat": 0.0, "sum_lon": 0.0, "last_seen": 0})
        inc["n"] += 1
        inc["sum_rough"] += s.roughness
        inc["events"] += int(s.event)
        inc["sum_lat"] += w.lat
        inc["sum_lon"] += w.lon
        inc["last_seen"] = max(inc["last_seen"], w.t)

    if stored:
        store.write(stored, cell_updates)
        _cells_cache["data"] = None
    return {"accepted": len(stored), "skipped": skipped}


@app.get("/api/cells")
def cells():
    if _cells_cache["data"] is None or time.time() - _cells_cache["t"] > 30:
        out = []
        for c in store.list_cells():
            n = c.get("n") or 0
            if not n:
                continue
            avg = c["sum_rough"] / n
            out.append({
                "id": c["id"], "lat": round(c["sum_lat"] / n, 6), "lon": round(c["sum_lon"] / n, 6),
                "n": int(n), "roughness": round(avg, 3), "condition": classify(avg),
                "events": int(c.get("events") or 0), "last_seen": c.get("last_seen"),
            })
        _cells_cache.update(t=time.time(), data=out)
    return JSONResponse(_cells_cache["data"], headers={"Cache-Control": "public, max-age=30"})


@app.get("/health")  # /healthz is reserved by Cloud Run's frontend
def healthz():
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
                        headers={"Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
