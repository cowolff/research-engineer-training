"""The glossary side chat (§5.6): the two properties that make it safe to put
an LLM next to an ungraded exercise — it cannot leak the answer, and it cannot
move the grade — plus the budgets that keep it from eating the day's quota.
"""

import re

from app.db import db
from app.jobs.dispatch import run_job
from app.models import User, Scenario, Job, HelpExchange, ConversationTurn
from app.llm.fake import FakeProvider
from app.llm.prompts import build_help_prompt, build_converse_prompt
from app.training.conversation import get_or_create_attempt, run_turn
from app.training.help import answer_question, exchanges_for, questions_remaining, HelpLimitReached


def _rubric():
    return [
        {
            "concept_id": "password-hashing",
            "expected": "Notices the passwords are stored with a fast general-purpose digest",
            "weight": 3,
            "essential": True,
        },
        {
            "concept_id": "session-auth",
            "expected": "Mentions server-side sessions over a bearer token in local storage",
            "weight": 1,
            "essential": True,
        },
    ]


def _scenario(user_id="u1"):
    scenario = Scenario(
        user_id=user_id,
        topic_id="web-auth",
        type="debug_artifact",
        band=1,
        title="Logins are slow and the audit failed",
        prompt_md="A colleague's login endpoint takes 900ms and the security audit flagged it.",
        artifacts_json=[{"label": "auth.log", "language": "text", "content": "digest=sha256 rounds=1"}],
        rubric_json=_rubric(),
        target_concepts_json=["password-hashing", "session-auth"],
        model="fake",
        prompt_version="converse.v1",
    )
    db.session.add(scenario)
    db.session.flush()
    return scenario


def _only_job_id(app):
    with app.app_context():
        return db.session.query(Job).filter_by(kind="help_question").one().id


def _user(app_ctx_user_id="u1", email="student@example.com"):
    user = User(id=app_ctx_user_id, email=email, password_hash="x")
    db.session.add(user)
    db.session.flush()
    return user


def test_the_help_prompt_is_never_given_the_rubric_or_the_transcript(app):
    """The load-bearing test for §5.6. "Don't give away the answer" is not
    just an instruction in help.v1.md — the prompt is built only from what the
    student can already see, so there is nothing in it to give away. The
    transcript is withheld for the same reason: every nudge in it names, by
    implication, a rubric item the student hasn't reached.
    """
    with app.app_context():
        _user()
        scenario = _scenario()
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)
        turn, _ = run_turn(attempt, scenario, "Not sure, maybe rate limiting?", FakeProvider())

        system, user_msg = build_help_prompt(scenario, "what does digest mean?", [])
        whole_prompt = system + user_msg

        for item in scenario.rubric:
            assert item["concept_id"] not in whole_prompt
            assert item["expected"] not in whole_prompt
        assert "session-auth" not in whole_prompt
        assert turn.follow_up_question not in whole_prompt
        assert turn.assistant_reply_md not in whole_prompt
        assert "COVERAGE" not in whole_prompt
        assert "RUBRIC" not in whole_prompt

        # But it *does* get what the student is looking at, or it can't
        # disambiguate a term at all.
        assert scenario.prompt_md in user_msg
        assert "digest=sha256" in user_msg


def test_asking_a_question_never_touches_the_grade(app):
    """A side-chat question is not an attempt at the scenario: it earns no
    coverage credit, adds no turn, and — critically — is never replayed into
    the next conversation prompt, where the assessing model could mistake the
    glossary's words for the student's own."""
    with app.app_context():
        _user()
        scenario = _scenario()
        db.session.commit()
        user = db.session.get(User, "u1")
        attempt = get_or_create_attempt(scenario, user)
        run_turn(attempt, scenario, "I think the endpoint is just under load.", FakeProvider())

        coverage_before = dict(attempt.coverage)
        turns_before = attempt.turn_count

        exchange = answer_question(scenario, user.id, "what is a digest?", FakeProvider())

        assert attempt.coverage == coverage_before
        assert attempt.turn_count == turns_before
        assert db.session.query(ConversationTurn).filter_by(attempt_id=attempt.id).count() == turns_before

        _, converse_user_msg = build_converse_prompt(
            scenario, list(attempt.turns), attempt.coverage, "my next answer", turns_remaining=1, is_final_turn=False
        )
        assert exchange.question not in converse_user_msg
        assert exchange.answer_md not in converse_user_msg


