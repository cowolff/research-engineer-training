from flask import Blueprint, render_template, redirect, url_for, request, abort, flash, current_app, make_response
from flask_login import login_required, current_user

from app.db import db
from app.models import Scenario, Attempt, Job, Concept
from app.training.quota import check_and_increment, QuotaExceeded
from app.training.gaps import apply_dispute
from app.training.conversation import get_or_create_attempt
from app.training.help import exchanges_for, questions_remaining

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
    """The conversation view: scenario, then the chat so far, then the input
    box — or a link to the summary once the conversation has closed."""
    scenario = _owned_scenario(scenario_id)
    attempt = _latest_attempt(scenario)
    if attempt and attempt.is_complete:
        return redirect(url_for("train.feedback", scenario_id=scenario.id))

    return render_template(
        "train/scenario.html",
        scenario=scenario,
        **_chat_context(scenario, attempt),
        **_help_context(scenario),
    )


def _latest_attempt(scenario):
    return (
        db.session.query(Attempt)
        .filter_by(scenario_id=scenario.id, user_id=current_user.id)
        .order_by(Attempt.submitted_at.desc())
        .first()
    )


def _chat_context(scenario, attempt, stream_job=None, pending_message=None, chat_error=None, draft=None):
    """Everything partials/chat_panel.html needs. Like the help panel, it
    always renders whole and from the database — `pending_message` is the one
    exception, and only because the turn now in flight is not in the database
    yet by definition."""
    return {
        "attempt": attempt,
        "turns": list(attempt.turns) if attempt else [],
        "max_turns": current_app.app_config.max_conversation_turns,
        "stream_job": stream_job,
        "pending_message": pending_message,
        "chat_error": chat_error,
        "draft": draft,
    }


def _render_chat_panel(scenario, attempt, **kwargs):
    return render_template(
        "partials/chat_panel.html", scenario=scenario, **_chat_context(scenario, attempt, **kwargs)
    )


@bp.post("/train/<scenario_id>/message")
@login_required
def send_message(scenario_id):
    """Starts one student turn.

    For an htmx request this answers with the chat panel itself rather than a
    redirect, and that is the point of §5.7: the student stays on the scenario
    they are reading while the reply streams into it, instead of being sent to
    a waiting page and then having to click their way back. Errors render
    inline for the same reason a `flash()` doesn't work here — there is no
    navigation left to display one on.

    The plain form post is kept working underneath, unchanged: without
    JavaScript there is no stream to watch, so that path still lands on the
    polling page.
    """
    scenario = _owned_scenario(scenario_id)
    streaming = bool(request.headers.get("HX-Request"))

    student_message = request.form.get("student_message", "").strip()
    if not student_message:
        if streaming:
            return _render_chat_panel(
                scenario, _latest_attempt(scenario), chat_error="Write something before sending."
            )
        flash("Write something before sending.", "error")
        return redirect(url_for("train.view_scenario", scenario_id=scenario_id))

    attempt = get_or_create_attempt(scenario, current_user)
    if attempt.is_complete:
        return _redirect_to_feedback(scenario_id, streaming)

    # Every turn is its own LLM call, so every turn costs quota — a longer
    # conversation genuinely consumes more of the daily budget than the old
    # single-shot flow did (§10, "LLM cost abuse").
    try:
        check_and_increment(current_user)
    except QuotaExceeded as exc:
        if streaming:
            # Their words go back into the composer rather than being dropped
            # on the floor: the quota is what failed, not the answer.
            return _render_chat_panel(scenario, attempt, chat_error=str(exc), draft=student_message)
        flash(str(exc), "error")
        return redirect(url_for("train.view_scenario", scenario_id=scenario_id))

    job = Job(
        user_id=current_user.id,
        kind="converse_turn",
        payload_json={"attempt_id": attempt.id, "student_message": student_message},
    )
    db.session.add(job)
    db.session.commit()

    if not streaming:
        return render_template(
            "train/pending.html", job=job, heading="Thinking about your answer", scenario=scenario
        )
    return _render_chat_panel(scenario, attempt, stream_job=job, pending_message=student_message)


def _redirect_to_feedback(scenario_id, streaming):
    """An htmx swap can't follow a 302 into a whole new page — the fragment
    would be pasted into the panel. `HX-Redirect` is how the browser is told
    to navigate for real."""
    target = url_for("train.feedback", scenario_id=scenario_id)
    if not streaming:
        return redirect(target)
    response = make_response("")
    response.headers["HX-Redirect"] = target
    return response


