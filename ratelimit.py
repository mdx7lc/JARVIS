"""Bounded, thread-safe, in-memory rate limiting (stdlib only).

Fixed-window buckets keyed by (policy, client key). Single-user
loopback deployment: one server process, in-memory state, restart
resets the limiter entirely. This is neither distributed nor
multi-process — never present it as such.

Guarantees:
- time.monotonic() only (immune to wall-clock changes)
- lock protects the small shared state; no sleeps in callers
- bounded memory: at most `max_buckets` buckets (LRU eviction of the
  least-recently-used identity when a flood of new keys appears)
- `hit()` never raises for missing policies (programming error) —
  policies are explicit constants in server.py
"""

import math
import threading
import time
from collections import OrderedDict


class RateLimiter:
    def __init__(self, policies, clock=time.monotonic, max_buckets=256):
        self._policies = dict(policies)  # name -> (limit, window_seconds)
        self._clock = clock
        self._max_buckets = max(1, int(max_buckets))
        self._buckets = OrderedDict()  # (policy, key) -> [start, count]
        self._lock = threading.Lock()

    def add_policy(self, name, limit, window):
        """Configure (or reconfigure) a policy: `limit` hits per `window`
        seconds. Tests use this to install small windows with a fake
        clock instead of sleeping."""
        self._policies[name] = (int(limit), float(window))

    def reset(self):
        """Forget all counters (used by tests between scenarios;
        restarts reset state implicitly)."""
        with self._lock:
            self._buckets.clear()

    def hit(self, policy, key="loopback"):
        """Register one request attempt.

        Returns (allowed, retry_after_seconds): allowed is False with a
        positive integer Retry-After once the bucket's counter exceeds
        the configured limit for the current fixed window.
        """
        if policy not in self._policies:
            raise KeyError(f"unknown rate-limit policy: {policy}")
        limit, window = self._policies[policy]
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get((policy, key))
            if bucket is None or (now - bucket[0]) >= window:
                bucket = [now, 0]
                self._buckets[(policy, key)] = bucket
                self._buckets.move_to_end((policy, key))
                self._evict_locked()
            bucket[1] += 1
            if bucket[1] > limit:
                retry = int(math.ceil(bucket[0] + window - now))
                return False, max(retry, 1)
            return True, 0

    def _evict_locked(self):
        """Keep the bucket map bounded; drop oldest-used identities."""
        while len(self._buckets) > self._max_buckets:
            self._buckets.popitem(last=False)