def test_a_question_that_asks_for_the_answer_is_sent_back_to_the_conversation(app):
    """FakeProvider mirrors help.v1.md's own rule, so "the side chat refuses
    to do the exercise" stays checkable without a live model."""
    with app.app_context():
        _user()
        scenario = _scenario()
        db.session.commit()

        declined = answer_question(scenario, "u1", "So what's wrong with the login endpoint?", FakeProvider())
        allowed = answer_question(scenario, "u1", "what is a digest?", FakeProvider())

        assert declined.declined is True
        assert "main conversation" in declined.answer_md
        assert allowed.declined is False


def test_a_follow_up_question_sees_the_earlier_exchange(app):
    """It's a chat, not a series of unrelated lookups — "and how is that
    different?" has to resolve to something."""
    with app.app_context():
        _user()
        scenario = _scenario()
        db.session.commit()

        first = answer_question(scenario, "u1", "what is a digest?", FakeProvider())
        _, user_msg = build_help_prompt(scenario, "how is that different from a cipher?", exchanges_for(scenario, "u1"))

        assert first.question in user_msg
        assert first.answer_md in user_msg
        # Still fenced as untrusted text on the way back in, exactly like the
        # conversation history is.
        assert f"<student_question>\n{first.question}\n</student_question>" in user_msg


def test_questions_are_capped_per_scenario(app):
    """The daily quota is shared with actual training, so without a per-scenario
    cap a student could spend the whole allowance on the glossary and have
    nothing left to train with."""
    with app.app_context():
        app.app_config.max_help_questions_per_scenario = 2
        _user()
        scenario = _scenario()
        db.session.commit()

        answer_question(scenario, "u1", "what is a digest?", FakeProvider())
        answer_question(scenario, "u1", "what is a salt?", FakeProvider())

        assert questions_remaining(scenario, "u1") == 0
        try:
            answer_question(scenario, "u1", "what is a nonce?", FakeProvider())
            raise AssertionError("expected the cap to be enforced in the service, not only at the route")
        except HelpLimitReached:
            pass

        assert db.session.query(HelpExchange).count() == 2


def test_the_cap_is_per_scenario_not_global(app):
    with app.app_context():
        app.app_config.max_help_questions_per_scenario = 1
        _user()
        first_scenario = _scenario()
        second_scenario = _scenario()
        db.session.commit()

        answer_question(first_scenario, "u1", "what is a digest?", FakeProvider())

        assert questions_remaining(first_scenario, "u1") == 0
        assert questions_remaining(second_scenario, "u1") == 1


