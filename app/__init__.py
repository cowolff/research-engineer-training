import logging

from flask import Flask
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.db import db, init_db

login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

logger = logging.getLogger("app")


def create_app(config=None):
    app = Flask(__name__)

    cfg = config if config is not None else Config()
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.database_uri
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config["TESTING"] = cfg.testing
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    app.app_config = cfg  # our own settings object, distinct from Flask's dict config

    # TLS is terminated upstream on Atlasflow; without ProxyFix, url_for(_external=True),
    # redirect targets, and Secure-cookie checks all see the internal http:// hop instead
    # of the real https:// request.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["SESSION_COOKIE_SECURE"] = cfg.session_cookie_secure
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    init_db(app)
    # `from app import models`, NOT `import app.models`: the latter binds the
    # name `app` in this local scope too (import X.Y always binds X), which
    # would silently clobber the `app` Flask instance created two lines above.
    from app import models  # noqa: F401 - registers every table on db.metadata before migrations run

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        from app.models import User

        return db.session.get(User, user_id)

    csrf.init_app(app)
    limiter.init_app(app)

    from app.public.routes import bp as public_bp
    from app.auth.routes import bp as auth_bp
    from app.core.routes import bp as core_bp
    from app.training.routes import bp as train_bp
    from app.jobs.routes import bp as jobs_bp
    from app.tutorials.routes import bp as tutorials_bp
    from app.tutorials.resource_routes import bp as resources_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(train_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(tutorials_bp)
    app.register_blueprint(resources_bp)

    csrf.exempt(jobs_bp)  # polled by htmx GETs only — no state-changing POSTs in this blueprint

    from app.cli import register_cli

    register_cli(app)

    @app.context_processor
    def inject_globals():
        return {"app_config": cfg}

    from app.render import render_markdown

    app.jinja_env.filters["markdown"] = render_markdown

    @app.after_request
    def set_security_headers(response):
        # No 'unsafe-inline' on script-src: htmx is vendored into static/ and
        # driven entirely through HTML attributes, so nothing here needs an
        # inline <script> block. This is defense-in-depth on top of nh3
        # sanitisation (docs §10) — LLM-authored markdown can never carry a
        # `style` attribute either way, since nh3's allowlist excludes it.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    if cfg.run_background_worker:
        from app.jobs.reaper import reap_stale_jobs
        from app.jobs.worker import start_worker

        with app.app_context():
            reap_stale_jobs()
        start_worker(app)

    return app
