from app.db import db
from app.models import User, Scenario, Attempt, Job, ConceptMastery
from app.training.gaps import record_concept_events, apply_dispute


def _scenario(user_id, n):
    scenario = Scenario(
        user_id=user_id,
        topic_id="web-auth",
        type="concept",
        band=1,
        title=f"Scenario {n}",
        prompt_md="...",
        rubric_json=[{"concept_id": "password-hashing", "expected": "...", "weight": 1, "essential": True}],
        target_concepts_json=["password-hashing"],
        model="fake",
        prompt_version="grade.v1",
        dedupe_hash=f"hash{n}",
    )
    db.session.add(scenario)
    db.session.flush()
    return scenario


def _attempt(user_id, scenario):
    attempt = Attempt(scenario_id=scenario.id, user_id=user_id, answer_text="...")
    db.session.add(attempt)
    db.session.flush()
    return attempt


def _missed_items():
    return [{"concept_id": "password-hashing", "status": "missed", "evidence": None, "feedback": "no"}]


def test_three_misses_across_three_scenarios_triggers_one_job(app):
    with app.app_context():
        db.session.add(User(id="u1", email="d@example.com", password_hash="x"))
        db.session.flush()

        for n in range(3):
            scenario = _scenario("u1", n)
            attempt = _attempt("u1", scenario)
            record_concept_events(attempt, scenario, _missed_items())

        jobs = db.session.query(Job).filter_by(kind="generate_tutorial").all()
        assert len(jobs) == 1
        assert jobs[0].payload["concept_id"] == "password-hashing"


def test_three_misses_in_one_scenario_does_not_trigger(app):
    """Repeatedly re-grading the SAME scenario must not count as distinct
    evidence of a gap — docs §6.2's '>= 3 distinct scenarios' clause exists
    exactly to block this."""
    with app.app_context():
        db.session.add(User(id="u2", email="e@example.com", password_hash="x"))
        db.session.flush()

        scenario = _scenario("u2", 0)
        for _ in range(3):
            attempt = _attempt("u2", scenario)
            record_concept_events(attempt, scenario, _missed_items())

        jobs = db.session.query(Job).filter_by(kind="generate_tutorial").all()
        assert len(jobs) == 0
        mastery = db.session.query(ConceptMastery).filter_by(user_id="u2", concept_id="password-hashing").one()
        assert mastery.consecutive_misses == 3


def test_dispute_decrements_consecutive_misses(app):
    with app.app_context():
        db.session.add(User(id="u3", email="f@example.com", password_hash="x"))
        db.session.flush()

        scenario = _scenario("u3", 0)
        attempt = _attempt("u3", scenario)
        attempt.grade_json = {"items": _missed_items()}
        record_concept_events(attempt, scenario, _missed_items())

        mastery = db.session.query(ConceptMastery).filter_by(user_id="u3", concept_id="password-hashing").one()
        assert mastery.consecutive_misses == 1

        apply_dispute(attempt, "password-hashing")

        db.session.refresh(mastery)
        assert mastery.consecutive_misses == 0
        assert attempt.disputed is True

        # Regression: grade_json is read, mutated in place, then reassigned —
        # SQLAlchemy's JSON column compares old vs. new by value, and a
        # naive reassignment after in-place mutation looks like a no-op and
        # silently fails to UPDATE without flag_modified(). Force a real
        # reload from the database to catch that class of bug.
        db.session.expire(attempt)
        reloaded = db.session.get(Attempt, attempt.id)
        assert reloaded.grade["items"][0]["status"] == "covered"


def test_second_student_reuses_existing_tutorial_without_a_new_job(app):
    """Once a canonical tutorial exists for a concept, a second student
    tripping the trigger must NOT spawn a second LLM generation — docs §6.2's
    whole reason for making tutorials shared."""
    from app.models import Tutorial, TutorialRead

    with app.app_context():
        db.session.add(User(id="u4", email="g@example.com", password_hash="x"))
        db.session.add(User(id="u5", email="h@example.com", password_hash="x"))
        db.session.add(
            Tutorial(
                id="t1",
                concept_id="password-hashing",
                slug="password-hashing",
                title="Existing tutorial",
                body_md="...",
            )
        )
        db.session.flush()

        for n in range(3):
            scenario = _scenario("u5", n + 10)
            attempt = _attempt("u5", scenario)
            record_concept_events(attempt, scenario, _missed_items())

        assert db.session.query(Job).filter_by(kind="generate_tutorial").count() == 0
        read = db.session.query(TutorialRead).filter_by(user_id="u5", tutorial_id="t1").one_or_none()
        assert read is not None
