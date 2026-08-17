"""Generates the ONE canonical tutorial for a concept, the first time a
student trips the trigger for it (docs §5.4, §6.2). Every later student who
misses the same concept reuses this row for free — see
app/training/gaps.py's _handle_trigger for the reuse path.
"""

import re

from flask import current_app

from app.db import db
from app.models import Attempt, Concept, ConceptEvent, ConceptMastery, Scenario, Tutorial, TutorialRead, TutorialLink, ResourceCitation
from app.llm.client import generate_structured
from app.llm.schemas import TutorialSpec
from app.llm.prompts import build_tutorial_prompt, TUTORIAL_PROMPT_VERSION
from app.tutorials.resources import select_resources

_RESOURCE_MARKER = re.compile(r"\[\[res:([a-z0-9][a-z0-9\-]*)\]\]")


def _gather_source_scenarios(user_id, concept_id, limit=3):
    events = (
        db.session.query(ConceptEvent)
        .filter_by(user_id=user_id, concept_id=concept_id, status="missed")
        .order_by(ConceptEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    pairs = []
    for event in events:
        attempt = db.session.get(Attempt, event.attempt_id)
        if attempt is None:
            continue
        scenario = db.session.get(Scenario, attempt.scenario_id)
        if scenario is None:
            continue
        pairs.append((scenario, attempt))
    return list(reversed(pairs))  # chronological, oldest first, for the prompt


def generate_tutorial_for_concept(concept_id, triggering_attempt_id, provider):
    concept = db.session.get(Concept, concept_id)
    if concept is None:
        raise ValueError(f"Unknown concept_id: {concept_id}")

    triggering_attempt = db.session.get(Attempt, triggering_attempt_id) if triggering_attempt_id else None
    user_id = triggering_attempt.user_id if triggering_attempt else None

    source_scenarios = _gather_source_scenarios(user_id, concept_id) if user_id else []
    if triggering_attempt and not any(a.id == triggering_attempt.id for _, a in source_scenarios):
        scenario = db.session.get(Scenario, triggering_attempt.scenario_id)
        if scenario:
            source_scenarios.append((scenario, triggering_attempt))

    weak_ids = [
        m.concept_id
        for m in db.session.query(ConceptMastery)
        .filter(ConceptMastery.user_id == user_id, ConceptMastery.consecutive_misses >= 1)
        .all()
    ] if user_id else []

    band = source_scenarios[-1][0].band if source_scenarios else 2
    latest_scenario = source_scenarios[-1][0] if source_scenarios else None
    shortlist = select_resources(current_app, concept, weak_concept_ids=weak_ids, scenario=latest_scenario, student_band=band)

    system, user_msg = build_tutorial_prompt(concept, source_scenarios, shortlist)
    spec = generate_structured(provider, "tutorial", system, user_msg, TutorialSpec, user_id or "system")

    # The model may only reference what it was actually shown — a fabricated
    # or merely-real-but-unoffered id is dropped, never rendered. Docs §7.4.
    shortlist_ids = {r["id"] for r in shortlist}
    cited_resource_ids = [rid for rid in spec.cited_resource_ids if rid in shortlist_ids]
    reading_order = [rid for rid in spec.reading_order if rid in shortlist_ids]
    for rid in cited_resource_ids:
        if rid not in reading_order:
            reading_order.append(rid)

    valid_related = set(concept.related)
    related_concept_ids = [cid for cid in spec.related_concept_ids if cid in valid_related]

    # Strip any marker referencing a dropped resource id so it never renders
    # as a dead inline link — docs §14 'Resource-citation tests'.
    def _strip_dropped_marker(match):
        return match.group(0) if match.group(1) in shortlist_ids else ""

    body_md = _RESOURCE_MARKER.sub(_strip_dropped_marker, spec.body_md)

    tutorial = Tutorial(
        concept_id=concept.id,
        slug=concept.id,
        title=spec.title,
        body_md=body_md,
        exercise_md=spec.exercise_md,
        cited_resource_ids_json=cited_resource_ids,
        reading_order_json=reading_order,
        related_concept_ids_json=related_concept_ids,
        source_attempt_ids_json=[a.id for _, a in source_scenarios],
        model=provider.model_name,
        prompt_version=TUTORIAL_PROMPT_VERSION,
    )
    db.session.add(tutorial)
    db.session.flush()

    for cid in related_concept_ids:
        db.session.add(TutorialLink(from_tutorial_id=tutorial.id, to_concept_id=cid, kind="llm", reason=""))

    for position, rid in enumerate(reading_order):
        inline = bool(re.search(rf"\[\[res:{re.escape(rid)}\]\]", body_md))
        db.session.add(ResourceCitation(tutorial_id=tutorial.id, resource_id=rid, position=position, inline=inline))

    if user_id:
        db.session.add(TutorialRead(user_id=user_id, tutorial_id=tutorial.id))
        mastery = db.session.query(ConceptMastery).filter_by(user_id=user_id, concept_id=concept.id).one_or_none()
        if mastery:
            mastery.tutorial_id = tutorial.id

    db.session.commit()
    return tutorial
