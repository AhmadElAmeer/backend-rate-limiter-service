# backend-rate-limiter-service

Four rate-limiting algorithms (fixed window, sliding window log, sliding window counter, token bucket) behind one interface, a Flask API that enforces them with standard `429` / `Retry-After` responses, and a simulator that replays the same traffic against all four to measure how strictly each one actually holds the limit.

## Problem

Every public API needs to stop one client from using up capacity meant for everyone: a buggy retry loop, a scraper, a brute-force login attempt. The policy usually sounds simple: *at most N requests per T seconds per client*.

The algorithms that enforce it are not equally strict. Some let through twice the limit at the right moment, some keep a timestamp for every request, and some deliberately allow bursts. This project implements the four common ones, puts them behind a real HTTP service, and measures the differences instead of just describing them.

## Overview

- `ratelimiter/algorithms.py`: the four limiters. Each has the same `allow(client) -> Decision(allowed, remaining, retry_after)` method, per-client state, a lock for thread safety, and automatic cleanup of idle clients.
- `ratelimiter/app.py`: a Flask app factory. A `before_request` hook rate-limits every route except `/health`. Clients are identified by the `X-API-Key` header, falling back to their IP address.
- `ratelimiter/simulate.py`: replays three traffic patterns against every algorithm on a simulated clock, and reports the most requests allowed in *any* window of T seconds.
- `ratelimiter/cli.py`: `serve`, `simulate` and `bench` commands.

## Key features

- Four algorithms behind one interface, selectable per service.
- Accurate `Retry-After`: the tests check that a client retrying slightly before it is still refused, and retrying at it is allowed.
- `RateLimit-Limit` and `RateLimit-Remaining` headers on every limited response, and a JSON `429` body.
- Thread-safe: one lock per limiter. A test races 32 threads against a single client.
- Bounded memory: clients idle for more than two windows are forgotten. A test proves this never changes a single decision.
- Injectable clock, so every test and simulation is deterministic.
- Configuration by arguments or by the `RATE_ALGORITHM`, `RATE_LIMIT` and `RATE_WINDOW` environment variables. Invalid settings fail at startup.

## Tech stack

- Python 3.10+ (tested on 3.13) and Flask 3
- pytest
- Standard library only for the algorithms (`collections.deque`, `threading`, `math`)

## Architecture

```
            HTTP request
                 │
                 ▼
   before_request: key = X-API-Key or IP ──► limiter.allow(key) ──► Decision
                 │                                                    │
        allowed? ├── no ──► 429 + Retry-After + JSON body             │
                 │                                                    │
                 ▼ yes                                                │
           route handler                                              │
                 │                                                    │
                 ▼                                                    │
   after_request: RateLimit-Limit / RateLimit-Remaining ◄─────────────┘
```

### The four algorithms

Policy: **L** requests per **W** seconds.

| algorithm | state per client | how it decides |
|---|---|---|
| Fixed window | window index, count | Counter for the current clock-aligned window `[kW, (k+1)W)`, reset when the window changes |
| Sliding window log | timestamp of every request in the last W s (up to L) | Drop timestamps older than W; allow if fewer than L remain |
| Sliding window counter | window index, previous count, current count | Estimate = previous × (fraction of the previous window still inside the last W s) + current; allow if estimate + 1 ≤ L |
| Token bucket | tokens, last update time | Refill at L/W tokens per second up to L; each request spends one |

**Retry-After** is computed exactly for each algorithm:
- **Fixed window:** time until the window ends.
- **Sliding log:** time until the oldest logged request is W seconds old.
- **Sliding counter:** solve `prev × (1 − f) + curr + 1 ≤ L` for the window fraction `f`. If the current window is already full, solve in the next window instead.
- **Token bucket:** `(1 − tokens) / rate`.

The HTTP header rounds up to whole seconds, so it is never too early.

## Installation

```bash
git clone https://github.com/AhmadElAmeer/backend-rate-limiter-service.git
cd backend-rate-limiter-service
python3 -m pip install -r requirements.txt
```

## Usage

### Run the API

```bash
python3 -m ratelimiter serve --algorithm sliding_log --limit 5 --window 10 --port 5077
```

A real session against that server:

