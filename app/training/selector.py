"""Deterministic, LLM-free selection of what to train next — docs §6.1.
Weighted sampling across four pools, then a topic, then 2-4 concepts within
it, then a scenario type, then a difficulty band."""

import random
from collections import defaultdict

from app.db import db
from app.models import Concept, ConceptMastery, Topic, Attempt

SCENARIO_TYPES = ("architecture", "debug_artifact", "design_review", "tradeoff", "concept")

# Topics whose failures naturally produce inspectable artefacts (a log, a
# metrics table, a stack trace). debug_artifact scenarios are only offered
# for concepts in these topics — asking for a fabricated "log" about OAuth
# scopes or ETL data contracts would read as contrived.
_DEBUGGABLE_TOPICS = {
    "research-demonstrator-stack",
    "ml-infra",
    "databases",
    "deployment-ops",
    "observability",
}


def _concept_weight(concept, mastery):
    if mastery and mastery.consecutive_misses >= 1 and not mastery.tutorial_id:
        return 4.0  # due for review
    if mastery is None and concept.essential:
        return 3.0  # never seen, essential
    if mastery and mastery.tutorial_id and mastery.consecutive_misses == 0:
        return 2.0  # tutorialised — re-test to confirm it landed
    if mastery and mastery.covers >= 3 and mastery.consecutive_misses == 0:
        return 0.5  # mastered — sample rarely
    return 1.0  # everything else gets a baseline chance to appear


def _weighted_sample(pairs, k):
    """Sample up to k items without replacement, weighted."""
    pool = list(pairs)
    chosen = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        if total <= 0:
            break
        r = random.uniform(0, total)
        upto = 0.0
        for i, (item, w) in enumerate(pool):
            upto += w
            if upto >= r:
                chosen.append(item)
                pool.pop(i)
                break
    return chosen


def _recent_avg_score(user):
    rows = (
        db.session.query(Attempt.score)
        .filter(Attempt.user_id == user.id, Attempt.score.isnot(None))
        .order_by(Attempt.submitted_at.desc())
        .limit(5)
        .all()
    )
    scores = [r[0] for r in rows]
    return sum(scores) / len(scores) if scores else None


def select_training_target(user, n_concepts=3, forced_concept_id=None):
    """Returns (topic, concepts, scenario_type, band).

    `forced_concept_id` is set from a tutorial's "train this now" chip
    (docs §6.3) — it bypasses the weighted selector entirely and always
    includes that concept, still picking type/band the normal way.
    """
    concepts = db.session.query(Concept).all()
    if not concepts:
        raise RuntimeError("No concepts loaded — run `flask seed-curriculum` first")

    if forced_concept_id:
        forced = db.session.get(Concept, forced_concept_id)
        if forced is not None:
            topic = db.session.get(Topic, forced.topic_id)
            candidate_types = list(SCENARIO_TYPES)
            if topic.id not in _DEBUGGABLE_TOPICS:
                candidate_types.remove("debug_artifact")
            avg_score = _recent_avg_score(user)
            band = topic.band
            if avg_score is not None:
                band = min(4, band + 1) if avg_score >= 0.8 else (max(1, band - 1) if avg_score < 0.4 else band)
            return topic, [forced], random.choice(candidate_types), band

    masteries = {m.concept_id: m for m in db.session.query(ConceptMastery).filter_by(user_id=user.id).all()}

    weighted = [(c, _concept_weight(c, masteries.get(c.id))) for c in concepts]

    by_topic = defaultdict(list)
    for c, w in weighted:
        by_topic[c.topic_id].append((c, w))

    topic_weights = [(topic_id, sum(w for _, w in items)) for topic_id, items in by_topic.items()]
    chosen_topic_id = _weighted_sample(topic_weights, 1)[0]
    topic = db.session.get(Topic, chosen_topic_id)

    chosen_concepts = _weighted_sample(by_topic[chosen_topic_id], n_concepts)
    if not chosen_concepts:
        chosen_concepts = [by_topic[chosen_topic_id][0][0]]

    candidate_types = list(SCENARIO_TYPES)
    if chosen_topic_id not in _DEBUGGABLE_TOPICS:
        candidate_types.remove("debug_artifact")
    scenario_type = random.choice(candidate_types)

    avg_score = _recent_avg_score(user)
    band = topic.band
    if avg_score is not None:
        if avg_score >= 0.8:
            band = min(4, band + 1)
        elif avg_score < 0.4:
            band = max(1, band - 1)

    return topic, chosen_concepts, scenario_type, band


def weak_concept_ids(user):
    return [
        m.concept_id
        for m in db.session.query(ConceptMastery)
        .filter(ConceptMastery.user_id == user.id, ConceptMastery.consecutive_misses >= 1)
        .all()
    ]
