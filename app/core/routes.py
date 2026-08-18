from flask import Blueprint, render_template, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import func

from app.db import db
from app.models import ConceptMastery, Concept, Topic, LLMCall, Tutorial, TutorialRead

bp = Blueprint("core", __name__)


@bp.get("/dashboard")
@login_required
def dashboard():
    masteries = db.session.query(ConceptMastery).filter_by(user_id=current_user.id).all()
    mastery_by_concept = {m.concept_id: m for m in masteries}

    topics = db.session.query(Topic).order_by(Topic.band, Topic.id).all()
    topic_rows = []
    for topic in topics:
        concepts = sorted(topic.concepts, key=lambda c: c.id)
        rows = []
        for concept in concepts:
            mastery = mastery_by_concept.get(concept.id)
            if mastery is None:
                state = "unseen"
            elif mastery.consecutive_misses >= 1:
                state = "struggling"
            elif mastery.covers >= 3:
                state = "mastered"
            else:
                state = "seen"
            rows.append({"concept": concept, "state": state, "mastery": mastery})
        topic_rows.append({"topic": topic, "concepts": rows})

    weak = [row for group in topic_rows for row in group["concepts"] if row["state"] == "struggling"]

    return render_template("core/dashboard.html", topic_rows=topic_rows, weak=weak)


@bp.get("/export.json")
@login_required
def export():
    from app.core.export import export_user_data

    return jsonify(export_user_data(current_user))


@bp.get("/admin/usage")
@login_required
def usage():
    calls = (
        db.session.query(LLMCall)
        .filter_by(user_id=current_user.id)
        .order_by(LLMCall.created_at.desc())
        .limit(200)
        .all()
    )
    total_calls, total_prompt, total_completion, total_cost = (
        db.session.query(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_estimate_cents), 0.0),
        )
        .filter_by(user_id=current_user.id)
        .one()
    )

    return render_template(
        "core/usage.html",
        calls=calls,
        total_calls=total_calls,
        total_prompt=total_prompt,
        total_completion=total_completion,
        total_cost=total_cost,
    )


@bp.get("/admin")
@login_required
def admin_dashboard():
    """Cross-cohort numbers for whoever is listed in ADMIN_EMAILS (docs §6.5):
    student/tutorial/scenario totals, cohort-wide LLM usage, and a per-student
    activity distribution — aggregate only, no per-student answer text."""
    if not current_user.is_admin:
        abort(403)

    from app.core.analytics import admin_overview

    return render_template("core/admin.html", **admin_overview())


@bp.get("/cohort")
@login_required
def cohort():
    """Instructor-only, aggregate-only — no per-student answer text ever
    appears here. Docs §6.4: this exists to show where the cohort is
    struggling, not to grade individuals."""
    if not current_user.is_instructor:
        abort(403)

    rows = []
    for concept in db.session.query(Concept).all():
        masteries = db.session.query(ConceptMastery).filter_by(concept_id=concept.id).all()
        n_students = len(masteries)
        n_struggling = sum(1 for m in masteries if m.consecutive_misses >= 1)
        n_mastered = sum(1 for m in masteries if m.covers >= 3 and m.consecutive_misses == 0)
        avg_consecutive = round(sum(m.consecutive_misses for m in masteries) / n_students, 2) if n_students else 0

        tutorial = db.session.query(Tutorial).filter_by(concept_id=concept.id).one_or_none()
        n_assigned = 0
        n_read = 0
        if tutorial:
            n_assigned = db.session.query(TutorialRead).filter_by(tutorial_id=tutorial.id).count()
            n_read = (
                db.session.query(TutorialRead)
                .filter(TutorialRead.tutorial_id == tutorial.id, TutorialRead.read_at.isnot(None))
                .count()
            )

        rows.append(
            {
                "concept": concept,
                "n_students": n_students,
                "n_struggling": n_struggling,
                "n_mastered": n_mastered,
                "avg_consecutive_misses": avg_consecutive,
                "tutorial": tutorial,
                "n_assigned": n_assigned,
                "n_read": n_read,
            }
        )

    rows.sort(key=lambda r: (-r["n_struggling"], r["concept"].id))
    return render_template("core/cohort.html", rows=rows)