```
$ for i in $(seq 1 7); do curl -s -o /dev/null -H "X-API-Key: demo" \
    -w "request $i: %{http_code}  remaining=%header{ratelimit-remaining}  retry-after=%header{retry-after}\n" \
    localhost:5077/api/quote; done
request 1: 200  remaining=4  retry-after=
request 2: 200  remaining=3  retry-after=
request 3: 200  remaining=2  retry-after=
request 4: 200  remaining=1  retry-after=
request 5: 200  remaining=0  retry-after=
request 6: 429  remaining=0  retry-after=10
request 7: 429  remaining=0  retry-after=10

$ curl -si -H "X-API-Key: demo" localhost:5077/api/quote
HTTP/1.1 429 TOO MANY REQUESTS
Retry-After: 10
RateLimit-Limit: 5
RateLimit-Remaining: 0
{"error":"rate_limited","message":"Too many requests. Try again in 10 s.","retry_after":10}

$ curl -s -H "X-API-Key: other" localhost:5077/api/quote      # a different client is unaffected
{"quote":"Programs must be written for people to read."}
```

| endpoint | limited | returns |
|---|---|---|
| `GET /api/quote` | yes | a random programming quote |
| `GET /api/limits` | yes | algorithm, limit, window, tracked clients, allowed/rejected counts |
| `GET /health` | no | `{"status": "ok"}` |

`serve` uses Flask's development server. For production you'd run `create_app()` under a WSGI server such as gunicorn, with a shared store (see Future improvements).

### As a library

```python
from ratelimiter import make_limiter

limiter = make_limiter("token_bucket", limit=100, window=60)
decision = limiter.allow("user-42")
if not decision.allowed:
    print(f"slow down, retry in {decision.retry_after:.1f} s")
```

### Compare the algorithms

```bash
python3 -m ratelimiter simulate          # 100 requests per 60 s, three traffic patterns
python3 -m ratelimiter bench             # allow() calls per second
```

## Results

`python3 -m ratelimiter simulate` (simulated clock, so these numbers are exact and reproducible):

```
policy: 100 requests per 60 s (worst window must be <= 100)

pattern             algorithm         requests  allowed  worst window  numbers/client
boundary-burst      fixed_window          4000     4000           200               2  <-- over limit
boundary-burst      sliding_log           4000     2000           100             100
boundary-burst      sliding_counter       4000     2000           100               3
boundary-burst      token_bucket          4000     2000           100               2
steady-3x-overload  fixed_window         30012    10001           126               2  <-- over limit
steady-3x-overload  sliding_log          30012     9957           100             100
steady-3x-overload  sliding_counter      30012     9898           102               3  <-- over limit
steady-3x-overload  token_bucket         30012    10099           199               2  <-- over limit
idle-then-bursts    fixed_window          6000     3000           107               2  <-- over limit
idle-then-bursts    sliding_log           6000     3000           100             100
idle-then-bursts    sliding_counter       6000     2770           100               3
idle-then-bursts    token_bucket          6000     3029           108               2  <-- over limit
```

"Worst window" is the most requests allowed in any interval of 60 seconds. The traffic patterns are:
- **boundary-burst:** 100 requests 0.01 s before a window boundary, then 100 more 0.01 s after it, repeated.
- **steady-3x-overload:** random (Poisson) arrivals at three times the allowed rate.
- **idle-then-bursts:** quiet periods followed by 200 requests within one second.

What the numbers show:

- **Fixed window let 200 requests through in 0.02 seconds** against a limit of 100: all 4,000 requests in the boundary test were allowed. Each window's count was legal, but together they doubled the rate. This is the classic flaw, reproduced exactly.
- **The sliding log is the only algorithm that never exceeded 100** in any pattern. The cost is memory: it stored 100 timestamps per client, against 2–3 numbers for the others. With a limit of 10,000 per hour, that's 10,000 numbers per client.
- **The sliding counter is close but approximate.** It allowed 102 under steady overload, because it assumes the previous window's requests were spread evenly. In the burst pattern it allowed 2,770 of 3,000, because the same assumption over-counts a burst that has already left the window.
- **The token bucket enforces a different policy.** It allows a burst of up to 100 (a full bucket) plus 100 more refilled over the next 60 s, so up to 199 in one window. In return its long-run rate matches the target: 10,099 allowed over 100 windows is the initial full bucket of 100, plus 100 per window. A token bucket is the right choice when short bursts are fine and the long-run rate is what matters.