@bp.get("/train/<scenario_id>/chat")
@login_required
def chat_panel(scenario_id):
    """What replaces the streamed preview once the turn has landed.

    The preview is raw model text put on screen as plain text nodes; this is
    the same reply after markdown rendering and nh3 sanitisation, read back
    from the database along with the nudge, the turn counter and whatever the
    conversation's ending changed. Nothing polls it — it is fetched exactly
    once, when the stream says it is done.
    """
    scenario = _owned_scenario(scenario_id)

    job_id = request.args.get("job_id")
    job = db.session.get(Job, job_id) if job_id else None
    if job is not None and job.user_id != current_user.id:
        abort(404)

    error = job.error if job is not None and job.status == "failed" else None
    # A failed turn shouldn't cost the student their answer — hand it back to
    # the composer so sending again is one click, not one retype.
    draft = job.payload.get("student_message") if error and job is not None else None

    return _render_chat_panel(scenario, _latest_attempt(scenario), chat_error=error, draft=draft)


def _help_context(scenario, help_job=None, help_error=None):
    """Everything partials/help_panel.html needs. The panel always re-renders
    whole — from the database, not from what the last swap happened to hold —
    so the same context builder serves the initial page render, the post, and
    the one swap that lands when the streamed answer finishes."""
    return {
        "help_exchanges": exchanges_for(scenario, current_user.id),
        "help_remaining": questions_remaining(scenario, current_user.id),
        "help_job": help_job,
        "help_error": help_error,
    }


def _render_help_panel(scenario, help_job=None, help_error=None):
    return render_template(
        "partials/help_panel.html", scenario=scenario, **_help_context(scenario, help_job, help_error)
    )


@bp.post("/train/<scenario_id>/help")
@login_required
def ask_help(scenario_id):
    """A terminology question in the side chat (§5.6).

    Answers with the panel fragment rather than a redirect, and that is the
    whole reason this endpoint exists separately: a navigation here would
    discard the answer the student has been drafting in the main textarea,
    which is the one thing on this page that must never be lost. Errors are
    rendered inline for the same reason — a `flash()` only appears on a full
    page render, so it would sit unseen until the student navigated away.
    """
    scenario = _owned_scenario(scenario_id)

    question = request.form.get("question", "").strip()
    if not question:
        return _render_help_panel(scenario, help_error="Type a question first.")

    if questions_remaining(scenario, current_user.id) <= 0:
        return _render_help_panel(scenario, help_error="You've used all your questions for this scenario.")

    # Same rule as a conversation turn: a real LLM call, so it is checked and
    # charged before the job is enqueued, never after (§10, "LLM cost abuse").
    try:
        check_and_increment(current_user)
    except QuotaExceeded as exc:
        return _render_help_panel(scenario, help_error=str(exc))

    job = Job(
        user_id=current_user.id,
        kind="help_question",
        payload_json={"scenario_id": scenario.id, "question": question},
    )
    db.session.add(job)
    db.session.commit()

    if not request.headers.get("HX-Request"):
        # No-JS fallback: a bare form post would otherwise be handed a naked
        # HTML fragment as a whole page.
        return redirect(url_for("train.view_scenario", scenario_id=scenario.id))
    return _render_help_panel(scenario, help_job=job)


@bp.get("/train/<scenario_id>/help")
@login_required
def help_panel(scenario_id):
    """The finished, rendered panel — fetched once when the streamed answer
    completes, so it lands without touching the rest of the page. (Until
    §5.7 this was polled every 1.5s; the answer now arrives as it is written
    and this is only what replaces the raw preview with the sanitised,
    markdown-rendered version.)"""
    scenario = _owned_scenario(scenario_id)

    job_id = request.args.get("job_id")
    job = db.session.get(Job, job_id) if job_id else None
    if job is not None and job.user_id != current_user.id:
        abort(404)

    error = job.error if job is not None and job.status == "failed" else None
    return _render_help_panel(scenario, help_job=job, help_error=error)


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

    return render_template(
        "train/feedback.html",
        scenario=scenario,
        attempt=attempt,
        turns=list(attempt.turns),
        concepts_by_id=concepts_by_id,
        help_exchanges=exchanges_for(scenario, current_user.id),
    )


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
