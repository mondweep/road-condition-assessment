"""Storage: Firestore in production, in-memory for tests and local runs.

Collections
  windows/{auto}   one doc per usable 1-s window (raw evidence)
  cells/{geohash8} running aggregate per ~38 m road cell (what the map shows)
"""

import os
import time
from collections import defaultdict


class MemoryStore:
    def __init__(self):
        self.windows = []
        self.cells = defaultdict(lambda: defaultdict(float))

    def write(self, windows, cell_updates):
        self.windows.extend(windows)
        for cell_id, inc in cell_updates.items():
            c = self.cells[cell_id]
            for k, v in inc.items():
                if k == "last_seen":
                    c[k] = max(c.get(k, 0), v)
                else:
                    c[k] += v

    def list_cells(self, limit=20000):
        return [{"id": k, **v} for k, v in list(self.cells.items())[:limit]]


class FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        self._fs = firestore
        self.db = firestore.Client()

    def write(self, windows, cell_updates):
        ops = []
        for w in windows:
            ops.append(("set", self.db.collection("windows").document(), w))
        for cell_id, inc in cell_updates.items():
            data = {k: (v if k == "last_seen" else self._fs.Increment(v)) for k, v in inc.items()}
            ops.append(("merge", self.db.collection("cells").document(cell_id), data))
        for i in range(0, len(ops), 450):  # Firestore batch limit is 500
            batch = self.db.batch()
            for kind, ref, data in ops[i:i + 450]:
                batch.set(ref, data, merge=(kind == "merge"))
            batch.commit()

    def list_cells(self, limit=20000):
        out = []
        for doc in self.db.collection("cells").limit(limit).stream():
            out.append({"id": doc.id, **doc.to_dict()})
        return out


def make_store():
    if os.environ.get("STORE", "firestore") == "memory":
        return MemoryStore()
    return FirestoreStore()


def now() -> float:
    return time.time()