`python3 -m ratelimiter bench` (real clock, 200,000 calls over 1,000 clients, 2-core Intel Xeon @ 2.10 GHz):

```
algorithm         allow() calls/s
fixed_window            1,590,885
sliding_log             1,453,405
sliding_counter         1,087,770
token_bucket            1,178,796
```

All four handle over a million decisions per second, far more than a Flask process serves requests. Between runs the numbers moved by up to 25% (token bucket ranged from 1.18 to 1.48 million). So speed isn't a meaningful way to choose between them; strictness and memory are.

## Testing

```bash
python3 -m pytest -q        # 157 tests, about 1 second
```

- **Guarantees, checked on random traffic:**
  - the sliding log never exceeds the limit in any window;
  - fixed window never exceeds it inside an aligned window;
  - the token bucket never exceeds `L + rate × T` in any interval T;
  - the sliding counter's current-window count never exceeds L.
- **The known flaw is demonstrated:** fixed window allows exactly 2 × L across a boundary.
- **`Retry-After` is honest:** for every rejection on random traffic, retrying 5 ms before it is refused, and retrying at it succeeds.
- **Thread safety:** 32 threads race for one client 25 times, with Python's thread switch interval set to 1 µs to make races likely. Exactly L requests may succeed every time.
- **Cleanup changes nothing:** a limiter that sweeps idle clients after every call makes the same decisions as one that never sweeps, over 3,000 random requests from 6 clients.
- **HTTP behaviour:**
  - headers, the 429 body and rounding;
  - per-key and per-IP limits, and a blank API key falling back to the IP;
  - `/health` unlimited;
  - the stats endpoint, JSON 404s, configuration from the environment, and startup failure on bad settings.

To check the tests can fail, eight bugs were planted one at a time:

- the log never records requests;
- the fixed window never resets;
- the sliding counter ignores overlap weighting;
- the token bucket isn't capped;
- the token bucket's `Retry-After` is too long;
- the lock is removed;
- cleanup evicts active clients;
- `Retry-After` is rounded down.

The first version of the suite missed three of them: the uncapped bucket, the missing lock, and the eager cleanup. The idle-burst test, the repeated race and the cleanup-equivalence test were added for those, and now all eight are caught. The missing lock was caught in 6 out of 6 runs.

## Design decisions and challenges

- **One interface, injectable clock.** Every limiter takes a `clock` function. Production uses `time.monotonic`, which never jumps backwards when the system clock is adjusted. Tests and the simulator use a fake clock, so a 100-window simulation runs in milliseconds and gives the same result every time.
- **Testing a race in Python.** The GIL rarely switches threads in the middle of a short function, so the obvious race test passed even with the lock removed. Lowering `sys.setswitchinterval` and repeating the race made the missing lock fail every time, while the real code passed every time.
- **Evicting idle clients safely.** Without cleanup, every client that ever sent one request stays in memory forever. Clients idle for more than two windows are dropped: by then every algorithm would treat them exactly like a new client. A test checks that this never changes a decision.
- **Rounding `Retry-After`.** The header must be whole seconds. Rounding down would tell clients to retry too early, and they'd be refused again. It is rounded up, and that's tested.
- **Rejected requests don't count.** None of the algorithms charge a client for a refused request. Otherwise a client that keeps retrying would stay locked out forever.

## Future improvements

- **Redis-backed limiters,** so several server processes share one limit (in Redis, the sliding log is a sorted set and the token bucket a Lua script). Today each process has its own in-memory state.
- Different limits per route or per plan (free vs. paid API keys).
- The [IETF RateLimit header draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/), including `RateLimit-Reset`.
- A leaky-bucket queue that delays requests instead of rejecting them.
- Load-testing the HTTP service itself, as opposed to the algorithm in isolation.

## What this project teaches

- Four classic rate-limiting algorithms and their trade-offs between strictness, memory and burst tolerance.
- Building a REST API with Flask: app factories, request hooks, custom headers and JSON error responses.
- Concurrency: why shared state needs a lock, and how to write a test that proves it.
- Making time testable: dependency injection of a clock, and a simulator built on it.
- Property-style testing: checking guarantees on random inputs instead of hand-picked examples.

## License

MIT — see [LICENSE](LICENSE).
