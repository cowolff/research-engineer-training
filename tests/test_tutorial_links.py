import json

from app.db import db
from app.models import User, Scenario, Attempt
from app.llm.base import LLMUsage
from app.tutorials.generation import generate_tutorial_for_concept


class _FabricatingProvider:
    """A stand-in that cites a resource id it was never shown, and one that
    IS real but wasn't offered — both must be dropped, per docs §7.4/§14."""

    model_name = "fabricator"

    def raw_complete(self, system, user, schema=None):
        spec = {
            "title": "Docker basics",
            "body_md": "Some content. [[res:totally-made-up]] [[res:docker-get-started]]",
            "exercise_md": "Run a container.",
            "related_concept_ids": ["ci-cd", "not-a-real-concept"],
            "cited_resource_ids": ["totally-made-up", "docker-get-started", "kafka-docs"],
            "reading_order": ["totally-made-up", "docker-get-started", "kafka-docs"],
        }
        return json.dumps(spec), LLMUsage()


def test_fabricated_and_unoffered_resource_ids_are_dropped(app):
    with app.app_context():
        db.session.add(User(id="u1", email="j@example.com", password_hash="x"))
        db.session.flush()
        scenario = Scenario(
            user_id="u1",
            topic_id="research-demonstrator-stack",
            type="concept",
            band=1,
            title="Containers",
            prompt_md="...",
            rubric_json=[{"concept_id": "containerization", "expected": "...", "weight": 1, "essential": True}],
            target_concepts_json=["containerization"],
            model="fake",
            prompt_version="grade.v1",
        )
        db.session.add(scenario)
        db.session.flush()
        attempt = Attempt(scenario_id=scenario.id, user_id="u1", answer_text="I don't know.")
        db.session.add(attempt)
        db.session.commit()

        tutorial = generate_tutorial_for_concept("containerization", attempt.id, _FabricatingProvider())

        # "kafka-docs" is a real resource in the index but wasn't part of the
        # shortlist shown for `containerization` — it must be dropped exactly
        # like the fully-invented id.
        assert "totally-made-up" not in tutorial.cited_resource_ids
        assert "kafka-docs" not in tutorial.cited_resource_ids
        assert "docker-get-started" in tutorial.cited_resource_ids

        assert "[[res:totally-made-up]]" not in tutorial.body_md
        assert "[[res:docker-get-started]]" in tutorial.body_md

        # related_concept_ids restricted to the concept's actual `related` list
        assert "not-a-real-concept" not in tutorial.related_concept_ids


def test_generated_tutorial_is_canonical_per_concept(app):
    from app.models import Tutorial

    with app.app_context():
        db.session.add(User(id="u2", email="k@example.com", password_hash="x"))
        db.session.flush()
        scenario = Scenario(
            user_id="u2",
            topic_id="research-demonstrator-stack",
            type="concept",
            band=1,
            title="Containers",
            prompt_md="...",
            rubric_json=[{"concept_id": "containerization", "expected": "...", "weight": 1, "essential": True}],
            target_concepts_json=["containerization"],
            model="fake",
            prompt_version="grade.v1",
        )
        db.session.add(scenario)
        db.session.flush()
        attempt = Attempt(scenario_id=scenario.id, user_id="u2", answer_text="I don't know.")
        db.session.add(attempt)
        db.session.commit()

        from app.llm.fake import FakeProvider

        tutorial = generate_tutorial_for_concept("containerization", attempt.id, FakeProvider())
        assert db.session.query(Tutorial).filter_by(concept_id="containerization").count() == 1
        assert tutorial.slug == "containerization"
