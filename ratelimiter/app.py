"""Flask service that puts a rate limiter in front of a small JSON API.

Clients are identified by the X-API-Key header, falling back to the remote
address. Every limited response carries the standard headers

    RateLimit-Limit, RateLimit-Remaining  (and Retry-After on 429)

so well-behaved clients know when to slow down.
"""
from __future__ import annotations

import math
import os
import random
import threading
import time

from flask import Flask, g, jsonify, request

from .algorithms import ALGORITHMS, RateLimiter, make_limiter

QUOTES = [
    "Premature optimization is the root of all evil.",
    "Make it work, make it right, make it fast.",
    "Simplicity is prerequisite for reliability.",
    "Programs must be written for people to read.",
    "Testing shows the presence, not the absence of bugs.",
]

UNLIMITED_PATHS = {"/health"}


def client_key() -> str:
    api_key = request.headers.get("X-API-Key", "").strip()
    if api_key:
        return "key:" + api_key[:128]
    return "ip:" + (request.remote_addr or "unknown")


def create_app(limiter: RateLimiter | None = None, *, algorithm: str | None = None,
               limit: int | None = None, window: float | None = None,
               clock=time.monotonic) -> Flask:
    """Build the app. Pass a ready limiter, or algorithm/limit/window
    (which default to the RATE_ALGORITHM / RATE_LIMIT / RATE_WINDOW
    environment variables, then token_bucket / 10 / 60)."""
    if limiter is None:
        algorithm = algorithm or os.environ.get("RATE_ALGORITHM", "token_bucket")
        limit = limit if limit is not None else int(os.environ.get("RATE_LIMIT", "10"))
        window = window if window is not None else float(os.environ.get("RATE_WINDOW", "60"))
        limiter = make_limiter(algorithm, limit, window, clock)

    app = Flask(__name__)
    app.config["LIMITER"] = limiter
    stats = {"allowed": 0, "rejected": 0}
    stats_lock = threading.Lock()

    @app.before_request
    def apply_limit():
        if request.path in UNLIMITED_PATHS:
            return None
        decision = limiter.allow(client_key())
        g.decision = decision
        with stats_lock:
            stats["allowed" if decision.allowed else "rejected"] += 1
        if decision.allowed:
            return None
        retry = max(1, math.ceil(decision.retry_after))  # header must be whole seconds
        resp = jsonify(error="rate_limited",
                       message=f"Too many requests. Try again in {retry} s.",
                       retry_after=retry)
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry)
        return resp

    @app.after_request
    def add_headers(resp):
        decision = g.get("decision")
        if decision is not None:
            resp.headers["RateLimit-Limit"] = str(decision.limit)
            resp.headers["RateLimit-Remaining"] = str(decision.remaining)
        return resp

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/quote")
    def quote():
        return jsonify(quote=random.choice(QUOTES))

    @app.get("/api/limits")
    def limits():
        with stats_lock:
            counts = dict(stats)
        return jsonify(algorithm=limiter.name, limit=limiter.limit, window_seconds=limiter.window,
                       tracked_clients=limiter.tracked_clients, **counts)

    @app.errorhandler(404)
    def not_found(_):
        return jsonify(error="not_found"), 404

    return app


__all__ = ["create_app", "client_key", "ALGORITHMS"]
