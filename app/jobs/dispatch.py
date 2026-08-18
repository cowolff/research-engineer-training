import logging
from datetime import datetime

from app.db import db
from app.jobs import stream
from app.llm.streaming import TextStreamSink, streaming_to
from app.models import Job, User, Scenario, Attempt

logger = logging.getLogger("app.jobs")

# Which field of each job kind's structured result is the one a human is
# waiting to read. This is the whole configuration of §5.7's token streaming:
# the field named here is extracted from the JSON as it generates and pushed
# to the browser, and everything else in the document stays behind the
# schema validation it has to pass first.
_PREVIEW_FIELDS = {
    "generate_scenario": "prompt_md",
    "converse_turn": "reply_md",
    "help_question": "answer_md",
    "generate_tutorial": "body_md",
}


def claim_next_job():
    """Picks the oldest queued job and marks it running. Only ever called
    from the single dispatcher thread in app/jobs/worker.py — that's what
    makes a plain read-then-write safe without SELECT ... FOR UPDATE, which
    SQLite doesn't support anyway."""
    job = db.session.query(Job).filter_by(status="queued").order_by(Job.created_at.asc()).first()
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.utcnow()
    job.attempts += 1
    db.session.commit()
    return job.id


def run_job(job_id):
    job = db.session.get(Job, job_id)
    if job is None:
        return

    from app.llm.factory import get_provider
    from flask import current_app

    provider = get_provider(current_app)
    channel = stream.open_channel(job.id)

    status = "failed"
    error = ""
    try:
        try:
            # Anything generated inside this block is mirrored to the browser
            # as it arrives (§5.7). Outside it, generation behaves exactly as
            # it did before — a job kind with no preview field just runs.
            with streaming_to(_preview_sink(job.kind, channel)):
                result = _run(job, provider)
            status = "done"
            job.result_json = result
        except Exception as exc:  # noqa: BLE001 - a job failure must never crash the worker thread
            logger.exception("job_failed", extra={"extra_fields": {"job_id": job.id, "kind": job.kind}})
            error = str(exc)[:2000]

        job.status = status
        job.error = error
        job.finished_at = datetime.utcnow()
        db.session.commit()
    finally:
        # In a `finally` because a browser waiting on this stream has no other
        # way to learn the job ended: an unclosed channel leaves it watching a
        # caret blink until the connection's own ceiling expires.
        channel.close(status=status, error=error)


def _preview_sink(kind, channel):
    field = _PREVIEW_FIELDS.get(kind)
    if field is None:
        return None
    return TextStreamSink(field, channel.publish_text, channel.publish_reset)


def _run(job, provider):
    if job.kind == "generate_scenario":
        return _run_generate_scenario(job, provider)
    if job.kind == "converse_turn":
        return _run_converse_turn(job, provider)
    if job.kind == "help_question":
        return _run_help_question(job, provider)
    if job.kind == "generate_tutorial":
        return _run_generate_tutorial(job, provider)
    raise ValueError(f"Unknown job kind: {job.kind}")


def _run_generate_scenario(job, provider):
    from app.training.scenario_service import generate_scenario_for_user

    user = db.session.get(User, job.user_id)
    scenario = generate_scenario_for_user(user, provider, forced_concept_id=job.payload.get("forced_concept_id"))
    return {"scenario_id": scenario.id}


def _run_converse_turn(job, provider):
    from app.training.conversation import run_turn

    attempt = db.session.get(Attempt, job.payload["attempt_id"])
    scenario = db.session.get(Scenario, attempt.scenario_id)
    turn, tutorial_signal = run_turn(attempt, scenario, job.payload["student_message"], provider)

    result = {
        "attempt_id": attempt.id,
        "scenario_id": scenario.id,
        "turn_index": turn.turn_index,
        "conversation_complete": attempt.is_complete,
    }
    if attempt.is_complete:
        result["score"] = attempt.score
        # The tutorial trigger only runs when the conversation closes, so this
        # is only ever present on the final turn's job result.
        result.update(tutorial_signal or {})
    return result


def _run_help_question(job, provider):
    """One side-chat question (§5.6). Runs as a job for the same reason a
    conversation turn does — it's a full LLM call — but the page it belongs to
    never navigates: the student is expected to be typing their real answer
    while this runs, the answer streams into the panel as it is written, and
    htmx swaps the finished, rendered panel in when it lands (§5.7)."""
    from app.training.help import answer_question

    scenario = db.session.get(Scenario, job.payload["scenario_id"])
    exchange = answer_question(scenario, job.user_id, job.payload["question"], provider)
    return {"scenario_id": scenario.id, "exchange_id": exchange.id, "declined": exchange.declined}


def _run_generate_tutorial(job, provider):
    from app.tutorials.generation import generate_tutorial_for_concept

    tutorial = generate_tutorial_for_concept(job.payload["concept_id"], job.payload.get("attempt_id"), provider)
    return {"tutorial_id": tutorial.id, "tutorial_slug": tutorial.slug, "concept_id": tutorial.concept_id}