def _login(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True


def _csrf(html):
    return re.search(r'name="csrf_token"[^>]*value="([^"]*)"', html).group(1)


def test_the_panel_renders_beside_the_scenario_and_asking_costs_quota(app, client):
    """Route-level: the question is charged and queued before any LLM call
    (§10), and the response is a fragment, not a page — the student's draft
    answer in the main textarea has to survive."""
    with app.app_context():
        _user(email="route@example.com")
        scenario = _scenario()
        db.session.commit()
        scenario_id = scenario.id

    _login(client, "u1")

    page = client.get(f"/train/{scenario_id}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'id="help-panel"' in html
    assert "Ask about a term" in html

    response = client.post(
        f"/train/{scenario_id}/help",
        data={"csrf_token": _csrf(html), "question": "what is a digest?"},
        headers={"HX-Request": "true"},
    )
    fragment = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<!doctype" not in fragment.lower()  # a fragment: no page navigation
    assert 'id="help-panel"' in fragment
    # In-flight state (§5.7): the question is echoed straight back and the
    # answer streams into the bubble below it, rather than the panel sitting
    # on a placeholder until the whole answer exists.
    assert "what is a digest?" in fragment
    assert f"/jobs/{_only_job_id(app)}/stream" in fragment

    with app.app_context():
        job = db.session.query(Job).filter_by(kind="help_question").one()
        assert job.payload["question"] == "what is a digest?"
        assert job.payload["scenario_id"] == scenario_id
        assert db.session.get(User, "u1").daily_llm_calls == 1  # charged up front


def test_the_answer_lands_in_the_panel_and_nothing_keeps_watching(app, client):
    """End to end through the queue: the route enqueues, the worker answers,
    and the swap that lands when the stream closes leaves no live element
    behind — nothing re-requests once there is nothing left to wait for."""
    with app.app_context():
        _user(email="poll@example.com")
        scenario = _scenario()
        db.session.commit()
        scenario_id = scenario.id

    _login(client, "u1")
    html = client.get(f"/train/{scenario_id}").get_data(as_text=True)
    client.post(
        f"/train/{scenario_id}/help",
        data={"csrf_token": _csrf(html), "question": "what is a digest?"},
        headers={"HX-Request": "true"},
    )

    with app.app_context():
        job = db.session.query(Job).filter_by(kind="help_question").one()
        run_job(job.id)
        assert db.session.get(Job, job.id).status == "done", db.session.get(Job, job.id).error
        job_id = job.id

    polled = client.get(f"/train/{scenario_id}/help?job_id={job_id}").get_data(as_text=True)

    assert "what is a digest?" in polled
    assert "term of art" in polled  # the fake's definition landed in the panel
    assert "hx-trigger" not in polled  # answered, so nothing is still listening
    assert "data-stream-url" not in polled
    assert "4 questions left" in polled  # one of five spent


def test_asking_over_the_cap_is_refused_at_the_route_without_spending_quota(app, client):
    with app.app_context():
        app.app_config.max_help_questions_per_scenario = 1
        _user(email="capped@example.com")
        scenario = _scenario()
        db.session.commit()
        scenario_id = scenario.id
        answer_question(scenario, "u1", "what is a digest?", FakeProvider())
        db.session.commit()

    _login(client, "u1")
    html = client.get(f"/train/{scenario_id}").get_data(as_text=True)
    assert "used your questions" in html  # composer replaced by the cap notice

    response = client.post(
        f"/train/{scenario_id}/help",
        data={"csrf_token": _csrf(html), "question": "what is a nonce?"},
        headers={"HX-Request": "true"},
    )

    assert "used all your questions" in response.get_data(as_text=True)
    with app.app_context():
        assert db.session.query(Job).filter_by(kind="help_question").count() == 0
        assert db.session.get(User, "u1").daily_llm_calls == 0


def test_another_students_scenario_is_not_askable(app, client):
    with app.app_context():
        _user(email="owner@example.com")
        _user("u2", "intruder@example.com")
        scenario = _scenario(user_id="u1")
        db.session.commit()
        scenario_id = scenario.id

    _login(client, "u2")

    # A real CSRF token, so the POST actually reaches the ownership check
    # rather than being turned away at the CSRF layer with a 400.
    token = _csrf(client.get("/dashboard").get_data(as_text=True))

    assert client.get(f"/train/{scenario_id}/help").status_code == 404
    assert client.get(f"/train/{scenario_id}/help?job_id=whatever").status_code == 404
    posted = client.post(
        f"/train/{scenario_id}/help",
        data={"csrf_token": token, "question": "what is a digest?"},
        headers={"HX-Request": "true"},
    )
    assert posted.status_code == 404
    with app.app_context():
        assert db.session.query(Job).count() == 0


def test_the_summary_replays_what_was_looked_up(app, client):
    with app.app_context():
        app.app_config.max_conversation_turns = 1
        _user(email="summary@example.com")
        scenario = _scenario()
        db.session.commit()
        scenario_id = scenario.id
        user = db.session.get(User, "u1")
        answer_question(scenario, "u1", "what is a digest?", FakeProvider())
        attempt = get_or_create_attempt(scenario, user)
        run_turn(attempt, scenario, "Something about password hashing.", FakeProvider())
        assert attempt.is_complete

    _login(client, "u1")
    html = client.get(f"/train/{scenario_id}/feedback").get_data(as_text=True)

    assert "Terms you looked up" in html
    assert "what is a digest?" in html
