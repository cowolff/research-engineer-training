import time


def test_root_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_root_does_not_redirect(client):
    """atlasflow's health check hits `/` anonymously and expects a fast 2xx
    with no redirect (e.g. to a login page) — see README and
    docs/IMPLEMENTATION_PLAN.md §2. A redirect here silently fails deploys."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code < 300


def test_root_is_fast_and_touches_no_database(client, monkeypatch):
    """The real health check target must never depend on the database being
    up — that's what the separate /healthz is for."""
    from app.db import db

    called = {"hit": False}
    original_execute = db.session.execute

    def _tripwire(*args, **kwargs):
        called["hit"] = True
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(db.session, "execute", _tripwire)

    started = time.monotonic()
    resp = client.get("/")
    elapsed_ms = (time.monotonic() - started) * 1000

    assert resp.status_code == 200
    assert called["hit"] is False
    assert elapsed_ms < 200


def test_csp_header_has_no_unsafe_inline_script(client):
    resp = client.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]  # not on script-src specifically


def test_healthz_reports_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["resource_index"] == "ok"
