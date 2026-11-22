import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, geohash
from .scoring import classify, is_hotspot, public_config, region_of, score_window
from .store import make_store

STATIC = Path(__file__).parent / "static"
CELL_PRECISION = 8
VERSION_CHECK_S = 20

app = FastAPI(title="Road Condition Assessment")


@app.middleware("http")
async def revalidate_pages(request, call_next):
    # Make browsers check for a new version on every load (cheap: ETag/304),
    # so phones never keep running an old recorder or map.
    resp = await call_next(request)
    path = request.url.path
    if path in ("/", "/record", "/sw.js") or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
store = make_store(os.environ.get("STORE", "firestore"))
limiter = access.Limiter()
blocklist = access.Blocklist(store)
MAX_SPEED_MS = 55.0             # ~200 km/h: faster is a GPS glitch or fake data
MAX_FUTURE_MS = 5 * 60 * 1000
MAX_AGE_MS = 14 * 24 * 3600 * 1000
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


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@app.post("/api/register")
def register(request: Request):
    """Gives a phone its own device id and upload token. No sign-up needed."""
    if not limiter.allow_register(_client_ip(request)):
        raise HTTPException(429, "too many new devices from this network; try again later")
    device_id = access.new_device_id()
    return {"device_id": device_id, "token": access.token_for(device_id)}


@app.post("/api/ingest")
def ingest(batch: Batch, x_ingest_key: str = Header(default=""), x_device_token: str = Header(default="")):
    admin = access.admin_ok(x_ingest_key)
    if not admin and not access.token_ok(batch.device_id, x_device_token):
        raise HTTPException(401, "unknown device; register again")
    if blocklist.is_blocked(batch.device_id):
        raise HTTPException(403, "this device has been blocked")
    if not admin:
        why = limiter.allow_upload(batch.device_id, len(batch.windows))
        if why:
            raise HTTPException(429, why)
    now_ms = time.time() * 1000

    trip = f"{batch.device_id}:{batch.trip_id}"
    stored, cell_updates, skipped = [], {}, {}
    for w in batch.windows:
        if w.t > now_ms + MAX_FUTURE_MS or (not admin and w.t < now_ms - MAX_AGE_MS) or (w.speed or 0) > MAX_SPEED_MS:
            skipped["implausible"] = skipped.get("implausible", 0) + 1
            continue
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


class BlockReq(BaseModel):
    device_id: str = Field(max_length=64)
    blocked: bool = True
    note: str = Field(default="", max_length=200)


@app.post("/api/admin/block")
def admin_block(req: BlockReq, x_ingest_key: str = Header(default="")):
    if not access.admin_ok(x_ingest_key):
        raise HTTPException(401, "admin key required")
    store.set_blocked(req.device_id, req.blocked, req.note)
    blocklist.refresh()
    return {"device_id": req.device_id, "blocked": req.blocked}


@app.get("/api/admin/devices")
def admin_devices(x_ingest_key: str = Header(default="")):
    if not access.admin_ok(x_ingest_key):
        raise HTTPException(401, "admin key required")
    return {"devices": store.device_summary() if hasattr(store, "device_summary") else [],
            "blocked": sorted(store.blocked())}


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
                        headers={"Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
