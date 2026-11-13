"""Command-line interface.

    python -m ratelimiter serve --algorithm sliding_log --limit 5 --window 10
    python -m ratelimiter simulate --limit 100 --window 60
    python -m ratelimiter bench
"""
from __future__ import annotations

import argparse

from .algorithms import ALGORITHMS
from .simulate import format_results, run_all, throughput


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ratelimiter", description="Rate limiting algorithms and a demo API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def policy(sp, limit, window):
        sp.add_argument("--limit", type=positive_int, default=limit, help=f"requests per window (default {limit})")
        sp.add_argument("--window", type=positive_float, default=window, help=f"window in seconds (default {window:g})")

    s = sub.add_parser("serve", help="run the demo API (Flask development server)")
    s.add_argument("--algorithm", choices=ALGORITHMS, default="token_bucket")
    policy(s, 10, 60)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=5000)

    s = sub.add_parser("simulate", help="replay traffic patterns against every algorithm")
    policy(s, 100, 60)
    s.add_argument("--seed", type=int, default=0)

    s = sub.add_parser("bench", help="measure allow() calls per second")
    s.add_argument("--calls", type=positive_int, default=200_000)

    a = p.parse_args(argv)
    if a.cmd == "serve":
        from .app import create_app
        app = create_app(algorithm=a.algorithm, limit=a.limit, window=a.window)
        print(f"{a.algorithm}: {a.limit} requests per {a.window:g} s per client")
        app.run(host=a.host, port=a.port, threaded=True)
    elif a.cmd == "simulate":
        print(format_results(run_all(a.limit, a.window, a.seed), a.limit, a.window))
    else:
        print(f"{'algorithm':<17}{'allow() calls/s':>16}")
        for name in ALGORITHMS:
            print(f"{name:<17}{throughput(name, calls=a.calls):>16,.0f}")
    return 0
