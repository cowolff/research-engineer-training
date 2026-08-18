"""Conversation mechanics: the turn budget, early exit, and the rule that
decides what a nudged-into-it concept is worth in the gap ledger (§5.5/§6.2).
"""

from app.db import db
from app.models import User, Scenario, Job, ConceptEvent, ConceptMastery
from app.llm.fake import FakeProvider
from app.training.conversation import get_or_create_attempt, run_turn


def _scenario(rubric, user_id="u1"):
    scenario = Scenario(
        user_id=user_id,
        topic_id="web-auth",
        type="concept",
        band=1,
        title="Auth scenario",
        prompt_md="Design the login flow.",
        rubric_json=rubric,
        target_concepts_json=[r["concept_id"] for r in rubric],
        model="fake",
        prompt_version="converse.v1",
    )
    db.session.add(scenario)
    db.session.flush()
    return scenario


def _two_essentials():
    return [
        {"concept_id": "password-hashing", "expected": "hashing", "weight": 1, "essential": True},
        {"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True},
    ]


def test_conversation_closes_early_once_all_essentials_are_covered(app):
    """Wrapping up immediately respects the student's time — and each extra
    turn is a full LLM call, so filler turns are expensive as well as dull."""
    with app.app_context():
        app.app_config.max_conversation_turns = 5
        db.session.add(User(id="u1", email="a@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        # One message that hits both essential concepts.
        turn, signal = run_turn(
            attempt, scenario, "Use argon2 password hashing, and server-side sessions.", FakeProvider()
        )

        assert attempt.is_complete
        assert attempt.turn_count == 1
        assert signal is not None  # the gap ledger ran
        assert turn.follow_up_question == ""  # no dangling nudge on a closing turn


def test_conversation_closes_when_the_turn_budget_runs_out(app):
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="b@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        for i in range(3):
            run_turn(attempt, scenario, f"I really don't know, guess {i}.", FakeProvider())

        assert attempt.is_complete
        assert attempt.turn_count == 3
        assert attempt.coverage["password-hashing"]["status"] == "missed"


def test_gap_ledger_is_written_exactly_once_at_the_end(app):
    """Writing per-turn would log a `missed` event for a concept the student
    then reached on a later turn, inflating the miss counters and firing
    tutorials for gaps that actually closed during the conversation."""
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="c@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        run_turn(attempt, scenario, "Something about hashing.", FakeProvider())
        assert db.session.query(ConceptEvent).count() == 0  # nothing yet, mid-conversation

        run_turn(attempt, scenario, "Oh — and sessions.", FakeProvider())
        assert attempt.is_complete

        # Exactly one event per rubric item, no duplicates from earlier turns.
        events = db.session.query(ConceptEvent).all()
        assert len(events) == 2
        assert {e.concept_id for e in events} == {"password-hashing", "session-auth"}


def test_concept_reached_only_after_a_nudge_counts_as_partial(app):
    """Turn 1 unaided earns mastery credit; reached later after a nudge is
    `partial` — neutral, so it neither builds mastery nor feeds the tutorial
    trigger. See §6.2 on why `partial` leaves the counters untouched."""
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="d@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        run_turn(attempt, scenario, "I'd start with password hashing.", FakeProvider())  # turn 1
        run_turn(attempt, scenario, "Also sessions, I suppose.", FakeProvider())  # turn 2 -> completes

        statuses = {item["concept_id"]: item["status"] for item in attempt.grade["items"]}
        assert statuses["password-hashing"] == "covered"  # unaided, turn 1
        assert statuses["session-auth"] == "partial"  # only reached on turn 2

        # Mastery: covered increments `covers`; partial leaves both counters alone.
        mastery = {m.concept_id: m for m in db.session.query(ConceptMastery).filter_by(user_id="u1").all()}
        assert mastery["password-hashing"].covers == 1
        assert mastery["session-auth"].covers == 0
        assert mastery["session-auth"].misses == 0


def test_nudged_concept_does_not_count_toward_the_tutorial_trigger(app):
    """The point of the `partial` choice: a student who keeps getting there
    with help never accumulates the 3 misses that would spawn a tutorial."""
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="e@example.com", password_hash="x"))
        db.session.flush()
        db.session.commit()
        user = db.session.get(User, "u1")

        for n in range(4):
            scenario = _scenario(_two_essentials())
            db.session.commit()
            attempt = get_or_create_attempt(scenario, user)
            run_turn(attempt, scenario, "Starting with password hashing.", FakeProvider())
            run_turn(attempt, scenario, "And sessions too.", FakeProvider())
            assert attempt.is_complete

        assert db.session.query(Job).filter_by(kind="generate_tutorial").count() == 0


def test_first_message_is_preserved_as_the_unassisted_attempt(app):
    """Tutorial generation quotes `answer_text` back at the student — it has
    to be their own first attempt, not a later already-nudged message."""
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="f@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        run_turn(attempt, scenario, "My honest first guess.", FakeProvider())
        run_turn(attempt, scenario, "A much better nudged answer about sessions.", FakeProvider())

        assert attempt.answer_text == "My honest first guess."


def test_turns_are_recorded_in_order_with_the_nudge_that_was_offered(app):
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="g@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        run_turn(attempt, scenario, "No idea at all.", FakeProvider())

        turns = list(attempt.turns)
        assert [t.turn_index for t in turns] == [1]
        assert turns[0].follow_up_question  # mid-conversation, so a nudge exists
        assert turns[0].nudge_concept_ids  # and it records what it was steering at


def test_nudge_never_names_the_target_concept(app):
    """The central pedagogical rule: a nudge that names the concept hands over
    the answer. Asserted on FakeProvider, which mirrors the real prompt's
    instruction, so the rule stays checkable without a live model."""
    with app.app_context():
        app.app_config.max_conversation_turns = 3
        db.session.add(User(id="u1", email="h@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)

        turn, _ = run_turn(attempt, scenario, "No idea.", FakeProvider())

        for concept_id in turn.nudge_concept_ids:
            assert concept_id not in turn.follow_up_question
            for token in concept_id.split("-"):
                if len(token) > 3:
                    assert token not in turn.follow_up_question.lower()


def test_a_new_conversation_starts_after_the_previous_one_completed(app):
    with app.app_context():
        app.app_config.max_conversation_turns = 1
        db.session.add(User(id="u1", email="i@example.com", password_hash="x"))
        db.session.flush()
        scenario = _scenario(_two_essentials())
        db.session.commit()
        user = db.session.get(User, "u1")

        first = get_or_create_attempt(scenario, user)
        run_turn(first, scenario, "Guess.", FakeProvider())
        assert first.is_complete

        second = get_or_create_attempt(scenario, user)
        assert second.id != first.id
        assert second.status == "in_progress"
        assert second.turn_count == 0
