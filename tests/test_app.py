import pytest

from ratelimiter import SlidingWindowLog
from ratelimiter.app import create_app
from ratelimiter.cli import main
from ratelimiter.simulate import FakeClock


@pytest.fixture
def clock():
    return FakeClock(1000.0)


@pytest.fixture
def client(clock):
    app = create_app(SlidingWindowLog(3, 10.0, clock))
    return app.test_client()


def get(client, path="/api/quote", key=None, ip="10.0.0.1"):
    headers = {"X-API-Key": key} if key else {}
    return client.get(path, headers=headers, environ_base={"REMOTE_ADDR": ip})


def test_allowed_requests_carry_rate_limit_headers(client):
    r = get(client)
    assert r.status_code == 200
    assert "quote" in r.get_json()
    assert r.headers["RateLimit-Limit"] == "3"
    assert r.headers["RateLimit-Remaining"] == "2"


def test_over_limit_returns_429_with_retry_after(client, clock):
    for _ in range(3):
        assert get(client).status_code == 200
    clock.t += 2.5
    r = get(client)
    assert r.status_code == 429
    body = r.get_json()
    assert body["error"] == "rate_limited"
    # oldest request leaves the window 7.5 s from now -> rounded up to 8
    assert r.headers["Retry-After"] == "8" and body["retry_after"] == 8
    assert r.headers["RateLimit-Remaining"] == "0"


def test_client_is_allowed_again_after_retry_after(client, clock):
    for _ in range(3):
        get(client)
    retry = int(get(client).headers["Retry-After"])
    clock.t += retry
    assert get(client).status_code == 200


def test_api_keys_are_limited_separately(client):
    for _ in range(3):
        assert get(client, key="alice").status_code == 200
    assert get(client, key="alice").status_code == 429
    assert get(client, key="bob").status_code == 200


def test_ip_used_when_no_api_key(client):
    for _ in range(3):
        get(client, ip="10.0.0.1")
    assert get(client, ip="10.0.0.1").status_code == 429
    assert get(client, ip="10.0.0.2").status_code == 200


def test_blank_api_key_falls_back_to_ip(client):
    for _ in range(3):
        get(client, key="   ", ip="10.0.0.9")
    assert get(client, ip="10.0.0.9").status_code == 429


def test_health_is_never_limited(client):
    for _ in range(10):
        r = get(client, "/health")
        assert r.status_code == 200 and "RateLimit-Limit" not in r.headers


def test_limits_endpoint_reports_counts(client):
    for _ in range(5):
        get(client)
    body = get(client, "/api/limits", key="admin").get_json()
    assert body["algorithm"] == "sliding_log" and body["limit"] == 3
    assert body["allowed"] == 4 and body["rejected"] == 2   # admin's own call counts as allowed
    assert body["tracked_clients"] == 2


def test_unknown_route_is_json_404_and_still_counted(client):
    r = get(client, "/nope")
    assert r.status_code == 404 and r.get_json() == {"error": "not_found"}
    assert r.headers["RateLimit-Remaining"] == "2"


def test_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("RATE_ALGORITHM", "fixed_window")
    monkeypatch.setenv("RATE_LIMIT", "2")
    monkeypatch.setenv("RATE_WINDOW", "30")
    body = create_app().test_client().get("/api/limits").get_json()
    assert (body["algorithm"], body["limit"], body["window_seconds"]) == ("fixed_window", 2, 30.0)


def test_invalid_configuration_fails_at_startup():
    with pytest.raises(ValueError):
        create_app(algorithm="nope", limit=5, window=10)
    with pytest.raises(ValueError):
        create_app(algorithm="token_bucket", limit=0, window=10)


def test_cli_simulate(capsys):
    assert main(["simulate", "--limit", "20", "--window", "10"]) == 0
    out = capsys.readouterr().out
    assert "boundary-burst" in out and "over limit" in out


def test_cli_rejects_bad_numbers():
    with pytest.raises(SystemExit):
        main(["simulate", "--limit", "0"])
    with pytest.raises(SystemExit):
        main(["simulate", "--window", "-1"])
