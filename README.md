# Road Condition Assessment

Crowd-sensed road roughness for rural roads (pilot: Assam). A phone mounted in any vehicle records motion and GPS while driving; the server scores each ~40 m stretch of road and shows it on a public map.

## How it works

1. **Recorder (`/record`)** – a web app (PWA) on the phone.
   - Reads the accelerometer at ~50–100 Hz. It estimates the gravity vector with a low-pass filter and projects acceleration onto it, so the phone can be mounted at any angle.
   - Summarises each second into a *window*: vertical-acceleration std, peak, peak-to-peak, GPS position, speed and speed change.
   - Buffers windows on the phone (up to ~7 h) and uploads every 20 s when there's signal.
   - Keeps the screen awake. Browsers pause sensors when the screen locks.
2. **Ingest API (`POST /api/ingest`)** – protected by the `X-Ingest-Key` header.
   - Drops unusable windows: slower than ~7 km/h, GPS accuracy worse than 35 m, or too few samples.
   - Scores the rest and aggregates them into geohash-8 cells (~38 × 19 m) in Firestore.
3. **Map (`/`)** – public Leaflet/OpenStreetMap view.
   - Colours each cell good, fair, poor or very poor.
   - Rings cells that had sharp isolated jolts (likely potholes).

## Scoring (`app/scoring.py`)

- `roughness = z_std × sqrt(30 / max(speed_kmh, 10))`. This is vertical shaking normalised to 30 km/h, since the same road shakes more at higher speed.
- Condition classes:

  | Roughness (m/s²) | Class |
  |---|---|
  | < 0.6 | good |
  | < 1.2 | fair |
  | < 2.5 | poor |
  | otherwise | very poor |

- **Event:** a peak ≥ 4.5 m/s² that is also ≥ 3× the window's std.

These thresholds are **starting heuristics**. Tune them on labelled drives: note the true condition of known stretches, then compare. Vehicle suspension matters too; the `vehicle` field is stored so per-vehicle calibration can be added.

## Run locally

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
STORE=memory INGEST_KEY=dev uvicorn app.main:app --reload
```

## Deploy (Cloud Run, asia-south1)

```bash
gcloud run deploy road-condition --source . --region asia-south1 \
  --allow-unauthenticated --set-env-vars INGEST_KEY=<secret>
```

The Cloud Run service account needs `roles/datastore.user` on the project.

## Data

- `windows` – every usable 1-second window, with device, trip and vehicle. This is the raw evidence, kept for re-scoring.
- `cells` – running sums per geohash-8 cell, used by the map.

## Next steps

- Labelled ground truth, e.g. a dashcam frame captured on each jolt.
- Per-vehicle calibration.
- Snap cells to road segments (OSM ways).
- Multi-pass confidence scores.
- A native app for screen-off recording.
