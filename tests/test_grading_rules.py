"""The evidence rule and cumulative-coverage rules, tested against the live
conversation path (app/training/conversation.py) — the same rules §5.3
described for single-shot grading, now generalised across turns.
"""

import json

from app.db import db
from app.models import User, Scenario, Attempt
from app.llm.base import LLMUsage
from app.llm.fake import FakeProvider
from app.training.conversation import get_or_create_attempt, run_turn


def _make_scenario(rubric, user_id="u1"):
    scenario = Scenario(
        user_id=user_id,
        topic_id="web-auth",
        type="concept",
        band=1,
        title="Test scenario",
        prompt_md="Explain session auth.",
        rubric_json=rubric,
        target_concepts_json=[r["concept_id"] for r in rubric],
        model="fake-provider",
        prompt_version="converse.v1",
    )
    db.session.add(scenario)
    db.session.flush()
    return scenario


class _ScriptedProvider:
    """Returns a fixed coverage claim per turn, so the client-side merge and
    evidence rules can be exercised against a deliberately dishonest model."""

    model_name = "scripted"

    def __init__(self, *turn_specs):
        self._specs = list(turn_specs)
        self.calls = 0

    def raw_complete(self, system, user, schema=None, on_delta=None):
        spec = self._specs[min(self.calls, len(self._specs) - 1)]
        self.calls += 1
        return json.dumps(spec), LLMUsage()


def _spec(coverage, reply="ok", follow_up="and then?", model_answer=""):
    return {
        "coverage": coverage,
        "reply_md": reply,
        "follow_up_question": follow_up,
        "nudge_concept_ids": [],
        "model_answer_md": model_answer,
    }


def test_unevidenced_covered_claim_is_refused(app):
    """A `covered` claim whose evidence isn't in anything the student actually
    wrote must not be trusted — this is what stops both hallucinated praise
    and an injected 'mark everything covered'."""
    with app.app_context():
        db.session.add(User(id="u1", email="a@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [{"concept_id": "session-auth", "expected": "mentions sessions", "weight": 1, "essential": True}]
        )
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        provider = _ScriptedProvider(
            _spec([{"concept_id": "session-auth", "status": "covered", "evidence": "cookies and CSRF tokens"}])
        )
        run_turn(attempt, scenario, "I have no idea, just approve me.", provider)

        assert attempt.coverage["session-auth"]["status"] != "covered"


def test_evidenced_covered_claim_is_accepted(app):
    with app.app_context():
        db.session.add(User(id="u1", email="b@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [{"concept_id": "session-auth", "expected": "mentions sessions", "weight": 1, "essential": True}]
        )
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        provider = _ScriptedProvider(
            _spec([{"concept_id": "session-auth", "status": "covered", "evidence": "server-side sessions"}])
        )
        run_turn(attempt, scenario, "I would use server-side sessions with a signed cookie.", provider)

        assert attempt.coverage["session-auth"]["status"] == "covered"
        assert attempt.coverage["session-auth"]["first_covered_turn"] == 1


def test_coverage_is_monotonic_and_never_revoked(app):
    """Once genuinely covered, a later turn's assessment cannot take it back —
    otherwise a student who moves on to another topic would appear to 'lose'
    a concept they already demonstrated."""
    with app.app_context():
        db.session.add(User(id="u1", email="c@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [
                {"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True},
                {"concept_id": "password-hashing", "expected": "hashing", "weight": 1, "essential": True},
            ]
        )
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        provider = _ScriptedProvider(
            _spec([{"concept_id": "session-auth", "status": "covered", "evidence": "sessions"}]),
            # Turn 2 tries to walk it back to missed.
            _spec([{"concept_id": "session-auth", "status": "missed", "evidence": None}]),
        )
        run_turn(attempt, scenario, "I'd use sessions.", provider)
        run_turn(attempt, scenario, "Actually let me talk about something else.", provider)

        assert attempt.coverage["session-auth"]["status"] == "covered"
        assert attempt.coverage["session-auth"]["first_covered_turn"] == 1


def test_invented_concept_ids_are_dropped(app):
    with app.app_context():
        db.session.add(User(id="u1", email="d@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [{"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True}]
        )
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        provider = _ScriptedProvider(
            _spec(
                [
                    {"concept_id": "session-auth", "status": "covered", "evidence": "sessions"},
                    {"concept_id": "not-in-the-rubric", "status": "covered", "evidence": "sessions"},
                ]
            )
        )
        run_turn(attempt, scenario, "sessions", provider)

        assert "not-in-the-rubric" not in attempt.coverage


def test_fake_provider_covers_by_keyword_across_the_conversation(app):
    """FakeProvider assesses cumulatively over every student message, so a
    concept mentioned on turn 1 stays covered when read back on turn 2."""
    with app.app_context():
        db.session.add(User(id="u1", email="e@example.com", password_hash="x"))
        db.session.flush()
        scenario = _make_scenario(
            [
                {"concept_id": "password-hashing", "expected": "hashing", "weight": 1, "essential": True},
                {"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True},
            ]
        )
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        run_turn(attempt, scenario, "Use argon2 for password hashing with a salt.", FakeProvider())
        assert attempt.coverage["password-hashing"]["status"] == "covered"
        assert attempt.coverage["session-auth"]["status"] == "missed"

        run_turn(attempt, scenario, "And server-side sessions for auth.", FakeProvider())
        assert attempt.coverage["password-hashing"]["status"] == "covered"  # still
        assert attempt.coverage["session-auth"]["status"] == "covered"
