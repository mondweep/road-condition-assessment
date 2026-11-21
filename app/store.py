"""Storage: Firestore in production, in-memory for tests and local runs.

Collections
  windows/{auto}    one doc per usable 1-s window (raw evidence, for re-scoring)
  cells/{geohash8}  running aggregate per ~38 m road cell (what the map shows)
  meta/state        a version counter bumped on every ingest, so the map API can
                    serve its cache after a single read instead of re-reading
                    every cell (keeps Firestore inside the free daily quota)
"""

from collections import defaultdict

SET_FIELDS = ("trips", "swerve_trips")
MAX_FIELDS = ("last_seen",)


class MemoryStore:
    def __init__(self):
        self.windows = []
        self.cells = defaultdict(dict)
        self._version = 0

    def write(self, windows, cell_updates):
        self.windows.extend(windows)
        for cell_id, inc in cell_updates.items():
            c = self.cells[cell_id]
            for k, v in inc.items():
                if k in SET_FIELDS:
                    c[k] = sorted(set(c.get(k, [])) | set(v))
                elif k in MAX_FIELDS:
                    c[k] = max(c.get(k, 0), v)
                else:
                    c[k] = c.get(k, 0) + v
        self._version += 1

    def version(self):
        return self._version

    def list_cells(self, limit=50000):
        return [{"id": k, **v} for k, v in list(self.cells.items())[:limit]]


class FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        self._fs = firestore
        self.db = firestore.Client()
        self._meta = self.db.collection("meta").document("state")

    def write(self, windows, cell_updates):
        fs = self._fs
        ops = [("set", self.db.collection("windows").document(), w) for w in windows]
        for cell_id, inc in cell_updates.items():
            data = {}
            for k, v in inc.items():
                if k in SET_FIELDS:
                    if v:
                        data[k] = fs.ArrayUnion(sorted(v))
                elif k in MAX_FIELDS:
                    data[k] = v
                else:
                    data[k] = fs.Increment(v)
            ops.append(("merge", self.db.collection("cells").document(cell_id), data))
        for i in range(0, len(ops), 450):  # Firestore batch limit is 500
            batch = self.db.batch()
            for kind, ref, data in ops[i:i + 450]:
                batch.set(ref, data, merge=(kind == "merge"))
            batch.commit()
        self._meta.set({"version": fs.Increment(1)}, merge=True)

    def version(self):
        doc = self._meta.get()
        return (doc.to_dict() or {}).get("version", 0) if doc.exists else 0

    def list_cells(self, limit=50000):
        return [{"id": d.id, **d.to_dict()} for d in self.db.collection("cells").limit(limit).stream()]


def make_store(kind: str):
    return MemoryStore() if kind == "memory" else FirestoreStore()
