"""Replay the same traffic against every algorithm with a simulated clock.

Policy for every run: LIMIT requests per WINDOW seconds. For each algorithm
and traffic pattern we report:

* allowed       - requests let through
* worst window  - most requests allowed in ANY interval of WINDOW seconds
                  (the true policy says this must be <= LIMIT)
* numbers/client - state kept for one client after the run
"""
from __future__ import annotations

import bisect
import random
import time
from dataclasses import dataclass

from .algorithms import ALGORITHMS


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def worst_window(times: list[float], window: float) -> int:
    """Max number of timestamps inside any half-open interval
    (t - window, t]. The policy is enforced on half-open windows: a request
    exactly `window` seconds old no longer counts."""
    best = 0
    for i, t in enumerate(times):
        j = bisect.bisect_right(times, t - window)
        best = max(best, i - j + 1)
    return best


def boundary_burst(limit: int, window: float, rng: random.Random) -> list[float]:
    """A full burst just before a window boundary and another just after,
    repeated. The classic weakness of fixed windows."""
    times = []
    for k in range(1, 21):
        edge = k * 3 * window
        times += [edge - 0.01] * limit + [edge + 0.01] * limit
    return times


def steady_overload(limit: int, window: float, rng: random.Random) -> list[float]:
    """Poisson arrivals at 3x the allowed rate for 100 windows."""
    rate = 3 * limit / window
    t, times = 0.0, []
    while t < 100 * window:
        t += rng.expovariate(rate)
        times.append(t)
    return times


def idle_then_bursts(limit: int, window: float, rng: random.Random) -> list[float]:
    """Quiet periods followed by bursts of 2 x limit requests over 1 s."""
    times = []
    t = 0.0
    for _ in range(30):
        t += rng.uniform(1.0, 3.0) * window
        times += sorted(t + rng.uniform(0, 1.0) for _ in range(2 * limit))
    return times


PATTERNS = {
    "boundary-burst": boundary_burst,
    "steady-3x-overload": steady_overload,
    "idle-then-bursts": idle_then_bursts,
}


@dataclass
class SimResult:
    pattern: str
    algorithm: str
    requests: int
    allowed: int
    worst_window: int
    stored_numbers: int


def simulate(algorithm: str, arrivals: list[float], limit: int, window: float) -> SimResult:
    clock = FakeClock()
    limiter = ALGORITHMS[algorithm](limit, window, clock)
    allowed_times = []
    for t in arrivals:
        clock.t = t
        if limiter.allow("client").allowed:
            allowed_times.append(t)
    return SimResult("", algorithm, len(arrivals), len(allowed_times),
                     worst_window(allowed_times, window), limiter.stored_numbers("client"))


def run_all(limit: int = 100, window: float = 60.0, seed: int = 0) -> list[SimResult]:
    results = []
    for pattern, gen in PATTERNS.items():
        arrivals = sorted(gen(limit, window, random.Random(seed)))
        for name in ALGORITHMS:
            r = simulate(name, arrivals, limit, window)
            r.pattern = pattern
            results.append(r)
    return results


def throughput(algorithm: str, limit: int = 100, window: float = 60.0,
               calls: int = 200_000, clients: int = 1000) -> float:
    """allow() calls per second with a real clock, spread over many clients."""
    limiter = ALGORITHMS[algorithm](limit, window)
    keys = [f"c{i}" for i in range(clients)]
    start = time.perf_counter()
    for i in range(calls):
        limiter.allow(keys[i % clients])
    return calls / (time.perf_counter() - start)


def format_results(results: list[SimResult], limit: int, window: float) -> str:
    lines = [f"policy: {limit} requests per {window:g} s (worst window must be <= {limit})", ""]
    lines.append(f"{'pattern':<20}{'algorithm':<17}{'requests':>9}{'allowed':>9}{'worst window':>14}{'numbers/client':>16}")
    for r in results:
        flag = "  <-- over limit" if r.worst_window > limit else ""
        lines.append(f"{r.pattern:<20}{r.algorithm:<17}{r.requests:>9}{r.allowed:>9}"
                     f"{r.worst_window:>14}{r.stored_numbers:>16}{flag}")
    return "\n".join(lines)
