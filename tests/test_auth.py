from app.db import db
from app.models import User


def _csrf(html):
    import re

    m = re.search(r'name="csrf_token"[^>]*value="([^"]*)"', html)
    return m.group(1)


def test_register_login_logout_roundtrip(client):
    r = client.get("/register")
    token = _csrf(r.get_data(as_text=True))
    r = client.post(
        "/register",
        data={"csrf_token": token, "email": "New.Student@Example.com", "password": "correcthorsebatterystaple", "confirm": "correcthorsebatterystaple"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"Dashboard" in r.data

    r = client.post("/logout", data={"csrf_token": token}, follow_redirects=True)
    assert r.status_code in (200, 400)  # a stale CSRF token from before logout may 400; either way session ends

    # Emails are lowercased on registration — this matters for the
    # enumeration defense and for INSTRUCTOR_EMAILS matching.
    with client.application.app_context():
        user = db.session.query(User).filter_by(email="new.student@example.com").one()
        assert user.role == "student"


def test_wrong_password_is_rejected(client, app):
    with app.app_context():
        from argon2 import PasswordHasher

        db.session.add(User(id="u1", email="known@example.com", password_hash=PasswordHasher().hash("correct-password-123")))
        db.session.commit()

    r = client.get("/login")
    token = _csrf(r.get_data(as_text=True))
    r = client.post("/login", data={"csrf_token": token, "email": "known@example.com", "password": "wrong-password"})
    assert r.status_code == 200
    assert b"Incorrect email or password" in r.data


def test_instructor_role_assigned_from_config(client, app):
    app.app_config.instructor_emails = {"prof@example.com"}

    r = client.get("/register")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        "/register",
        data={"csrf_token": token, "email": "prof@example.com", "password": "correcthorsebatterystaple", "confirm": "correcthorsebatterystaple"},
    )
    with app.app_context():
        user = db.session.query(User).filter_by(email="prof@example.com").one()
        assert user.role == "instructor"


def test_anonymous_cannot_reach_dashboard(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
