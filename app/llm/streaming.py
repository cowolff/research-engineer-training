"""Turning a structured JSON generation into a stream of visible text.

Every LLM call in this app returns a JSON object validated against a Pydantic
schema (§5.1), and that isn't negotiable: rubric ids, coverage statuses and
evidence quotes are only trustworthy because they are checked, not because the
model was asked nicely. But a JSON document arriving token by token is not
something you can put on a screen — the student would watch
`{"coverage": [{"concept_id": "pass` scroll past.

So the raw stream is filtered through `JsonFieldStreamer`, which pulls exactly
one top-level string field (`reply_md`, `answer_md`, ...) out of the document
*while it is still being written* and releases its decoded text as it grows.
Everything else is dropped from the preview. The preview is never the source
of truth: what gets persisted, sanitised and rendered at the end is the
validated object, and the finished turn is re-rendered from the database once
the stream closes.

Two properties this has to have, both because of what models actually emit:

- **A partial escape must never be released.** A chunk boundary lands mid-`\\n`
  or mid-`\\u00e9` often enough to matter; releasing the half puts a literal
  backslash on screen followed later by a stray `n`. Only the prefix that is
  known to decode cleanly goes out, the remainder waits for the next chunk.
  The same applies to the lone leading surrogate of a `\\uD83D\\uDE00` pair,
  which cannot be encoded as UTF-8 on its own and would break the frame
  carrying it.
- **The field has to be found as a *key*, not as a substring.** `reply_md` can
  legitimately occur inside an earlier string value — the assessment's
  `evidence` field quotes the student verbatim, and the student can type
  anything, including a field name. So the scan tracks JSON string state and
  only matches a completed string token immediately followed by `:`.
"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger("app.llm")

_WHITESPACE = " \t\r\n"
_HIGH_SURROGATE = range(0xD800, 0xDC00)


def _decodable_prefix_len(raw):
    """Length of the longest prefix of a raw JSON string body that does not
    end inside an unfinished escape sequence."""
    index = 0
    safe = 0
    length = len(raw)
    while index < length:
        if raw[index] == "\\":
            if index + 1 >= length:
                break  # a trailing backslash — the escape hasn't arrived yet
            if raw[index + 1] == "u":
                if index + 6 > length:
                    break  # \uXXXX still in flight
                index += 6
            else:
                index += 2
        else:
            index += 1
        safe = index
    return safe


class JsonFieldStreamer:
    """Incrementally extracts one top-level string field from a JSON document
    that is still being generated. Feed it raw text; it returns the newly
    decoded characters of that field, or "" when this chunk added nothing
    visible."""

    def __init__(self, field_name):
        self._field_name = field_name
        self._state = "scan"  # scan -> value -> done

        # Scan phase: enough JSON structure to tell a key from a value.
        self._in_string = False
        self._escaped = False
        self._token = []
        self._last_token = None
        self._awaiting_value = False

        # Value phase.
        self._raw = []
        self._value_escaped = False
        self._released = 0

    @property
    def finished(self):
        return self._state == "done"

    def feed(self, chunk):
        if not chunk or self._state == "done":
            return ""
        start = 0
        if self._state == "scan":
            start = self._scan(chunk)
            if self._state != "value":
                return ""
        return self._consume_value(chunk, start)

    def _scan(self, chunk):
        """Consume up to and including the opening quote of our field's value.
        Returns the index just past it, or len(chunk) if it isn't here yet."""
        for index, char in enumerate(chunk):
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
                    self._last_token = "".join(self._token)
                    self._token = []
                    continue
                self._token.append(char)
                continue

            if char == '"':
                if self._awaiting_value:
                    self._state = "value"
                    return index + 1
                self._in_string = True
                self._token = []
            elif char == ":":
                # Only a string that closed immediately before this colon is a
                # key — which is what keeps a quoted "reply_md" inside some
                # earlier *value* from being mistaken for the field itself.
                self._awaiting_value = self._last_token == self._field_name
                self._last_token = None
            elif char not in _WHITESPACE:
                # Any other structural character: `null`, a `[`, a `,` — the
                # value we were waiting for isn't a string after all.
                self._last_token = None
                self._awaiting_value = False
        return len(chunk)

    def _consume_value(self, chunk, start):
        for index in range(start, len(chunk)):
            char = chunk[index]
            if self._value_escaped:
                self._value_escaped = False
                self._raw.append(char)
            elif char == "\\":
                self._value_escaped = True
                self._raw.append(char)
            elif char == '"':
                self._state = "done"
                break
            else:
                self._raw.append(char)
        return self._release()

    def _release(self):
        raw = "".join(self._raw)
        safe = len(raw) if self._state == "done" else _decodable_prefix_len(raw)
        if safe <= 0:
            return ""
        try:
            # strict=False: a model that emits a literal newline inside a
            # string is producing invalid JSON, but that's the schema
            # validator's problem to report — the preview shouldn't go blank
            # over it.
            decoded = json.loads(f'"{raw[:safe]}"', strict=False)
        except json.JSONDecodeError:
            return ""

        if self._state != "done" and decoded and ord(decoded[-1]) in _HIGH_SURROGATE:
            decoded = decoded[:-1]  # its pair is in the next chunk

        delta = decoded[self._released :]
        self._released = len(decoded)
        return delta


class TextStreamSink:
    """Bridges one LLM call to whatever is displaying it (app/jobs/stream.py).

    Deliberately swallows its own failures: a preview that breaks must never
    take down the generation it is previewing. Losing the animation is a
    cosmetic problem; losing the turn is not.
    """

    def __init__(self, field_name, publish_text, publish_reset=None):
        self._field_name = field_name
        self._publish_text = publish_text
        self._publish_reset = publish_reset
        self._streamer = JsonFieldStreamer(field_name)

    def feed(self, chunk):
        try:
            text = self._streamer.feed(chunk)
            if text:
                self._publish_text(text)
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.exception("stream_preview_failed", extra={"extra_fields": {"field": self._field_name}})

    def restart(self):
        """A schema-validation retry (§5.1) regenerates the whole document, so
        whatever the student has already watched appear belongs to the
        abandoned attempt. Clear it rather than letting the second attempt
        append to the first."""
        self._streamer = JsonFieldStreamer(self._field_name)
        if self._publish_reset is not None:
            try:
                self._publish_reset()
            except Exception:  # noqa: BLE001
                logger.exception("stream_reset_failed")


# Ambient rather than a parameter, on purpose. The call that actually streams
# is `generate_structured`, several frames below `run_turn` /
# `answer_question` / `generate_scenario_for_user`; threading a display
# callback through every one of those signatures would push a presentation
# concern into services that have no business knowing whether anyone is
# watching. A ContextVar scopes it exactly right instead: each job runs on its
# own worker thread, and a thread starts with an empty context, so one job's
# sink can never leak into another's.
_current_sink = ContextVar("llm_stream_sink", default=None)


@contextmanager
def streaming_to(sink):
    token = _current_sink.set(sink)
    try:
        yield sink
    finally:
        _current_sink.reset(token)


def current_sink():
    return _current_sink.get()
