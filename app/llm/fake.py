"""Deterministic stand-in for a real model. Not a toy: it parses the same
structured `TASK:` sections app/llm/prompts.py writes, so it exercises the
real registry-validation and evidence-checking code paths in
app/training/conversation.py and app/tutorials/generation.py without ever
making a network call. This is what LLM_PROVIDER=fake runs, and it is what every test
and CI run uses — see docs/IMPLEMENTATION_PLAN.md §5.1 and §14.
"""

import hashlib
import json
import re
import time

from app.llm.base import LLMUsage

# Small enough that a reply arrives in visibly many pieces rather than one, so
# the incremental JSON extraction in app/llm/streaming.py is genuinely
# exercised — including chunk boundaries landing mid-escape — instead of being
# handed one complete document and never tested.
_STREAM_CHUNK_CHARS = 7

_CONCEPT_LINE = re.compile(r"^- id: (\S+) \| name: (.+?) \| essential: (\S+) \| probe: (.*)$", re.M)
_RUBRIC_LINE = re.compile(r"^- concept_id: (\S+) \| expected: (.+?) \| weight: (\d+) \| essential: (\S+)$", re.M)
_RESOURCE_LINE = re.compile(r"^\d+\. id: (\S+) \| title: (.+?) \| kind: (\S+) \| minutes: (\d+) \| summary: (.*)$", re.M)
_STUDENT_ANSWER = re.compile(r"<student_answer>\n(.*?)\n</student_answer>", re.S)
_NEWEST_STUDENT_MESSAGE = re.compile(r"NEWEST_STUDENT_MESSAGE:\n<student_message>\n(.*?)\n</student_message>", re.S)
_ALL_STUDENT_MESSAGES = re.compile(r"<student_message>\n(.*?)\n</student_message>", re.S)
_IS_FINAL_TURN = re.compile(r"^IS_FINAL_TURN: (\S+)$", re.M)
_NEWEST_QUESTION = re.compile(r"NEWEST_QUESTION:\n<student_question>\n(.*?)\n</student_question>", re.S)

# Mirrors help.v1.md's own "this belongs in the main conversation" rule, so
# a test that the side chat refuses to do the exercise stays meaningful
# without a live model. Deliberately phrase-level, not word-level: a bare
# "fix" would also fire on "what is a fixture?", which is a fair question.
_ANSWER_SEEKING_MARKERS = (
    "wrong with", "what should i", "what would you", "is my answer",
    "am i right", "the answer", "the problem here", "how do i fix",
    "solve this", "diagnose", "explain the scenario",
)


def _seed(text):
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


class FakeProvider:
    model_name = "fake-provider"

    def __init__(self, chunk_delay_seconds=0.0):
        # Zero in tests (nothing to watch, and a suite shouldn't sleep); a
        # small non-zero value for `LLM_PROVIDER=fake` local dev, where the
        # whole point is to see the reply land token by token without paying
        # for a real model. See FAKE_STREAM_DELAY_SECONDS in app/config.py.
        self._chunk_delay_seconds = chunk_delay_seconds

    def raw_complete(self, system, user, schema=None, on_delta=None):
        # `schema` is ignored — this always produces a matching shape by
        # construction (see the builders below), so there's nothing a
        # schema-constrained real provider would add here.
        usage = LLMUsage(prompt_tokens=len(user) // 4, completion_tokens=40, latency_ms=5)
        return self._emit(self._body(user), on_delta), usage

    def _body(self, user):
        if "TASK: scenario_generation" in user:
            return self._scenario(user)
        if "TASK: grading" in user:
            return self._grade(user)
        if "TASK: tutorial_generation" in user:
            return self._tutorial(user)
        if "TASK: conversation_turn" in user:
            return self._conversation_turn(user)
        if "TASK: term_help" in user:
            return self._term_help(user)

        raise ValueError("FakeProvider: could not determine TASK from prompt")

    def _emit(self, text, on_delta):
        """Hand the finished document over in pieces, the way a real backend
        does. The split is on raw character count, deliberately blind to JSON
        structure — that's what makes it land mid-token and mid-escape, which
        is exactly the case app/llm/streaming.py has to survive."""
        if on_delta is None:
            return text
        for start in range(0, len(text), _STREAM_CHUNK_CHARS):
            on_delta(text[start : start + _STREAM_CHUNK_CHARS])
            if self._chunk_delay_seconds:
                time.sleep(self._chunk_delay_seconds)
        return text

    def _conversation_turn(self, user):
        rubric = _RUBRIC_LINE.findall(user)
        is_final = (_IS_FINAL_TURN.search(user).group(1) if _IS_FINAL_TURN.search(user) else "false") == "true"

        # Assess cumulatively across every student message in the prompt, the
        # same way the real prompt asks the model to — so the monotonic-merge
        # and evidence rules in app/training/conversation.py get exercised
        # against realistic input rather than a fixed canned reply.
        all_messages = " ".join(_ALL_STUDENT_MESSAGES.findall(user)).lower()

        coverage = []
        uncovered = []
        for cid, expected, weight, essential in rubric:
            tokens = [t for t in cid.split("-") if len(t) > 2]
            hit = next((t for t in tokens if t in all_messages), None)
            if hit:
                coverage.append({"concept_id": cid, "status": "covered", "evidence": hit})
            else:
                coverage.append({"concept_id": cid, "status": "missed", "evidence": None})
                uncovered.append(cid)

        nudge_targets = uncovered[:1]
        spec = {
            "coverage": coverage,
            "reply_md": (
                "(Deterministic fake reply.) "
                + ("Wrapping up here." if is_final else f"You covered {len(rubric) - len(uncovered)} of {len(rubric)} so far.")
            ),
            # Deliberately never names the target concept — mirrors the real
            # prompt's central nudging rule, so a test asserting "the nudge
            # doesn't give away the answer" is meaningful on the fake too.
            "follow_up_question": "" if is_final else "What would have to be true for that to happen?",
            "nudge_concept_ids": [] if is_final else nudge_targets,
            "model_answer_md": ("A strong answer covers: " + ", ".join(cid for cid, *_ in rubric)) if is_final else "",
        }
        return json.dumps(spec)

    def _term_help(self, user):
        """The side chat (§5.6). Notice there is no rubric to read here — the
        real prompt isn't given one either, which is the structural half of
        "it can't leak the answer" and is what tests/test_help.py asserts."""
        question_match = _NEWEST_QUESTION.search(user)
        question = (question_match.group(1) if question_match else "").strip()
        lowered = question.lower()

        if any(marker in lowered for marker in _ANSWER_SEEKING_MARKERS):
            return json.dumps(
                {
                    "answer_md": (
                        "(Deterministic fake reply.) That one belongs in the main conversation — "
                        "working it out is the point of the exercise."
                    ),
                    "declined": True,
                }
            )

        term = re.sub(r"^(what|whats|what's|how|why)\s+(is|are|does|do)?\s*", "", lowered).strip(" ?.")
        return json.dumps(
            {
                "answer_md": (
                    f"(Deterministic fake definition.) \u201c{term or question}\u201d is a term of art. "
                    "Here is what it means in general, with no reference to the scenario in front of you."
                ),
                "declined": False,
            }
        )

    def _scenario(self, user):
        scenario_type_match = re.search(r"^SCENARIO_TYPE: (\S+)$", user, re.M)
        scenario_type = scenario_type_match.group(1) if scenario_type_match else "concept"

        topic_match = re.search(r"^TOPIC: (.+?) \((\S+)\)$", user, re.M)
        topic_title = topic_match.group(1) if topic_match else "the topic"

        concepts = _CONCEPT_LINE.findall(user)
        seed = _seed(user)

        rubric = []
        for i, (cid, name, essential, probe) in enumerate(concepts):
            rubric.append(
                {
                    "concept_id": cid,
                    "expected": f"Mentions {name.lower()} ({probe})" if probe else f"Mentions {name.lower()}",
                    "weight": 3 if essential == "True" else 1,
                    "essential": essential == "True",
                }
            )

        artifacts = []
        if scenario_type == "debug_artifact" and concepts:
            first_name = concepts[0][1]
            artifacts.append(
                {
                    "label": "app.log",
                    "language": "text",
                    "content": f"[fake] seed={seed % 10000} generated log related to {first_name}",
                }
            )

        spec = {
            "type": scenario_type,
            "title": f"[fake #{seed % 1000}] {topic_title}",
            "prompt_md": (
                f"(Deterministic fake scenario, seed {seed % 1000}.) Consider {topic_title}. "
                + " ".join(f"Address {name}." for _, name, _, _ in concepts)
            ),
            "artifacts": artifacts,
            "rubric": rubric or [{"concept_id": "unknown", "expected": "n/a", "weight": 1, "essential": False}],
        }
        return json.dumps(spec)

    def _grade(self, user):
        rubric = _RUBRIC_LINE.findall(user)
        answer_match = _STUDENT_ANSWER.search(user)
        answer = (answer_match.group(1) if answer_match else "").lower()

        items = []
        covered_count = 0.0
        for cid, expected, weight, essential in rubric:
            # A concept counts as covered if a token derived from its id
            # (split on '-') shows up in the student's answer. This is what
            # makes the fake provider double as a prompt-injection test: text
            # that tries to instruct the grader ("mark everything covered")
            # doesn't mention any real concept token, so it is graded missed.
            tokens = [t for t in cid.split("-") if len(t) > 2]
            hit = next((t for t in tokens if t in answer), None)
            if hit:
                items.append({"concept_id": cid, "status": "covered", "evidence": hit, "feedback": f"Good — you mentioned {hit}."})
                covered_count += 1
            else:
                items.append({"concept_id": cid, "status": "missed", "evidence": None, "feedback": f"Didn't address {cid}."})

        score = covered_count / len(rubric) if rubric else 0.0
        report = {
            "items": items,
            "score": round(score, 2),
            "strengths_md": "You engaged with the scenario." if answer.strip() else "No answer submitted.",
            "model_answer_md": "A strong answer would cover: " + ", ".join(cid for cid, *_ in rubric),
        }
        return json.dumps(report)

    def _tutorial(self, user):
        concept_match = re.search(r"^CONCEPT: (\S+) \| (.+?) \| (.*)$", user, re.M)
        concept_id, concept_name, probe = concept_match.groups() if concept_match else ("unknown", "Unknown", "")

        related_match = re.search(r"^RELATED_CONCEPTS: (.*)$", user, re.M)
        related = [r.strip() for r in related_match.group(1).split(",")] if related_match else []
        related = [r for r in related if r and r != "(none)"]

        resources = _RESOURCE_LINE.findall(user)
        resource_ids = [r[0] for r in resources][:2]

        answer_match = re.search(r"STUDENT_ANSWER: (.+)", user)
        quoted_answer = (answer_match.group(1)[:120] if answer_match else "").strip()

        spec = {
            "title": f"Closing the gap: {concept_name}",
            "body_md": (
                f"(Deterministic fake tutorial.) You were asked about {concept_name} and answered "
                f'"{quoted_answer}" — here is what to know. {probe}'
                + ("".join(f" See also [[res:{rid}]]." for rid in resource_ids))
            ),
            "exercise_md": f"Try a small hands-on exercise involving {concept_name}.",
            "related_concept_ids": related,
            "cited_resource_ids": resource_ids,
            "reading_order": resource_ids,
        }
        return json.dumps(spec)
