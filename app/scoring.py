"""Road condition scoring from 1-second sensor windows.

The phone summarises each second of ~50-100 Hz motion data into a window:
vertical shaking (accelerometer projected onto gravity, so mounting angle
doesn't matter), the number of distinct lurches, yaw rate from the gyroscope,
GPS position and speed. It also flags swerves (see static/signals.js).

Two scoring modes, chosen by speed:
  normal  (>= ~10 km/h)  speed-normalised vertical shaking -> roughness
  crawl   (~3-10 km/h)   lurches per second -> roughness on the same scale.
                         A crawl with no lurches is not scored: it is more
                         likely traffic than a good road.
Below ~3 km/h the vehicle is treated as stopped and the window is skipped.

Thresholds differ per region because the vehicles, roads and what counts as
"bad" differ. They are first heuristics, to be tuned against labelled drives.
"""

from dataclasses import dataclass

MIN_SPEED_MS = 0.8          # ~3 km/h; below this GPS speed is mostly noise
CRAWL_SPEED_MS = 2.8        # ~10 km/h; below this use lurch counting
MAX_GPS_ACCURACY_M = 35.0
REF_SPEED_KMH = 30.0
CRAWL_UNIT = 1.2            # roughness per lurch/s while crawling
CRAWL_CAP = 4.0

REGIONS = {
    # name: bounding box (lat_min, lat_max, lon_min, lon_max), thresholds
    "uk": {
        "label": "United Kingdom",
        "bbox": [49.8, 61.0, -8.7, 2.1],
        "thresholds": [0.35, 0.7, 1.4],     # good < 0.35 <= fair < 0.7 <= poor < 1.4 <= very poor
        "event_peak": 3.5,
    },
    "ne_india": {
        "label": "Assam & North East India",
        "bbox": [21.9, 29.5, 89.6, 97.5],
        "thresholds": [0.6, 1.2, 2.5],
        "event_peak": 4.5,
    },
}
DEFAULT_REGION = "other"
DEFAULTS = {"label": "Elsewhere", "bbox": None, "thresholds": [0.6, 1.2, 2.5], "event_peak": 4.5}
CLASSES = ["good", "fair", "poor", "very_poor"]
EVENT_PEAK_TO_STD = 3.0

# A cell is an avoidance hotspot when drivers on several separate trips
# swerved there, and that is a sizeable share of the trips through it.
HOTSPOT_MIN_TRIPS = 2
HOTSPOT_MIN_SHARE = 0.3


@dataclass
class Scored:
    usable: bool
    roughness: float = 0.0
    condition: str = ""
    event: bool = False
    mode: str = ""
    region: str = DEFAULT_REGION
    reason: str = ""


def region_of(lat: float, lon: float) -> str:
    for name, r in REGIONS.items():
        a, b, c, d = r["bbox"]
        if a <= lat <= b and c <= lon <= d:
            return name
    return DEFAULT_REGION


def region_cfg(region: str) -> dict:
    return REGIONS.get(region, DEFAULTS)


def speed_factor(speed_ms: float) -> float:
    """Vertical response grows roughly with sqrt(speed) on random roughness."""
    kmh = max(speed_ms * 3.6, 10.0)
    return (REF_SPEED_KMH / kmh) ** 0.5


def classify(roughness: float, region: str = DEFAULT_REGION) -> str:
    for limit, label in zip(region_cfg(region)["thresholds"], CLASSES):
        if roughness < limit:
            return label
    return CLASSES[-1]


def score_window(w: dict) -> Scored:
    lat, lon = w.get("lat"), w.get("lon")
    if lat is None or lon is None:
        return Scored(False, reason="no_position")
    acc = w.get("acc")
    if acc is not None and float(acc) > MAX_GPS_ACCURACY_M:
        return Scored(False, reason="poor_gps")
    if int(w.get("n") or 0) < 10:
        return Scored(False, reason="too_few_samples")
    speed = float(w.get("speed") or 0.0)
    if speed < MIN_SPEED_MS:
        return Scored(False, reason="stopped")

    region = region_of(lat, lon)
    cfg = region_cfg(region)
    z_std = float(w.get("z_std") or 0.0)
    z_peak = float(w.get("z_peak") or 0.0)

    if speed < CRAWL_SPEED_MS:
        jolts = w.get("jolts")
        if not jolts:
            return Scored(False, reason="crawl_no_evidence")
        roughness = round(min(CRAWL_UNIT * float(jolts), CRAWL_CAP), 3)
        return Scored(True, roughness, classify(roughness, region), False, "crawl", region)

    roughness = round(z_std * speed_factor(speed), 3)
    event = z_peak >= cfg["event_peak"] and z_peak >= EVENT_PEAK_TO_STD * max(z_std, 0.3)
    return Scored(True, roughness, classify(roughness, region), event, "normal", region)


def is_hotspot(trips: int, swerve_trips: int) -> bool:
    return swerve_trips >= HOTSPOT_MIN_TRIPS and trips > 0 and swerve_trips / trips >= HOTSPOT_MIN_SHARE


def public_config() -> dict:
    return {
        "regions": {k: {kk: v[kk] for kk in ("label", "bbox", "thresholds")} for k, v in REGIONS.items()},
        "default": {"thresholds": DEFAULTS["thresholds"]},
        "min_speed_ms": MIN_SPEED_MS,
        "crawl_speed_ms": CRAWL_SPEED_MS,
        "crawl_unit": CRAWL_UNIT,
    }
