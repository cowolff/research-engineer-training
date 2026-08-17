import logging
from datetime import datetime

from app.db import db
from app.models import Job, User, Scenario, Attempt

logger = logging.getLogger("app.jobs")


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

    try:
        if job.kind == "generate_scenario":
            result = _run_generate_scenario(job, provider)
        elif job.kind == "grade_attempt":
            result = _run_grade_attempt(job, provider)
        elif job.kind == "generate_tutorial":
            result = _run_generate_tutorial(job, provider)
        else:
            raise ValueError(f"Unknown job kind: {job.kind}")
        job.status = "done"
        job.result_json = result
    except Exception as exc:  # noqa: BLE001 - a job failure must never crash the worker thread
        logger.exception("job_failed", extra={"extra_fields": {"job_id": job.id, "kind": job.kind}})
        job.status = "failed"
        job.error = str(exc)[:2000]

    job.finished_at = datetime.utcnow()
    db.session.commit()


def _run_generate_scenario(job, provider):
    from app.training.scenario_service import generate_scenario_for_user

    user = db.session.get(User, job.user_id)
    scenario = generate_scenario_for_user(user, provider, forced_concept_id=job.payload.get("forced_concept_id"))
    return {"scenario_id": scenario.id}


def _run_grade_attempt(job, provider):
    from app.training.grading import grade_attempt

    attempt = db.session.get(Attempt, job.payload["attempt_id"])
    scenario = db.session.get(Scenario, attempt.scenario_id)
    attempt, tutorial_signal = grade_attempt(attempt, scenario, provider)
    return {"attempt_id": attempt.id, "scenario_id": scenario.id, "score": attempt.score, **tutorial_signal}


def _run_generate_tutorial(job, provider):
    from app.tutorials.generation import generate_tutorial_for_concept

    tutorial = generate_tutorial_for_concept(job.payload["concept_id"], job.payload.get("attempt_id"), provider)
    return {"tutorial_id": tutorial.id, "tutorial_slug": tutorial.slug, "concept_id": tutorial.concept_id}
