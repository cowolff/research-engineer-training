from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user

from app.db import db
from app.models import Job, Attempt

bp = Blueprint("jobs", __name__, url_prefix="/jobs")


@bp.get("/<job_id>")
@login_required
def status(job_id):
    job = db.session.get(Job, job_id)
    if job is None or job.user_id != current_user.id:
        abort(404)

    retry_scenario_id = None
    if job.kind == "grade_attempt":
        attempt = db.session.get(Attempt, job.payload.get("attempt_id"))
        if attempt:
            retry_scenario_id = attempt.scenario_id

    return render_template("partials/job_status.html", job=job, retry_scenario_id=retry_scenario_id)
