"""Road roughness scoring from 1-second sensor windows.

Each window is summarised on the phone from ~50-100 Hz accelerometer data:
the gravity vector is estimated with a low-pass filter and the acceleration
is projected onto it, so readings don't depend on how the phone is mounted.

The server turns a window into a speed-normalised roughness value and a
condition class. Thresholds are heuristics for a first pilot and are meant
to be tuned against labelled drives (see README).
"""

from dataclasses import dataclass

MIN_SPEED_MS = 2.0          # below ~7 km/h readings are dominated by handling noise
MAX_GPS_ACCURACY_M = 35.0   # drop windows with poor position fixes
REF_SPEED_KMH = 30.0        # roughness is normalised to this speed

# Speed-normalised vertical acceleration std (m/s^2) -> class
THRESHOLDS = [
    (0.6, "good"),
    (1.2, "fair"),
    (2.5, "poor"),
]
WORST = "very_poor"

EVENT_PEAK_MS2 = 4.5        # single jolt this strong is reported as an event
EVENT_PEAK_TO_STD = 3.0     # ...if it also stands well clear of the background


@dataclass
class Scored:
    usable: bool
    roughness: float = 0.0
    condition: str = ""
    event: bool = False
    reason: str = ""


def speed_factor(speed_ms: float) -> float:
    """Vertical response grows roughly with sqrt(speed) on random roughness."""
    kmh = max(speed_ms * 3.6, 10.0)
    return (REF_SPEED_KMH / kmh) ** 0.5


def classify(roughness: float) -> str:
    for limit, label in THRESHOLDS:
        if roughness < limit:
            return label
    return WORST


def score_window(w: dict) -> Scored:
    speed = float(w.get("speed") or 0.0)
    acc = w.get("acc")
    if w.get("lat") is None or w.get("lon") is None:
        return Scored(False, reason="no_position")
    if acc is not None and float(acc) > MAX_GPS_ACCURACY_M:
        return Scored(False, reason="poor_gps")
    if speed < MIN_SPEED_MS:
        return Scored(False, reason="too_slow")
    if int(w.get("n") or 0) < 10:
        return Scored(False, reason="too_few_samples")

    z_std = float(w.get("z_std") or 0.0)
    z_peak = float(w.get("z_peak") or 0.0)
    roughness = round(z_std * speed_factor(speed), 3)
    event = z_peak >= EVENT_PEAK_MS2 and z_peak >= EVENT_PEAK_TO_STD * max(z_std, 0.3)
    return Scored(True, roughness, classify(roughness), event)
