from flask import Blueprint, render_template, redirect, url_for, request, abort, flash
from flask_login import login_required, current_user

from app.db import db
from app.models import Scenario, Attempt, Job, Concept
from app.training.quota import check_and_increment, QuotaExceeded
from app.training.gaps import apply_dispute

bp = Blueprint("train", __name__)


def _owned_scenario(scenario_id):
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != current_user.id:
        abort(404)
    return scenario


@bp.post("/train")
@login_required
def start():
    try:
        check_and_increment(current_user)
    except QuotaExceeded as exc:
        flash(str(exc), "error")
        return redirect(url_for("core.dashboard"))

    forced_concept_id = request.form.get("concept_id") or None
    job = Job(
        user_id=current_user.id,
        kind="generate_scenario",
        payload_json={"forced_concept_id": forced_concept_id} if forced_concept_id else {},
    )
    db.session.add(job)
    db.session.commit()
    return render_template("train/pending.html", job=job, heading="Writing your next scenario")


@bp.get("/train/<scenario_id>")
@login_required
def view_scenario(scenario_id):
    scenario = _owned_scenario(scenario_id)
    latest_attempt = (
        db.session.query(Attempt)
        .filter_by(scenario_id=scenario.id, user_id=current_user.id)
        .order_by(Attempt.submitted_at.desc())
        .first()
    )
    if latest_attempt and latest_attempt.graded_at:
        return redirect(url_for("train.feedback", scenario_id=scenario.id))
    return render_template("train/scenario.html", scenario=scenario)


@bp.post("/train/<scenario_id>/answer")
@login_required
def submit_answer(scenario_id):
    scenario = _owned_scenario(scenario_id)

    answer_text = request.form.get("answer_text", "").strip()
    if not answer_text:
        flash("Write an answer before submitting.", "error")
        return redirect(url_for("train.view_scenario", scenario_id=scenario_id))

    try:
        check_and_increment(current_user)
    except QuotaExceeded as exc:
        flash(str(exc), "error")
        return redirect(url_for("train.view_scenario", scenario_id=scenario_id))

    attempt = Attempt(scenario_id=scenario.id, user_id=current_user.id, answer_text=answer_text)
    db.session.add(attempt)
    db.session.commit()

    job = Job(user_id=current_user.id, kind="grade_attempt", payload_json={"attempt_id": attempt.id})
    db.session.add(job)
    db.session.commit()
    return render_template("train/pending.html", job=job, heading="Grading your answer")


@bp.get("/train/<scenario_id>/feedback")
@login_required
def feedback(scenario_id):
    scenario = _owned_scenario(scenario_id)
    attempt = (
        db.session.query(Attempt)
        .filter_by(scenario_id=scenario.id, user_id=current_user.id)
        .order_by(Attempt.submitted_at.desc())
        .first()
    )
    if attempt is None or attempt.graded_at is None:
        abort(404)

    concept_ids = [item["concept_id"] for item in attempt.grade.get("items", [])]
    concepts_by_id = {c.id: c for c in db.session.query(Concept).filter(Concept.id.in_(concept_ids)).all()}

    return render_template("train/feedback.html", scenario=scenario, attempt=attempt, concepts_by_id=concepts_by_id)


@bp.post("/attempts/<attempt_id>/dispute")
@login_required
def dispute(attempt_id):
    attempt = db.session.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != current_user.id:
        abort(404)

    concept_id = request.form.get("concept_id")
    try:
        apply_dispute(attempt, concept_id)
        flash("Thanks — we've updated your record for that concept.", "flash")
    except ValueError:
        flash("Couldn't process that dispute.", "error")

    return redirect(url_for("train.feedback", scenario_id=attempt.scenario_id))
