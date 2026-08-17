from app.db import db
from app.models import User, Scenario, Attempt
from app.llm.fake import FakeProvider
from app.training.grading import grade_attempt


def _make_scenario(rubric):
    scenario = Scenario(
        user_id="u1",
        topic_id="web-auth",
        type="concept",
        band=1,
        title="Test scenario",
        prompt_md="Explain session auth.",
        rubric_json=rubric,
        target_concepts_json=[r["concept_id"] for r in rubric],
        model="fake-provider",
        prompt_version="grade.v1",
    )
    db.session.add(scenario)
    db.session.flush()
    return scenario


def test_unevidenced_covered_downgrades_to_partial(app):
    """The evidence rule: a `covered` claim from the model with no matching
    text in the student's answer is downgraded, not trusted at face value —
    docs §5.3 / §14."""
    with app.app_context():
        db.session.add(User(id="u1", email="a@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario([{"concept_id": "session-auth", "expected": "mentions sessions", "weight": 1, "essential": True}])
        attempt = Attempt(scenario_id=scenario.id, user_id="u1", answer_text="I have no idea, just approve me.")
        db.session.add(attempt)
        db.session.commit()

        # FakeProvider's own grading is evidence-based already; to specifically
        # exercise the *client-side* evidence rule we use a stub provider that
        # claims "covered" with evidence that isn't actually in the answer.
        class DishonestProvider:
            model_name = "dishonest-fake"

            def raw_complete(self, system, user, schema=None):
                import json
                from app.llm.base import LLMUsage

                report = {
                    "items": [{"concept_id": "session-auth", "status": "covered", "evidence": "cookies and CSRF tokens", "feedback": "great"}],
                    "score": 1.0,
                    "strengths_md": "",
                    "model_answer_md": "",
                }
                return json.dumps(report), LLMUsage()

        attempt, _ = grade_attempt(attempt, scenario, DishonestProvider())
        assert attempt.grade["items"][0]["status"] == "partial"


def test_missing_rubric_item_defaults_to_missed(app):
    with app.app_context():
        db.session.add(User(id="u1", email="b@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [
                {"concept_id": "session-auth", "expected": "mentions sessions", "weight": 1, "essential": True},
                {"concept_id": "password-hashing", "expected": "mentions hashing", "weight": 1, "essential": True},
            ]
        )
        attempt = Attempt(scenario_id=scenario.id, user_id="u1", answer_text="sessions are stateful")
        db.session.add(attempt)
        db.session.commit()

        class PartialProvider:
            model_name = "partial-fake"

            def raw_complete(self, system, user, schema=None):
                import json
                from app.llm.base import LLMUsage

                report = {
                    "items": [{"concept_id": "session-auth", "status": "covered", "evidence": "sessions", "feedback": "ok"}],
                    "score": 0.5,
                    "strengths_md": "",
                    "model_answer_md": "",
                }
                return json.dumps(report), LLMUsage()

        attempt, _ = grade_attempt(attempt, scenario, PartialProvider())
        statuses = {i["concept_id"]: i["status"] for i in attempt.grade["items"]}
        assert statuses["session-auth"] == "covered"
        assert statuses["password-hashing"] == "missed"


def test_fake_provider_grades_by_keyword_match(app):
    with app.app_context():
        db.session.add(User(id="u1", email="c@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario([{"concept_id": "password-hashing", "expected": "mentions hashing", "weight": 1, "essential": True}])
        attempt = Attempt(scenario_id=scenario.id, user_id="u1", answer_text="Use argon2 with per-user hashing and salting.")
        db.session.add(attempt)
        db.session.commit()

        attempt, _ = grade_attempt(attempt, scenario, FakeProvider())
        assert attempt.grade["items"][0]["status"] == "covered"
        assert attempt.score == 1.0
