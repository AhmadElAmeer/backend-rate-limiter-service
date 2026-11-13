import random
import sys
import threading

import pytest

from ratelimiter import ALGORITHMS, FixedWindow, SlidingWindowCounter, SlidingWindowLog, TokenBucket, make_limiter
from ratelimiter.simulate import FakeClock, boundary_burst, simulate, worst_window

ALL = list(ALGORITHMS.values())


def limiter(cls, limit=5, window=10.0, t=0.0):
    clock = FakeClock(t)
    return cls(limit, window, clock), clock


def random_arrivals(seed, n=400, span=200.0):
    rng = random.Random(seed)
    times = sorted(rng.uniform(0, span) for _ in range(n))
    # add some exact duplicates and tight bursts
    times += [times[n // 2]] * 10 + [times[n // 3] + i * 1e-4 for i in range(15)]
    return sorted(times)


# ---- basic behaviour shared by all algorithms ---------------------------

@pytest.mark.parametrize("cls", ALL)
def test_first_limit_requests_allowed_then_rejected(cls):
    lim, _ = limiter(cls)
    decisions = [lim.allow("a") for _ in range(6)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert [d.remaining for d in decisions[:5]] == [4, 3, 2, 1, 0]
    assert decisions[5].retry_after > 0 and decisions[5].remaining == 0


@pytest.mark.parametrize("cls", ALL)
def test_clients_are_independent(cls):
    lim, _ = limiter(cls, limit=1)
    assert lim.allow("a").allowed
    assert not lim.allow("a").allowed
    assert lim.allow("b").allowed


@pytest.mark.parametrize("cls", ALL)
def test_full_recovery_after_idle_but_no_more_than_limit(cls):
    lim, clock = limiter(cls)
    for _ in range(10):
        lim.allow("a")
    clock.t += 1000.0          # long idle must not bank extra capacity
    assert [lim.allow("a").allowed for _ in range(6)] == [True] * 5 + [False]


@pytest.mark.parametrize("cls", ALL)
@pytest.mark.parametrize("limit,window", [(0, 10), (-1, 10), (2.5, 10), (True, 10), (5, 0), (5, -3), (5, float("inf"))])
def test_invalid_configuration_rejected(cls, limit, window):
    with pytest.raises(ValueError):
        cls(limit, window)


def test_make_limiter_unknown_algorithm():
    with pytest.raises(ValueError, match="unknown algorithm"):
        make_limiter("leaky_magic", 5, 10)


# ---- retry_after is exact: not too early, not too late -------------------

@pytest.mark.parametrize("cls", ALL)
@pytest.mark.parametrize("seed", range(8))
def test_retry_after_is_honest(cls, seed):
    """Whenever a request is rejected with retry_after = r, retrying
    slightly before r is still rejected and retrying at r is allowed."""
    lim, clock = limiter(cls, limit=7, window=10.0)
    checked = 0
    for t in random_arrivals(seed, n=300, span=120.0):
        clock.t = max(clock.t, t)
        d = lim.allow("a")
        if d.allowed:
            continue
        r = d.retry_after
        if r > 0.01:
            clock.t += r - 0.005
            assert not lim.allow("a").allowed, f"allowed {0.005}s before retry_after"
            clock.t += 0.005
        else:
            clock.t += r
        assert lim.allow("a").allowed, "still rejected at retry_after"
        checked += 1
    assert checked > 10


# ---- what each algorithm actually guarantees -----------------------------

@pytest.mark.parametrize("seed", range(10))
def test_sliding_log_never_exceeds_limit_in_any_window(seed):
    r = simulate("sliding_log", random_arrivals(seed), limit=7, window=10.0)
    assert r.worst_window <= 7


@pytest.mark.parametrize("seed", range(10))
def test_fixed_window_respects_limit_inside_each_aligned_window(seed):
    lim, clock = limiter(FixedWindow, limit=7, window=10.0)
    per_window = {}
    for t in random_arrivals(seed):
        clock.t = t
        if lim.allow("a").allowed:
            per_window[int(t // 10)] = per_window.get(int(t // 10), 0) + 1
    assert max(per_window.values()) <= 7


def test_fixed_window_allows_double_limit_across_a_boundary():
    """The known weakness, demonstrated: 2 x limit within 0.02 s."""
    r = simulate("fixed_window", sorted(boundary_burst(10, 60.0, random.Random(0))), limit=10, window=60.0)
    assert r.worst_window == 20


@pytest.mark.parametrize("seed", range(10))
def test_token_bucket_bound(seed):
    """In any interval of length T a token bucket allows at most
    capacity + rate * T requests."""
    limit, window = 7, 10.0
    allowed = []
    lim, clock = limiter(TokenBucket, limit, window)
    for t in random_arrivals(seed):
        clock.t = t
        if lim.allow("a").allowed:
            allowed.append(t)
    rate = limit / window
    for i, start in enumerate(allowed):
        for j in range(i, len(allowed)):
            span = allowed[j] - start
            assert j - i + 1 <= limit + rate * span + 1e-6


@pytest.mark.parametrize("seed", range(10))
def test_sliding_counter_current_window_never_exceeds_limit(seed):
    lim, clock = limiter(SlidingWindowCounter, limit=7, window=10.0)
    for t in random_arrivals(seed):
        clock.t = t
        lim.allow("a")
        start, prev, curr = lim._state["a"]
        assert curr <= 7


def test_sliding_counter_weights_previous_window():
    lim, clock = limiter(SlidingWindowCounter, limit=10, window=10.0)
    clock.t = 5.0
    for _ in range(10):
        assert lim.allow("a").allowed
    # At t = 12.5, 75% of the previous window still overlaps: 10 * 0.75 = 7.5
    # counted, so 2 more requests fit (7.5 + 2 = 9.5 <= 10, a third would be 10.5).
    clock.t = 12.5
    assert [lim.allow("a").allowed for _ in range(3)] == [True, True, False]


def test_worst_window_helper():
    assert worst_window([0, 1, 2, 10, 10.5], 10) == 4   # (0.5, 10.5] holds 1, 2, 10, 10.5
    assert worst_window([0, 10], 10) == 1               # a request exactly one window old has expired
    assert worst_window([0, 9.99], 10) == 2
    assert worst_window([], 10) == 0


def _race_once(cls):
    lim, _ = limiter(cls, limit=100, window=60.0)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def worker():
        barrier.wait()
        local = [lim.allow("shared").allowed for _ in range(10)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(results), len(results)


@pytest.mark.parametrize("cls", ALL)
def test_thread_safety_exact_count(cls):
    """32 threads race for the same client at a frozen instant: exactly
    `limit` requests may get through. Repeated, with very frequent thread
    switches, so that a missing lock actually shows up as a race."""
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(25):
            assert _race_once(cls) == (100, 320)
    finally:
        sys.setswitchinterval(old_interval)


@pytest.mark.parametrize("cls", ALL)
def test_idle_clients_are_evicted(cls):
    clock = FakeClock()
    lim = cls(5, 10.0, clock, sweep_every=10)
    for i in range(9):
        lim.allow(f"old{i}")
    assert lim.tracked_clients == 9
    clock.t = 25.0                       # more than two windows later
    lim.allow("new")                     # 10th call triggers the sweep
    assert lim.tracked_clients == 1
    assert all(lim.allow("old0").allowed for _ in range(5))


@pytest.mark.parametrize("cls", ALL)
@pytest.mark.parametrize("seed", range(5))
def test_eviction_never_changes_a_decision(cls, seed):
    """A limiter that sweeps after every call must make exactly the same
    decisions as one that never sweeps."""
    rng = random.Random(seed)
    c1, c2 = FakeClock(), FakeClock()
    sweeping = cls(4, 10.0, c1, sweep_every=1)
    never = cls(4, 10.0, c2, sweep_every=10**9)
    t = 0.0
    for _ in range(3000):
        t += rng.expovariate(1.5)
        c1.t = c2.t = t
        key = f"c{rng.randrange(6)}"
        assert sweeping.allow(key) == never.allow(key), (t, key)
