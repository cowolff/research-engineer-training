import hashlib

from app.db import db
from app.models import Scenario
from app.llm.client import generate_structured, LLMGenerationError
from app.llm.schemas import ScenarioSpec
from app.llm.prompts import build_scenario_prompt, SCENARIO_PROMPT_VERSION
from app.training.selector import select_training_target, weak_concept_ids


def _dedupe_hash(spec):
    return hashlib.sha256(spec.prompt_md.encode()).hexdigest()[:16]


def generate_scenario_for_user(user, provider, forced_concept_id=None):
    topic, concepts, scenario_type, band = select_training_target(user, forced_concept_id=forced_concept_id)
    weak_ids = weak_concept_ids(user)
    recent_hashes = [
        s.dedupe_hash
        for s in db.session.query(Scenario)
        .filter_by(user_id=user.id)
        .order_by(Scenario.created_at.desc())
        .limit(20)
        .all()
    ]

    system, user_msg = build_scenario_prompt(topic, band, scenario_type, concepts, weak_ids, recent_hashes)
    spec = generate_structured(provider, "scenario", system, user_msg, ScenarioSpec, user.id)

    # The model was explicitly told to use only these ids (app/llm/prompts/scenario.v1.md);
    # this is what makes that instruction load-bearing rather than aspirational.
    valid_ids = {c.id for c in concepts}
    filtered_rubric = [item for item in spec.rubric if item.concept_id in valid_ids]
    if not filtered_rubric:
        raise LLMGenerationError("Scenario generation produced no rubric items with valid concept ids")

    scenario = Scenario(
        user_id=user.id,
        topic_id=topic.id,
        type=spec.type,
        band=band,
        title=spec.title,
        prompt_md=spec.prompt_md,
        artifacts_json=[a.model_dump() for a in spec.artifacts],
        rubric_json=[item.model_dump() for item in filtered_rubric],
        target_concepts_json=[c.id for c in concepts],
        model=provider.model_name,
        prompt_version=SCENARIO_PROMPT_VERSION,
        dedupe_hash=_dedupe_hash(spec),
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario
