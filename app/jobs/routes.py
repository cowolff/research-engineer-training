import json
import time

from flask import Blueprint, render_template, abort, request, url_for, make_response, Response
from flask_login import login_required, current_user

from app.db import db
from app.jobs import stream
from app.models import Job, Attempt

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

# Long enough for a slow self-hosted reasoning model plus its retries (docs
# §5.1 records a 67s real generation, and there's no honest ceiling), short
# enough that an abandoned tab eventually gives a thread back. Reaching it is
# not data loss: the client re-renders from the database either way.
_MAX_STREAM_SECONDS = 600
# A comment frame on an idle connection, so an intermediary that reaps quiet
# sockets doesn't decide this one is dead while the model is still thinking.
_KEEPALIVE_SECONDS = 15


@bp.get("/<job_id>")
@login_required
def status(job_id):
    job = _owned_job(job_id)

    if job.status == "done" and job.kind == "generate_scenario" and request.headers.get("HX-Request"):
        # The scenario just finished streaming in above this partial, and the
        # only thing left between the student and it was a button. Go (§5.7).
        # Only for htmx: the plain-form path has no way to act on this header,
        # and still gets the link below.
        response = make_response("")
        response.headers["HX-Redirect"] = url_for("train.view_scenario", scenario_id=job.result["scenario_id"])
        return response

    retry_scenario_id = None
    if job.kind == "converse_turn":
        attempt = db.session.get(Attempt, job.payload.get("attempt_id"))
        if attempt:
            retry_scenario_id = attempt.scenario_id

    return render_template("partials/job_status.html", job=job, retry_scenario_id=retry_scenario_id)


@bp.get("/<job_id>/stream")
@login_required
def token_stream(job_id):
    """Server-sent events carrying one job's reply as it is written (§5.7).

    SSE rather than a WebSocket because the traffic is one-directional and
    this has to survive the same constraints everything else here does: it is
    plain HTTP through the upstream TLS terminator, it needs no protocol
    upgrade, and the browser reconnects on its own — which the channel's
    retained backlog turns into a resume rather than a restart.

    The generator below deliberately touches no database and no request
    context: ownership is settled here, before the response begins, and after
    that everything it needs comes from the in-memory channel. A request that
    can be open for minutes has no business holding a session that long.
    """
    job = _owned_job(job_id)
    cursor = _resume_cursor()

    if job.status in ("done", "failed") and stream.get_channel(job_id) is None:
        # Finished before anyone subscribed, or the process restarted and took
        # the channel with it. The content is in the database, so end the
        # stream at once and let the client swap in the rendered version
        # rather than sit watching nothing.
        return _sse(iter([_frame(cursor, {"type": "done", "status": job.status})]))

    # Idempotent, and it has to be: a browser can open this before the
    # dispatcher has claimed the job, in which case this creates the channel
    # and the worker joins it a moment later.
    channel = stream.open_channel(job_id)
    return _sse(_events(channel, cursor))


def _owned_job(job_id):
    job = db.session.get(Job, job_id)
    if job is None or job.user_id != current_user.id:
        abort(404)
    return job


def _resume_cursor():
    """`Last-Event-ID` is what the browser sends back automatically when
    EventSource reconnects after a dropped connection. Honouring it is what
    makes a flaky network resume mid-reply instead of replaying it."""
    try:
        return int(request.headers.get("Last-Event-ID", "")) + 1
    except ValueError:
        return 0


def _frame(index, event):
    # json.dumps is not decoration: SSE frames are newline-delimited, and a
    # reply full of markdown line breaks would otherwise split into fragments
    # the browser reassembles wrongly.
    return f"id: {index}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _events(channel, cursor):
    deadline = time.monotonic() + _MAX_STREAM_SECONDS
    while True:
        events, exhausted = channel.read(cursor, _KEEPALIVE_SECONDS)
        for event in events:
            yield _frame(cursor, event)
            cursor += 1
        if exhausted:
            return
        if time.monotonic() > deadline:
            yield _frame(cursor, {"type": "done", "status": "timeout"})
            return
        if not events:
            yield ": keepalive\n\n"


def _sse(generator):
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Nginx-family proxies buffer response bodies by default, which
            # would hold every token back until the job finished — precisely
            # the behaviour this replaces.
            "X-Accel-Buffering": "no",
        },
    )
