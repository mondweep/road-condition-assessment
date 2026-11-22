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
        self._blocked = set()
        self.devices = {}
        self.users = {}

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

    def blocked(self):
        return set(self._blocked)

    def set_blocked(self, device_id, blocked, note=""):
        (self._blocked.add if blocked else self._blocked.discard)(device_id)

    def get_device(self, device_id):
        return self.devices.get(device_id)

    def add_trial(self, device_id, seconds):
        d = self.devices.setdefault(device_id, {})
        d["trial_used"] = d.get("trial_used", 0) + seconds

    def link_user(self, device_id, user):
        self.devices.setdefault(device_id, {})["user"] = user["email"]
        u = self.users.setdefault(user["email"], {**user, "devices": []})
        u.update({k: v for k, v in user.items()})
        if device_id not in u["devices"]:
            u["devices"].append(device_id)

    def list_users(self):
        return list(self.users.values())

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

    def blocked(self):
        return {d.id for d in self.db.collection("blocked_devices").stream()}

    def set_blocked(self, device_id, blocked, note=""):
        ref = self.db.collection("blocked_devices").document(device_id)
        if blocked:
            ref.set({"note": note, "at": self._fs.SERVER_TIMESTAMP})
        else:
            ref.delete()

    def get_device(self, device_id):
        d = self.db.collection("devices").document(device_id).get()
        return d.to_dict() if d.exists else None

    def add_trial(self, device_id, seconds):
        self.db.collection("devices").document(device_id).set(
            {"trial_used": self._fs.Increment(seconds), "last_upload": self._fs.SERVER_TIMESTAMP}, merge=True)

    def link_user(self, device_id, user):
        fs = self._fs
        self.db.collection("devices").document(device_id).set({"user": user["email"]}, merge=True)
        self.db.collection("users").document(user["email"]).set(
            {**user, "devices": fs.ArrayUnion([device_id]), "last_signin": fs.SERVER_TIMESTAMP}, merge=True)

    def list_users(self):
        return [{k: v for k, v in d.to_dict().items() if k != "last_signin"} for d in self.db.collection("users").stream()]

    def device_summary(self, limit=200):
        """Recent uploaders: windows per device from the last 5,000 stored windows."""
        q = self.db.collection("windows").order_by("t", direction=self._fs.Query.DESCENDING).limit(5000)
        out = {}
        for d in q.select(["device_id", "vehicle", "t"]).stream():
            x = d.to_dict(); k = x.get("device_id")
            o = out.setdefault(k, {"device_id": k, "vehicle": x.get("vehicle"), "windows": 0, "last_t": x.get("t")})
            o["windows"] += 1
        return sorted(out.values(), key=lambda o: -(o["last_t"] or 0))[:limit]

    def version(self):
        doc = self._meta.get()
        return (doc.to_dict() or {}).get("version", 0) if doc.exists else 0

    def list_cells(self, limit=50000):
        return [{"id": d.id, **d.to_dict()} for d in self.db.collection("cells").limit(limit).stream()]


def make_store(kind: str):
    return MemoryStore() if kind == "memory" else FirestoreStore()
