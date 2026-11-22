"""Open contribution with safeguards.

- Any phone can register and gets its own device id + token. The token is an
  HMAC of the id, so nothing needs storing to check it.
- Uploads are rate-limited per device (about what real driving produces) and
  registrations per IP address. Limits are per server instance, which is fine:
  they stop floods, not determined attackers.
- Devices can be blocked; the blocklist lives in Firestore and is cached briefly.
- The old shared INGEST_KEY still works and doubles as the admin key.
"""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

ADMIN_KEY = os.environ.get("INGEST_KEY", "")
_SECRET = (os.environ.get("TOKEN_SECRET") or ("rca-token:" + ADMIN_KEY)).encode()

WINDOWS_PER_HOUR = 4500        # an hour of driving is 3,600 one-second windows
REQUESTS_PER_MINUTE = 20       # the app uploads every 20 s; allows catching up after a dead zone
REGISTRATIONS_PER_HOUR_PER_IP = 10
BLOCKLIST_TTL_S = 60


def new_device_id() -> str:
    return "p-" + secrets.token_urlsafe(9)


def token_for(device_id: str) -> str:
    return hmac.new(_SECRET, device_id.encode(), hashlib.sha256).hexdigest()[:32]


def token_ok(device_id: str, token: str) -> bool:
    return bool(device_id and token) and hmac.compare_digest(token_for(device_id), token)


def admin_ok(key: str) -> bool:
    return bool(ADMIN_KEY) and hmac.compare_digest(key or "", ADMIN_KEY)


class Limiter:
    def __init__(self):
        self.windows = defaultdict(deque)   # device -> deque[(t, count)]
        self.requests = defaultdict(deque)  # device -> deque[t]
        self.regs = defaultdict(deque)      # ip -> deque[t]

    def allow_upload(self, device: str, n_windows: int, now: float = None) -> str:
        now = now or time.time()
        rq = self.requests[device]
        while rq and rq[0] < now - 60:
            rq.popleft()
        if len(rq) >= REQUESTS_PER_MINUTE:
            return "too many uploads per minute"
        wq = self.windows[device]
        while wq and wq[0][0] < now - 3600:
            wq.popleft()
        if sum(c for _, c in wq) + n_windows > WINDOWS_PER_HOUR:
            return "too much data this hour"
        rq.append(now)
        wq.append((now, n_windows))
        return ""

    def allow_register(self, ip: str, now: float = None) -> bool:
        now = now or time.time()
        q = self.regs[ip]
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= REGISTRATIONS_PER_HOUR_PER_IP:
            return False
        q.append(now)
        return True


class Blocklist:
    def __init__(self, store):
        self.store = store
        self._ids, self._at = set(), 0.0

    def is_blocked(self, device: str) -> bool:
        if time.time() - self._at > BLOCKLIST_TTL_S:
            self._ids, self._at = set(self.store.blocked()), time.time()
        return device in self._ids

    def refresh(self):
        self._at = 0.0
