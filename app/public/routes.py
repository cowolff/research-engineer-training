from flask import Blueprint, render_template

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    """atlasflow's health check target: probed every 15 s with a 5 s timeout,
    any 2xx counts as healthy, 3 consecutive failures marks the deployment
    unhealthy. Must stay reachable, fast, and un-redirected for an anonymous
    request forever — see tests/test_app.py and docs/IMPLEMENTATION_PLAN.md §2.
    No database access here on purpose."""
    return render_template("public/index.html")


@bp.get("/healthz")
def healthz():
    """A *separate*, DB-touching health endpoint for humans/monitoring — not
    what atlasflow probes. Kept apart so a slow or broken DB can never turn
    into a failed deployment via the real health check."""
    from flask import jsonify
    from app.db import db
    from sqlalchemy import text
    from flask import current_app

    checks = {"database": "unknown", "resource_index": "unknown"}
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        checks["database"] = f"error: {exc}"

    try:
        from app.tutorials.resources import resource_index_available

        checks["resource_index"] = "ok" if resource_index_available(current_app) else "missing"
    except Exception as exc:  # noqa: BLE001
        checks["resource_index"] = f"error: {exc}"

    ok = all(v == "ok" for v in checks.values())
    return jsonify({"ok": ok, "checks": checks}), (200 if ok else 503)
