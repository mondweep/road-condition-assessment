import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, geohash, identity
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
TRIAL_SECONDS = int(os.environ.get("TRIAL_SECONDS", 15 * 60))   # free recording before sign-in
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


class RegisterReq(BaseModel):
    captcha: str = Field(default="", max_length=4000)


@app.post("/api/register")
def register(request: Request, req: Optional[RegisterReq] = None):
    """Gives a phone its own device id and upload token, after a bot check."""
    if not limiter.allow_register(_client_ip(request)):
        raise HTTPException(429, "too many new devices from this network; try again later")
    if not identity.human_ok((req or RegisterReq()).captcha, "register"):
        raise HTTPException(403, "bot check failed; reload the page and try again")
    device_id = access.new_device_id()
    return {"device_id": device_id, "token": access.token_for(device_id), "trial_left_s": TRIAL_SECONDS}


def _device(x_device_id: str, x_device_token: str) -> str:
    if not access.token_ok(x_device_id, x_device_token):
        raise HTTPException(401, "unknown device; register again")
    return x_device_id


def _status(device_id: str, dev: Optional[dict]) -> dict:
    dev = dev or {}
    user = dev.get("user")
    return {"signed_in": user, "trial_left_s": None if user else max(0, TRIAL_SECONDS - int(dev.get("trial_used", 0)))}


@app.get("/api/me")
def me(x_device_id: str = Header(default=""), x_device_token: str = Header(default="")):
    d = _device(x_device_id, x_device_token)
    return _status(d, store.get_device(d))


class SignInReq(BaseModel):
    credential: str = Field(max_length=8000)


@app.post("/api/signin")
def signin(req: SignInReq, x_device_id: str = Header(default=""), x_device_token: str = Header(default="")):
    """Links this phone to a verified Google account; removes the trial limit."""
    d = _device(x_device_id, x_device_token)
    try:
        user = identity.verify_google(req.credential)
    except ValueError as e:
        raise HTTPException(401, f"sign-in failed: {e}")
    if blocklist.is_blocked("user:" + user["email"]):
        raise HTTPException(403, "this account has been blocked")
    store.link_user(d, user)
    return {**_status(d, {"user": user["email"]}), "name": user["name"]}


@app.post("/api/ingest")
def ingest(batch: Batch, x_ingest_key: str = Header(default=""), x_device_token: str = Header(default="")):
    admin = access.admin_ok(x_ingest_key)
    if not admin and not access.token_ok(batch.device_id, x_device_token):
        raise HTTPException(401, "unknown device; register again")
    if blocklist.is_blocked(batch.device_id):
        raise HTTPException(403, "this device has been blocked")
    windows, status = batch.windows, {"signed_in": None, "trial_left_s": None}
    if not admin:
        dev = store.get_device(batch.device_id) or {}
        status = _status(batch.device_id, dev)
        if status["signed_in"]:
            if blocklist.is_blocked("user:" + status["signed_in"]):
                raise HTTPException(403, "this account has been blocked")
        else:
            left = status["trial_left_s"]
            if left <= 0:
                raise HTTPException(402, "trial_over")
            windows = windows[:left]
        why = limiter.allow_upload(batch.device_id, len(windows))
        if why:
            raise HTTPException(429, why)
        if not status["signed_in"]:
            store.add_trial(batch.device_id, len(windows))
            status["trial_left_s"] -= len(windows)
    now_ms = time.time() * 1000

    trip = f"{batch.device_id}:{batch.trip_id}"
    stored, cell_updates, skipped = [], {}, {}
    for w in windows:
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
    out = {"accepted": len(stored), "skipped": skipped}
    if not admin:
        out.update(status, unsent=len(batch.windows) - len(windows))
    return out


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
    device_id: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=254)
    blocked: bool = True
    note: str = Field(default="", max_length=200)


@app.post("/api/admin/block")
def admin_block(req: BlockReq, x_ingest_key: str = Header(default="")):
    if not access.admin_ok(x_ingest_key):
        raise HTTPException(401, "admin key required")
    key = ("user:" + req.email.lower()) if req.email else req.device_id
    if not key:
        raise HTTPException(400, "give a device_id or an email")
    store.set_blocked(key, req.blocked, req.note)
    blocklist.refresh()
    return {"blocked_id": key, "blocked": req.blocked}


@app.get("/api/admin/devices")
def admin_devices(x_ingest_key: str = Header(default="")):
    if not access.admin_ok(x_ingest_key):
        raise HTTPException(401, "admin key required")
    return {"devices": store.device_summary() if hasattr(store, "device_summary") else [],
            "users": store.list_users(), "blocked": sorted(store.blocked())}


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
    cfg = {**public_config(), "trial_seconds": TRIAL_SECONDS,
           "recaptcha_site_key": identity.RECAPTCHA_SITE_KEY, "google_client_id": identity.GOOGLE_CLIENT_ID}
    return JSONResponse(cfg, headers={"Cache-Control": "public, max-age=300"})


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
