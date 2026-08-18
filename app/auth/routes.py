import hashlib
from datetime import datetime, date

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.db import db
from app.models import User, AuthSession
from app.auth.forms import RegisterForm, LoginForm
from app import limiter

bp = Blueprint("auth", __name__)
_hasher = PasswordHasher()


def _role_for_email(email):
    email = email.lower()
    cfg = current_app.app_config
    if email in cfg.admin_emails:
        return "admin"
    if email in cfg.instructor_emails:
        return "instructor"
    return "student"


def _record_session(user):
    ua_hash = hashlib.sha256((request.headers.get("User-Agent") or "").encode()).hexdigest()
    session_row = AuthSession(user_id=user.id, user_agent_hash=ua_hash)
    db.session.add(session_row)
    user.last_seen_at = datetime.utcnow()
    db.session.commit()


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("3 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        existing = db.session.query(User).filter_by(email=email).first()
        if existing:
            # Never reveal whether the email is taken — same message either way.
            flash("If that email can be registered, we've sent next steps. Try logging in.", "flash")
            return redirect(url_for("auth.login"))

        user = User(
            email=email,
            password_hash=_hasher.hash(form.password.data),
            role=_role_for_email(email),
            daily_window_start=date.today(),
        )
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        _record_session(user)
        flash("Welcome — let's find out what you already know.", "flash")
        return redirect(url_for("core.dashboard"))

    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = db.session.query(User).filter_by(email=email).first()
        ok = False
        if user:
            try:
                _hasher.verify(user.password_hash, form.password.data)
                ok = True
            except VerifyMismatchError:
                ok = False
            if ok and _hasher.check_needs_rehash(user.password_hash):
                user.password_hash = _hasher.hash(form.password.data)

        if not ok:
            flash("Incorrect email or password.", "error")
            return render_template("auth/login.html", form=form)

        # Re-check on every login so promoting an instructor via INSTRUCTOR_EMAILS
        # takes effect without recreating the account.
        user.role = _role_for_email(email)
        login_user(user, remember=True)
        _record_session(user)
        return redirect(url_for("core.dashboard"))

    return render_template("auth/login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "flash")
    return redirect(url_for("public.index"))
