# Road Condition Assessment

Crowd-sensed road roughness for rural roads (pilot: Assam). A phone mounted in any vehicle records motion and GPS while driving; the server scores each ~40 m stretch of road and shows it on a public map.

## How it works

The app is vibration- and GPS-based. It never uses the camera.

1. **Recorder (`/record`)**: a web app (PWA) on the phone, with a first-run guide.
   - Reads the accelerometer and gyroscope at ~50–100 Hz. It projects motion onto the gravity vector, so the phone can be mounted at any angle.
   - Each second it summarises a *window*: vertical shaking (std, peak), distinct lurches, yaw rate, GPS position, speed and speed change.
   - Detects **swerves** on the phone (`static/signals.js`). A swerve is a quick out-and-back steer on a straight road, ending on the same heading, with a small total swing. Bends and S-bends are rejected.
   - **Privacy trimming:** nothing is uploaded until the phone is 300 m in a straight line from the trip start. The last ~400 m of driving are held back and discarded when the trip stops.
   - Buffers windows on the phone (up to ~7 h) and uploads every 20 s when there's signal. Keeps the screen awake.
2. **Access:** 15 minutes of free recording with no sign-up (bot-checked with reCAPTCHA Enterprise), then **Sign in with Google**. Recording stays free and unlimited (within rate limits) once signed in. Readings made after the trial wait on the phone until the driver signs in.
   - **Device registration (no sign-up):** the first time a phone opens `/record`, it calls `POST /api/register` and gets its own device ID and upload token. The token is an HMAC of the ID, so the server stores nothing.
   - Safeguards: at most 20 uploads/min and 4,500 windows/hour per device, and 10 new devices/hour per IP address.
   - Implausible readings are dropped: timestamps in the future or older than 14 days, and speeds over 200 km/h.
   - The shared `INGEST_KEY` still works (`?key=` link) and is the **admin key**.
3. **Ingest API (`POST /api/ingest`)**: accepts an `X-Device-Token` header or the admin `X-Ingest-Key` header. Scores each window and aggregates it into geohash-8 cells (~38 × 19 m) in Firestore.
4. **Map (`/`)**: public Leaflet/OpenStreetMap view.
   - Region buttons (Assam & NE India / UK / All).
   - "Near me" asks for location only when tapped. The last view is remembered.
   - "How this works" explains the method and its limits.

## Scoring (`app/scoring.py`)

Scoring mode depends on speed:

| Speed | Mode | Roughness |
|---|---|---|
| < ~3 km/h | stopped | skipped (GPS speed is noise) |
| ~3–10 km/h | crawl | `1.2 × lurches/s` (capped at 4). A crawl with no lurches is skipped as likely traffic. |
| ≥ ~10 km/h | normal | `z_std × sqrt(30 / max(km/h, 10))`, i.e. vertical shaking normalised to 30 km/h |

Thresholds are **per region**. Classes are good / fair / poor / very poor at these roughness values:

| Region | Fair from | Poor from | Very poor from | Sharp-jolt peak |
|---|---|---|---|---|
| UK | 0.35 | 0.7 | 1.4 | 3.5 m/s² |
| Assam & NE India (and default) | 0.6 | 1.2 | 2.5 | 4.5 m/s² |

Cell-level signals:

- **Sharp jolt:** a peak above the region's threshold that is also ≥ 3× the window std. Likely a pothole that was hit, or a speed breaker.
- **Swerve hotspot:** ≥ 2 separate trips swerved in the cell, and they make up ≥ 30% of the trips through it. A single swerve (a cow, an overtake) is noise.

All thresholds are **starting heuristics**. Tune them on labelled drives by noting the true condition of known stretches. The raw windows are stored, so everything can be re-scored.

### Known limits

- Potholes that every driver steers around are only visible through swerves.
- Speed breakers look like potholes.
- Tight hairpins could occasionally pass as swerves.
- Vehicle suspension changes readings; the `vehicle` field is stored for later per-vehicle calibration.
- Browsers pause sensors when the screen locks.

## Admin

```bash
# recent uploaders and the blocklist
curl -H "X-Ingest-Key: $KEY" $URL/api/admin/devices
# block (or unblock with "blocked": false) a phone, or every phone of a signed-in account ({"email": ...})
curl -X POST -H "X-Ingest-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"device_id":"p-XXXX","note":"spam"}' $URL/api/admin/block
```

## Run locally

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
node --test tests/signals.test.js
STORE=memory INGEST_KEY=dev uvicorn app.main:app --reload
```

## Deploy (Cloud Run, asia-south1)

```bash
gcloud run deploy road-condition --source . --region asia-south1 \
  --allow-unauthenticated --set-env-vars INGEST_KEY=<admin secret>,TOKEN_SECRET=<random secret>,RECAPTCHA_SITE_KEY=<key>,GOOGLE_CLIENT_ID=<oauth web client id>
```

The Cloud Run service account needs `roles/datastore.user` on the project.

## Data

- `windows` – every usable 1-second window, with device, trip and vehicle. This is the raw evidence, kept for re-scoring.
- `cells` – running sums per geohash-8 cell, plus the trip IDs seen and the trip IDs that swerved there.
- `meta/state` – a version counter bumped on each ingest. The map API re-reads all cells only when it changes, which keeps Firestore reads inside the free quota.

## Next steps

- Labelled ground truth, e.g. a dashcam frame captured on each jolt.
- Per-vehicle calibration.
- Snap cells to road segments (OSM ways).
- Multi-pass confidence scores.
- A native app for screen-off recording.
