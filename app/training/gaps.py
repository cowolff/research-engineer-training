"""The gap ledger: turns a grade into concept_events, keeps concept_mastery in
sync, and decides when a concept trigger fires a tutorial job. See docs
§6.2 and §4.3 on why concept_events is the source of truth and
concept_mastery is a derived rollup that can always be rebuilt from it.
"""

from datetime import datetime

from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

from app.db import db
from app.models import Attempt, Concept, ConceptEvent, ConceptMastery, Job, Tutorial, TutorialRead


def _get_or_create_mastery(user_id, concept_id):
    mastery = db.session.query(ConceptMastery).filter_by(user_id=user_id, concept_id=concept_id).one_or_none()
    if mastery is None:
        mastery = ConceptMastery(user_id=user_id, concept_id=concept_id)
        db.session.add(mastery)
        db.session.flush()
    return mastery


def record_concept_events(attempt, scenario, final_items):
    """One transaction: insert the ledger rows, update the rollup, and enqueue
    (or skip) a tutorial job. Returns which concepts triggered something, so
    the caller can surface it in the job result the frontend polls."""
    cfg = current_app.app_config
    triggered = []

    for item in final_items:
        concept = db.session.get(Concept, item["concept_id"])
        if concept is None:
            continue  # defensive — the rubric was already filtered against the registry upstream

        db.session.add(
            ConceptEvent(
                user_id=attempt.user_id,
                concept_id=concept.id,
                attempt_id=attempt.id,
                status=item["status"],
                essential=concept.essential,
            )
        )

        mastery = _get_or_create_mastery(attempt.user_id, concept.id)
        mastery.last_event_at = datetime.utcnow()

        if item["status"] == "missed":
            mastery.misses += 1
            mastery.consecutive_misses += 1
            scenarios = set(mastery.distinct_miss_scenarios_json or [])
            scenarios.add(scenario.id)
            mastery.distinct_miss_scenarios_json = list(scenarios)
        elif item["status"] == "covered":
            mastery.covers += 1
            mastery.consecutive_misses = 0
            mastery.distinct_miss_scenarios_json = []
        # partial: counters untouched, per docs §6.2.

        db.session.flush()

        distinct_scenarios = len(mastery.distinct_miss_scenarios_json or [])
        if concept.essential and mastery.consecutive_misses >= cfg.miss_threshold and distinct_scenarios >= 3:
            signal = _handle_trigger(attempt.user_id, concept, mastery, attempt)
            if signal:
                triggered.append(signal)

    db.session.commit()
    return {"triggered": triggered}


def _handle_trigger(user_id, concept, mastery, attempt):
    existing = db.session.query(Tutorial).filter_by(concept_id=concept.id).one_or_none()

    if existing:
        already = db.session.query(TutorialRead).filter_by(user_id=user_id, tutorial_id=existing.id).one_or_none()
        if already:
            return None  # already assigned — don't re-notify on every subsequent miss
        db.session.add(TutorialRead(user_id=user_id, tutorial_id=existing.id))
        mastery.tutorial_id = existing.id
        db.session.commit()
        return {"concept_id": concept.id, "mode": "existing", "tutorial_slug": existing.slug}

    # No canonical tutorial yet. Guard against a second student tripping the
    # same trigger while generation for concept X is already in flight.
    in_flight = (
        db.session.query(Job)
        .filter(Job.kind == "generate_tutorial", Job.status.in_(["queued", "running"]))
        .all()
    )
    for job in in_flight:
        if job.payload.get("concept_id") == concept.id:
            return {"concept_id": concept.id, "mode": "pending"}

    job = Job(
        user_id=user_id,
        kind="generate_tutorial",
        payload_json={"concept_id": concept.id, "attempt_id": attempt.id},
    )
    db.session.add(job)
    db.session.commit()
    return {"concept_id": concept.id, "mode": "generating", "job_id": job.id}


def apply_dispute(attempt, concept_id):
    """'I did cover this' — writes a correcting event rather than mutating
    history, per docs §6.2. Keeps students from being lectured about
    something they actually said."""
    items = (attempt.grade_json or {}).get("items", [])
    target = next((i for i in items if i["concept_id"] == concept_id), None)
    if target is None or target["status"] == "covered":
        raise ValueError("No disputable (non-covered) item for that concept on this attempt")

    target["status"] = "covered"
    target["feedback"] = (target.get("feedback") or "") + " (marked covered after student dispute)"
    attempt.grade_json = {"items": items}
    # `items` is the SAME list object already inside attempt.grade_json (from
    # the .get() above), mutated in place before this reassignment — so by
    # the time SQLAlchemy compares old vs. new value they're already equal,
    # and a plain attribute set silently no-ops instead of emitting an
    # UPDATE. flag_modified forces it regardless of that equality check.
    flag_modified(attempt, "grade_json")
    attempt.disputed = True

    concept = db.session.get(Concept, concept_id)
    db.session.add(
        ConceptEvent(
            user_id=attempt.user_id,
            concept_id=concept_id,
            attempt_id=attempt.id,
            status="covered",
            essential=concept.essential if concept else False,
        )
    )

    mastery = _get_or_create_mastery(attempt.user_id, concept_id)
    mastery.covers += 1
    mastery.consecutive_misses = max(0, mastery.consecutive_misses - 1)
    scenarios = set(mastery.distinct_miss_scenarios_json or [])
    scenarios.discard(attempt.scenario_id)
    mastery.distinct_miss_scenarios_json = list(scenarios)
    mastery.last_event_at = datetime.utcnow()

    db.session.commit()


def recompute_all_mastery():
    """The escape hatch when concept_mastery and concept_events disagree:
    concept_events is the source of truth (docs §4.3), so this wipes the
    rollup and replays it from the ledger, in order."""
    db.session.query(ConceptMastery).delete()
    db.session.commit()

    events = db.session.query(ConceptEvent).order_by(ConceptEvent.created_at.asc()).all()
    seen_pairs = set()
    for event in events:
        mastery = _get_or_create_mastery(event.user_id, event.concept_id)
        seen_pairs.add((event.user_id, event.concept_id))

        if event.status == "missed":
            mastery.misses += 1
            mastery.consecutive_misses += 1
            attempt = db.session.get(Attempt, event.attempt_id)
            if attempt:
                scenarios = set(mastery.distinct_miss_scenarios_json or [])
                scenarios.add(attempt.scenario_id)
                mastery.distinct_miss_scenarios_json = list(scenarios)
        elif event.status == "covered":
            mastery.covers += 1
            mastery.consecutive_misses = 0
            mastery.distinct_miss_scenarios_json = []
        mastery.last_event_at = event.created_at

    db.session.commit()
    return len(seen_pairs)
