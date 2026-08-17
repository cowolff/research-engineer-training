from app import app


def test_root_returns_hello_world():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.data == b"Hello, World!"


def test_root_does_not_redirect():
    """atlasflow's health check hits `/` anonymously and expects a fast
    2xx with no redirect (e.g. to a login page) — see README. This is a
    trivial assertion today since the template has no auth at all, but
    keep it once you add any: a redirect here silently fails deploys."""
    client = app.test_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code < 300
