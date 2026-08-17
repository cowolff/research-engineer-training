import random

from app.db import db
from app.models import User, ConceptMastery
from app.training.selector import select_training_target, SCENARIO_TYPES, _DEBUGGABLE_TOPICS


def test_forced_concept_bypasses_the_weighted_selector(app):
    with app.app_context():
        db.session.add(User(id="u1", email="m@example.com", password_hash="x"))
        db.session.commit()

        topic, concepts, scenario_type, band = select_training_target(
            db.session.get(User, "u1"), forced_concept_id="password-hashing"
        )
        assert [c.id for c in concepts] == ["password-hashing"]
        assert topic.id == "web-auth"
        assert scenario_type in SCENARIO_TYPES
        assert 1 <= band <= 4


def test_debug_artifact_only_offered_for_debuggable_topics(app):
    """A fabricated log about OAuth scopes or ETL data contracts would read
    as contrived — debug_artifact scenarios are restricted to topics whose
    failures naturally produce inspectable artefacts. Docs §6.1."""
    with app.app_context():
        db.session.add(User(id="u1", email="n@example.com", password_hash="x"))
        db.session.commit()
        user = db.session.get(User, "u1")

        seen_types = set()
        for _ in range(30):
            _, _, scenario_type, _ = select_training_target(user, forced_concept_id="oauth-oidc")
            seen_types.add(scenario_type)

        assert "debug_artifact" not in seen_types
        assert "web-auth" not in _DEBUGGABLE_TOPICS


def test_unseen_essential_concepts_are_eventually_selected(app):
    """With no mastery history at all, the selector must still be able to
    reach every essential concept — not just the ones it happens to weight
    heaviest."""
    with app.app_context():
        db.session.add(User(id="u1", email="o@example.com", password_hash="x"))
        db.session.commit()
        user = db.session.get(User, "u1")

        seen_concepts = set()
        for _ in range(200):
            _, concepts, _, _ = select_training_target(user, n_concepts=3)
            seen_concepts.update(c.id for c in concepts)

        assert len(seen_concepts) > 20  # broad coverage across topics, not stuck on one


def test_struggling_concept_outdraws_an_unseen_optional_concept_in_its_own_topic(app):
    """Selection is two-stage — a topic, then concepts within it — so a
    concept's weight only matters relative to its topic-mates, not against
    the full 57-concept pool. `password-hashing` (struggling, weight 4)
    should be pulled far more often than `mfa` (unseen, optional, weight 1)
    whenever web-auth is the topic drawn, holding topic-selection constant.

    Seeded for determinism: the selector itself is meant to be random, so an
    unseeded run of this test is inherently flaky at any fixed trial count —
    a fixed seed makes the sample reproducible without changing what's under
    test."""
    random.seed(20260817)
    with app.app_context():
        db.session.add(User(id="u1", email="p@example.com", password_hash="x"))
        db.session.flush()
        db.session.add(ConceptMastery(user_id="u1", concept_id="password-hashing", consecutive_misses=2))
        db.session.commit()
        user = db.session.get(User, "u1")

        password_hits = 0
        mfa_hits = 0
        web_auth_draws = 0
        for _ in range(2000):
            topic, concepts, _, _ = select_training_target(user, n_concepts=3)
            if topic.id != "web-auth":
                continue
            web_auth_draws += 1
            ids = {c.id for c in concepts}
            password_hits += "password-hashing" in ids
            mfa_hits += "mfa" in ids

        assert web_auth_draws > 10, "web-auth was never drawn across 400 trials — widen the trial count"
        assert password_hits > mfa_hits * 2
