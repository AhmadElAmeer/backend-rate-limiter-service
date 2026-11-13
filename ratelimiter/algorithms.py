"""Four rate-limiting algorithms behind one interface.

Every limiter answers: "client `key` wants to make a request now; allow it?"
under the policy "at most `limit` requests per `window` seconds".

They differ in how strictly they enforce that policy, how much state they
keep per client, and how they treat bursts:

* FixedWindow          - one counter per clock-aligned window. Cheapest, but
                         allows up to 2 x limit across a window boundary.
* SlidingWindowLog     - remembers every timestamp in the last window. Exact,
                         but stores up to `limit` numbers per client.
* SlidingWindowCounter - weights the previous window's count by how much of it
                         still overlaps the sliding window. O(1) memory, close
                         to exact.
* TokenBucket          - tokens refill at limit/window per second up to a
                         capacity of `limit`. Allows bursts, smooths the
                         long-run rate.

The clock is injectable so tests and simulations are deterministic.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

Clock = Callable[[], float]

# Small slack added to computed wait times so that a client retrying after
# exactly `retry_after` seconds is not refused by floating-point rounding.
_EPS = 1e-6


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int         # requests still allowed right now
    retry_after: float     # seconds until a request would be allowed (0 if allowed)


class RateLimiter:
    """Base class: per-key state, locking, and eviction of idle clients."""

    name = "base"

    def __init__(self, limit: int, window: float, clock: Clock = time.monotonic,
                 sweep_every: int = 1000):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if not (isinstance(window, (int, float)) and math.isfinite(window) and window > 0):
            raise ValueError("window must be a positive number of seconds")
        self.limit = limit
        self.window = float(window)
        self.clock = clock
        self._state: dict[str, object] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._calls = 0
        self._sweep_every = sweep_every

    def allow(self, key: str) -> Decision:
        with self._lock:
            now = self.clock()
            self._last_seen[key] = now
            decision = self._allow(key, now)
            self._calls += 1
            if self._calls % self._sweep_every == 0:
                self._sweep(now)
            return decision

    def _allow(self, key: str, now: float) -> Decision:  # pragma: no cover
        raise NotImplementedError

    def _sweep(self, now: float) -> None:
        """Forget clients idle for more than two windows. After that long
        every algorithm would treat them exactly like a new client, so
        dropping their state changes no decision and bounds memory."""
        cutoff = now - 2 * self.window
        for key in [k for k, t in self._last_seen.items() if t < cutoff]:
            del self._last_seen[key]
            self._state.pop(key, None)

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._state)

    def stored_numbers(self, key: str) -> int:
        """How many numbers this limiter keeps for one client (for comparing
        memory use between algorithms)."""
        raise NotImplementedError  # pragma: no cover


class FixedWindow(RateLimiter):
    name = "fixed_window"

    def _allow(self, key, now):
        index = math.floor(now / self.window)
        start, count = self._state.get(key, (index, 0))
        if start != index:
            start, count = index, 0
        if count < self.limit:
            count += 1
            self._state[key] = (start, count)
            return Decision(True, self.limit, self.limit - count, 0.0)
        self._state[key] = (start, count)
        wait = (index + 1) * self.window - now
        return Decision(False, self.limit, 0, wait + _EPS)

    def stored_numbers(self, key):
        return 2 if key in self._state else 0


class SlidingWindowLog(RateLimiter):
    name = "sliding_log"

    def _allow(self, key, now):
        log = self._state.setdefault(key, deque())
        while log and log[0] <= now - self.window:
            log.popleft()
        if len(log) < self.limit:
            log.append(now)
            return Decision(True, self.limit, self.limit - len(log), 0.0)
        # The oldest request leaves the window at log[0] + window.
        return Decision(False, self.limit, 0, log[0] + self.window - now + _EPS)

    def stored_numbers(self, key):
        return len(self._state.get(key, ()))


class SlidingWindowCounter(RateLimiter):
    name = "sliding_counter"

    def _counts(self, key, now):
        index = math.floor(now / self.window)
        start, prev, curr = self._state.get(key, (index, 0, 0))
        if start == index - 1:            # moved into the next window
            start, prev, curr = index, curr, 0
        elif start != index:              # idle for 2+ windows
            start, prev, curr = index, 0, 0
        return start, prev, curr

    def _estimate(self, prev, curr, start, now):
        overlap = 1.0 - (now - start * self.window) / self.window
        return prev * overlap + curr

    def _allow(self, key, now):
        start, prev, curr = self._counts(key, now)
        estimate = self._estimate(prev, curr, start, now)
        if estimate + 1 <= self.limit + 1e-9:
            curr += 1
            self._state[key] = (start, prev, curr)
            remaining = max(0, math.floor(self.limit - estimate - 1 + 1e-9))
            return Decision(True, self.limit, remaining, 0.0)
        self._state[key] = (start, prev, curr)
        return Decision(False, self.limit, 0, self._retry_after(prev, curr, start, now))

    def _retry_after(self, prev, curr, start, now):
        """Earliest time at which estimate + 1 <= limit."""
        w = self.window
        room = self.limit - 1 - curr       # how far the weighted previous count must drop
        if room >= 0 and prev > 0:
            # prev * (1 - f) <= room  ->  f >= 1 - room / prev
            f = 1.0 - room / prev
            t = start * w + f * w
        else:
            # Need the next window: then prev' = curr, curr' = 0, and
            # curr * (1 - f) <= limit - 1.
            f = 1.0 - (self.limit - 1) / curr if curr > 0 else 0.0
            t = (start + 1) * w + max(0.0, f) * w
        return max(t - now, 0.0) + _EPS

    def stored_numbers(self, key):
        return 3 if key in self._state else 0


class TokenBucket(RateLimiter):
    name = "token_bucket"

    def __init__(self, limit, window, clock=time.monotonic, sweep_every=1000):
        super().__init__(limit, window, clock, sweep_every)
        self.rate = self.limit / self.window     # tokens per second

    def _allow(self, key, now):
        tokens, last = self._state.get(key, (float(self.limit), now))
        tokens = min(float(self.limit), tokens + (now - last) * self.rate)
        if tokens >= 1.0 - 1e-9:
            tokens -= 1.0
            self._state[key] = (tokens, now)
            return Decision(True, self.limit, math.floor(tokens + 1e-9), 0.0)
        self._state[key] = (tokens, now)
        return Decision(False, self.limit, 0, (1.0 - tokens) / self.rate + _EPS)

    def stored_numbers(self, key):
        return 2 if key in self._state else 0


ALGORITHMS: dict[str, type[RateLimiter]] = {
    cls.name: cls for cls in (FixedWindow, SlidingWindowLog, SlidingWindowCounter, TokenBucket)
}


def make_limiter(algorithm: str, limit: int, window: float, clock: Clock = time.monotonic) -> RateLimiter:
    try:
        cls = ALGORITHMS[algorithm]
    except KeyError:
        raise ValueError(f"unknown algorithm {algorithm!r}; choose from {', '.join(ALGORITHMS)}") from None
    return cls(limit, window, clock)
