"""Token streaming (§5.7): the reply appears as it is written, in place.

The load-bearing property under all of this is that streaming is a *display*
concern layered onto structured generation, not a replacement for it. What is
persisted and graded still comes from the schema-validated object; the stream
only decides what the student watches while that is being produced. Several
tests below exist to pin exactly that.
"""

import json
import re

import pytest

from app.db import db
from app.jobs import stream as job_stream
from app.jobs.dispatch import run_job
from app.llm.schemas import ConversationTurnSpec
from app.llm.streaming import JsonFieldStreamer, TextStreamSink, streaming_to
from app.llm.client import generate_structured
from app.models import User, Scenario, Job, ConversationTurn, HelpExchange
from app.training.conversation import get_or_create_attempt


# --- the incremental JSON extractor --------------------------------------


def _feed(document, field, chunk_size):
    streamer = JsonFieldStreamer(field)
    return "".join(
        streamer.feed(document[i : i + chunk_size]) for i in range(0, len(document), chunk_size)
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_the_field_is_recovered_whatever_the_chunk_boundaries(chunk_size):
    """A chunk boundary is arbitrary — it lands mid-word, mid-escape and
    mid-`\\uXXXX` with no warning, and every one of those has to reassemble
    into exactly the string the parsed JSON would have given."""
    reply = 'Line one.\nLine two with "quotes", a backslash \\, and café \U0001f600.\tTabbed.'
    document = json.dumps({"coverage": [], "reply_md": reply, "follow_up_question": "And why?"})

    assert _feed(document, "reply_md", chunk_size) == reply


@pytest.mark.parametrize("chunk_size", [1, 3, 8])
def test_a_field_name_quoted_inside_an_earlier_value_is_not_mistaken_for_the_field(chunk_size):
    """`evidence` quotes the student verbatim, and a student can type anything
    — including the name of the field we are looking for. Matching on a bare
    substring would start streaming the student's own words back at them."""
    document = json.dumps(
        {
            "coverage": [{"concept_id": "c1", "status": "missed", "evidence": 'I wrote "reply_md": "hi"'}],
            "reply_md": "The actual reply.",
        }
    )

    assert _feed(document, "reply_md", chunk_size) == "The actual reply."


def test_nothing_is_released_until_an_escape_is_complete():
    """The half of a `\\n` that has arrived is not text — releasing it would
    put a literal backslash on screen and a stray `n` after it."""
    streamer = JsonFieldStreamer("reply_md")
    assert streamer.feed('{"reply_md": "a') == "a"
    assert streamer.feed("\\") == ""  # an escape has started; its meaning is unknown
    assert streamer.feed("n") == "\n"

    assert streamer.feed("\\u00e") == ""  # \uXXXX still one hex digit short
    assert streamer.feed("9") == "é"


def test_a_surrogate_pair_split_across_chunks_is_held_back_until_it_pairs():
    """A lone leading surrogate cannot be encoded as UTF-8, so releasing one
    would break the SSE frame carrying it rather than merely look wrong."""
    streamer = JsonFieldStreamer("reply_md")
    assert streamer.feed('{"reply_md": "hi \\ud83d') == "hi "
    assert streamer.feed('\\ude00"') == "\U0001f600"


def test_the_stream_stops_at_the_end_of_the_field():
    streamer = JsonFieldStreamer("reply_md")
    streamer.feed('{"reply_md": "done", "model_answer_md": "not this"}')
    assert streamer.finished
    assert streamer.feed('{"reply_md": "more"}') == ""


# --- the sink and the retry it has to survive ----------------------------


class _ScriptedProvider:
    """Streams whatever it is told to, in the same small pieces a real backend
    would. The first document is deliberately schema-invalid, to drive
    app/llm/client.py's one retry."""

    model_name = "scripted"

    def __init__(self, *documents):
        self._documents = list(documents)

    def raw_complete(self, system, user, schema=None, on_delta=None):
        from app.llm.base import LLMUsage

        document = self._documents.pop(0)
        if on_delta is not None:
            for i in range(0, len(document), 4):
                on_delta(document[i : i + 4])
        return document, LLMUsage()


def test_a_validation_retry_clears_what_was_already_on_screen(app):
    """The retry regenerates from the top (§5.1), so the abandoned attempt's
    text must not be left for the second one to append to — the student would
    read one reply spliced onto another."""
    published = []
    invalid = json.dumps({"reply_md": "First attempt, wrong shape.", "coverage": "not a list"})
    valid = json.dumps(
        {"coverage": [], "reply_md": "Second attempt.", "follow_up_question": "", "nudge_concept_ids": []}
    )

    with app.app_context():
        db.session.add(User(id="u1", email="s@example.com", password_hash="x"))
        db.session.commit()

        sink = TextStreamSink(
            "reply_md", published.append, lambda: published.clear()
        )
        with streaming_to(sink):
            spec = generate_structured(
                _ScriptedProvider(invalid, valid), "converse", "sys", "usr", ConversationTurnSpec, "u1"
            )

        assert spec.reply_md == "Second attempt."
        assert "".join(published) == "Second attempt."


def test_generation_is_unaffected_when_nobody_is_watching(app):
    """No sink installed is the ordinary case for anything not driven by a
    browser (the CLI, a test, a tutorial generated in the background)."""
    with app.app_context():
        db.session.add(User(id="u1", email="s@example.com", password_hash="x"))
        db.session.commit()

        valid = json.dumps({"coverage": [], "reply_md": "Plain.", "follow_up_question": ""})
        spec = generate_structured(
            _ScriptedProvider(valid), "converse", "sys", "usr", ConversationTurnSpec, "u1"
        )
        assert spec.reply_md == "Plain."


# --- the broker ----------------------------------------------------------


def test_a_late_subscriber_still_receives_the_whole_reply():
    """The browser opens its EventSource only after the POST has returned, so
    the first tokens are always generated before anyone is listening."""
    job_stream.reset_all()
    channel = job_stream.open_channel("j1")
    channel.publish_text("Hel")
    channel.publish_text("lo")
    channel.close(status="done")

    events, exhausted = channel.read(0, timeout=0)

    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello"
    assert events[-1] == {"type": "done", "status": "done"}
    assert exhausted


def test_a_reset_reaches_a_subscriber_that_is_already_past_it():
    """The regression this guards: a reset used to clear the backlog, which
    invalidated the cursor of anyone already reading — they waited on events
    that no longer existed and never saw the reset itself."""
    job_stream.reset_all()
    channel = job_stream.open_channel("j1")
    channel.publish_text("aban")
    channel.publish_text("doned")

    read_so_far, _ = channel.read(0, timeout=0)
    cursor = len(read_so_far)  # a subscriber that has consumed both deltas

    channel.publish_reset()
    channel.publish_text("kept")

    events, _ = channel.read(cursor, timeout=0)
    assert [e["type"] for e in events] == ["reset", "delta"]
    assert events[1]["text"] == "kept"


def test_a_late_subscriber_sees_the_reset_before_the_replacement_text():
    """It replays the abandoned attempt, then the reset that discards it —
    which the client applies in order, ending up with only the good text."""
    job_stream.reset_all()
    channel = job_stream.open_channel("j1")
    channel.publish_text("abandoned")
    channel.publish_reset()
    channel.publish_text("kept")

    events, _ = channel.read(0, timeout=0)
    assert [e["type"] for e in events] == ["delta", "reset", "delta"]


def test_the_worker_and_the_subscriber_converge_on_one_channel():
    """They race: a browser can subscribe before the dispatcher has claimed
    the job. Whoever arrives first creates it and the other joins."""
    job_stream.reset_all()
    assert job_stream.open_channel("j1") is job_stream.open_channel("j1")


# --- end to end ----------------------------------------------------------


def _seed_conversation(app):
    user = User(id="u1", email="s@example.com", password_hash="x")
    db.session.add(user)
    db.session.flush()
    scenario = Scenario(
        id="sc1",
        user_id="u1",
        topic_id="web-auth",
        type="concept",
        band=1,
        title="Auth scenario",
        prompt_md="Design the login flow.",
        rubric_json=[
            {"concept_id": "password-hashing", "expected": "hashing", "weight": 1, "essential": True},
            {"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True},
        ],
        target_concepts_json=["password-hashing", "session-auth"],
        model="fake",
        prompt_version="converse.v1",
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario


def test_a_conversation_turn_streams_exactly_the_reply_it_persists(app):
    """The single most important guarantee here: what the student watched
    appear and what ends up in the database are the same text. If these ever
    diverge, the animation is lying about the turn."""
    job_stream.reset_all()
    with app.app_context():
        scenario = _seed_conversation(app)
        attempt = get_or_create_attempt(scenario, db.session.get(User, "u1"))
        job = Job(
            id="j1",
            user_id="u1",
            kind="converse_turn",
            payload_json={"attempt_id": attempt.id, "student_message": "Use argon2 hashing."},
        )
        db.session.add(job)
        db.session.commit()

        run_job("j1")

        events, exhausted = job_stream.get_channel("j1").read(0, timeout=0)
        streamed = "".join(e["text"] for e in events if e["type"] == "delta")
        turn = db.session.query(ConversationTurn).filter_by(attempt_id=attempt.id).one()

        assert streamed == turn.assistant_reply_md
        assert exhausted
        assert events[-1]["status"] == "done"


def test_a_failed_job_still_closes_its_channel(app):
    """An unclosed channel leaves the browser watching a caret forever — the
    one failure mode streaming adds that polling didn't have."""
    job_stream.reset_all()
    with app.app_context():
        db.session.add(User(id="u1", email="s@example.com", password_hash="x"))
        db.session.commit()
        db.session.add(Job(id="j1", user_id="u1", kind="converse_turn", payload_json={"attempt_id": "nope"}))
        db.session.commit()

        run_job("j1")

        events, exhausted = job_stream.get_channel("j1").read(0, timeout=0)
        assert exhausted
        assert events[-1]["type"] == "done"
        assert events[-1]["status"] == "failed"
        assert db.session.get(Job, "j1").status == "failed"


def test_a_help_answer_streams_the_answer_and_not_the_declined_flag(app):
    """`answer_md` is the only field of HelpAnswerSpec a human reads; the
    preview shouldn't leak the rest of the document around it."""
    job_stream.reset_all()
    with app.app_context():
        scenario = _seed_conversation(app)
        db.session.add(
            Job(
                id="j1",
                user_id="u1",
                kind="help_question",
                payload_json={"scenario_id": scenario.id, "question": "what is throughput?"},
            )
        )
        db.session.commit()

        run_job("j1")

        events, _ = job_stream.get_channel("j1").read(0, timeout=0)
        streamed = "".join(e["text"] for e in events if e["type"] == "delta")
        exchange = db.session.query(HelpExchange).one()

        assert streamed == exchange.answer_md
        assert "declined" not in streamed


# --- the routes ----------------------------------------------------------


def _login(client, email="s@example.com", password="correcthorsebatterystaple"):
    page = client.get("/register").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]*)"', page).group(1)
    client.post(
        "/register",
        data={"csrf_token": token, "email": email, "password": password, "confirm": password},
        follow_redirects=True,
    )
    return token


def test_sending_a_message_answers_in_place_instead_of_navigating(app, client):
    """The behaviour §5.7 exists to change: an htmx post gets the conversation
    panel back, containing the student's own message and a stream to watch —
    not a redirect to a waiting page."""
    csrf = _login(client)
    with app.app_context():
        user = db.session.query(User).one()
        scenario = Scenario(
            id="sc1",
            user_id=user.id,
            topic_id="web-auth",
            type="concept",
            band=1,
            title="Auth scenario",
            prompt_md="Design the login flow.",
            rubric_json=[{"concept_id": "session-auth", "expected": "sessions", "weight": 1, "essential": True}],
            target_concepts_json=["session-auth"],
            model="fake",
            prompt_version="converse.v1",
        )
        db.session.add(scenario)
        db.session.commit()

    response = client.post(
        "/train/sc1/message",
        data={"csrf_token": csrf, "student_message": "Server-side sessions."},
        headers={"HX-Request": "true"},
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200  # not a 302 to train/pending
    assert "Server-side sessions." in body  # their message is already on screen
    assert "data-stream-url" in body
    assert 'id="chat-panel"' in body


def test_a_stream_belonging_to_another_student_is_not_readable(app, client):
    """Same ownership rule as every other `<id>` route: 404, not 403."""
    csrf = _login(client)
    with app.app_context():
        db.session.add(User(id="other", email="other@example.com", password_hash="x"))
        db.session.flush()
        db.session.add(Job(id="j1", user_id="other", kind="converse_turn", payload_json={}))
        db.session.commit()

    assert client.get("/jobs/j1/stream").status_code == 404


def test_a_finished_job_with_no_channel_ends_the_stream_at_once(app, client):
    """After a restart the channel is gone but the reply is not — the client
    must be told to stop waiting and re-render from the database."""
    job_stream.reset_all()
    csrf = _login(client)
    with app.app_context():
        user = db.session.query(User).one()
        db.session.add(
            Job(id="j1", user_id=user.id, kind="converse_turn", status="done", payload_json={})
        )
        db.session.commit()

    response = client.get("/jobs/j1/stream")

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert "event: done" in response.get_data(as_text=True)
