from flask import Blueprint, render_template, redirect, abort, request, flash, url_for, current_app
from flask_login import login_required, current_user

from app.db import db
from app.models import ResourceReport, ResourceCitation, Tutorial
from app.tutorials.resources import get_resource, list_resources

bp = Blueprint("resources", __name__)


@bp.get("/r/<resource_id>")
@login_required
def redirect_resource(resource_id):
    """The only place a URL is ever emitted to the browser — everywhere else
    in the app, a resource is referenced by id and resolved here. Docs §7.4."""
    resource = get_resource(current_app, resource_id)
    if resource is None:
        abort(404)
    if resource["link_status"] == "gone":
        archive_url = f"https://web.archive.org/web/2/{resource['url']}"
        return render_template("resources/gone.html", resource=resource, archive_url=archive_url)
    return redirect(resource["url"])


@bp.get("/resources")
@login_required
def browse():
    resources = list_resources(current_app)
    return render_template("resources/browse.html", resources=resources)


@bp.get("/resources/<resource_id>")
@login_required
def view(resource_id):
    resource = get_resource(current_app, resource_id)
    if resource is None:
        abort(404)
    citations = db.session.query(ResourceCitation).filter_by(resource_id=resource_id).all()
    tutorial_ids = {c.tutorial_id for c in citations}
    tutorials = db.session.query(Tutorial).filter(Tutorial.id.in_(tutorial_ids)).all() if tutorial_ids else []
    return render_template("resources/view.html", resource=resource, tutorials=tutorials)


@bp.post("/resources/<resource_id>/report")
@login_required
def report(resource_id):
    reason = request.form.get("reason", "").strip()
    db.session.add(ResourceReport(user_id=current_user.id, resource_id=resource_id, reason=reason))
    db.session.commit()
    flash("Thanks — flagged for review.", "flash")
    return redirect(request.referrer or url_for("resources.browse"))
