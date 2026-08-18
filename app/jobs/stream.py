"""In-process fan-out from a job's LLM call to the browser watching it.

The job worker and the request serving the SSE connection are two threads in
one process. That is a given here rather than an assumption to design around:
this container runs a single gunicorn worker and the job queue is an
in-process ThreadPoolExecutor (docs §3), for the same reason there is no Redis
— atlasflow gives no volumes and no managed queue service. So the "broker" is
a dict of channels behind a Condition, and nothing more.

What it does have to be careful about:

- **The browser subscribes after the job has already started.** The POST
  returns HTML, and only then does the browser open the EventSource; anything
  generated in between would be lost. So a channel keeps its events and a
  subscriber replays from wherever it is up to. That same buffer is what makes
  EventSource's own reconnect (`Last-Event-ID`) resume mid-reply instead of
  restarting from an empty bubble.
- **A finished job stays readable for a while.** Channels are retained after
  they close, so a subscriber arriving late still gets the whole reply and the
  terminal event rather than an empty stream.
- **Nothing here is the source of truth.** The reply is in the database either
  way. If the process restarted, or the channel aged out, the client falls
  back to re-rendering the panel from there — losing a channel costs the
  animation, never the content. That is also why unbounded growth is not
  worth defending against with anything cleverer than a cap: dropping the
  tail of a runaway preview is free.
"""

import logging
import threading
import time

logger = logging.getLogger("app.jobs")

# A generous ceiling on one reply's buffered preview. Reached only by a model
# that has run away; the validated result is unaffected either way.
_MAX_BUFFERED_CHARS = 200_000
# How long a closed channel stays replayable. Comfortably longer than the gap
# between a job finishing and a student's browser reconnecting after a sleep.
_RETENTION_SECONDS = 600
_MAX_CHANNELS = 128

_channels = {}
_lock = threading.Lock()


class Channel:
    """One job's event log, plus the Condition that lets readers block on it
    instead of polling."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.created_at = time.monotonic()
        self._events = []
        self._condition = threading.Condition()
        self._closed_at = None
        self._buffered_chars = 0

    @property
    def closed(self):
        return self._closed_at is not None

    def _append(self, event):
        with self._condition:
            if self._closed_at is not None:
                return
            self._events.append(event)
            self._condition.notify_all()

    def publish_text(self, text):
        if self._buffered_chars >= _MAX_BUFFERED_CHARS:
            return
        self._buffered_chars += len(text)
        self._append({"type": "delta", "text": text})

    def publish_reset(self):
        """The generation restarted (a schema-validation retry), so everything
        published so far belongs to an abandoned attempt.

        Appended rather than replacing the backlog, and that is not a detail:
        a subscriber's cursor is an index into this list, so truncating it
        would strand anyone already past the new length — they would wait on
        events that no longer exist and never see the reset that was the whole
        point. A late subscriber replays the dead text and then immediately
        clears it, which costs a few wasted bytes on a rare path and keeps one
        invariant worth keeping: this list only ever grows.
        """
        self._buffered_chars = 0
        self._append({"type": "reset"})

    def close(self, **payload):
        with self._condition:
            if self._closed_at is not None:
                return
            self._events.append({"type": "done", **payload})
            self._closed_at = time.monotonic()
            self._condition.notify_all()

    def read(self, cursor, timeout):
        """Return `(events_from_cursor, exhausted)`, blocking up to `timeout`
        seconds for the first one. `exhausted` means the channel is closed and
        the caller has now seen everything — there will never be more."""
        with self._condition:
            if cursor >= len(self._events) and self._closed_at is None:
                self._condition.wait(timeout)
            events = self._events[cursor:]
            exhausted = self._closed_at is not None and cursor + len(events) >= len(self._events)
            return events, exhausted


def open_channel(job_id):
    """Idempotent: whichever of the worker and the subscriber gets here first
    creates the channel, the other one joins it. That ordering is genuinely
    racy — the browser can open its EventSource before the dispatcher has
    claimed the job — and this is what makes it not matter."""
    with _lock:
        _prune_locked()
        channel = _channels.get(job_id)
        if channel is None:
            channel = Channel(job_id)
            _channels[job_id] = channel
        return channel


def get_channel(job_id):
    with _lock:
        return _channels.get(job_id)


def close_channel(job_id, **payload):
    channel = get_channel(job_id)
    if channel is not None:
        channel.close(**payload)


def _prune_locked():
    now = time.monotonic()
    expired = [
        job_id
        for job_id, channel in _channels.items()
        if channel.closed and now - channel._closed_at > _RETENTION_SECONDS
    ]
    for job_id in expired:
        del _channels[job_id]

    # Backstop for a pathological burst: drop the oldest *closed* channels
    # only. An open one still has a worker writing to it and a browser reading
    # it, and must survive regardless of how many there are.
    if len(_channels) > _MAX_CHANNELS:
        closed = sorted(
            (c for c in _channels.values() if c.closed), key=lambda c: c._closed_at
        )
        for channel in closed[: len(_channels) - _MAX_CHANNELS]:
            _channels.pop(channel.job_id, None)


def reset_all():
    """Test hook — the broker is process-global, so it has to be clearable
    between tests."""
    with _lock:
        _channels.clear()
