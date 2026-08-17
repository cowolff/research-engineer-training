"""Deterministic stand-in for a real model. Not a toy: it parses the same
structured `TASK:` sections app/llm/prompts.py writes, so it exercises the
real registry-validation and evidence-checking code paths in
app/training/grading.py and app/tutorials/generation.py without ever making a
network call. This is what LLM_PROVIDER=fake runs, and it is what every test
and CI run uses — see docs/IMPLEMENTATION_PLAN.md §5.1 and §14.
"""

import hashlib
import json
import re

from app.llm.base import LLMUsage

_CONCEPT_LINE = re.compile(r"^- id: (\S+) \| name: (.+?) \| essential: (\S+) \| probe: (.*)$", re.M)
_RUBRIC_LINE = re.compile(r"^- concept_id: (\S+) \| expected: (.+?) \| weight: (\d+) \| essential: (\S+)$", re.M)
_RESOURCE_LINE = re.compile(r"^\d+\. id: (\S+) \| title: (.+?) \| kind: (\S+) \| minutes: (\d+) \| summary: (.*)$", re.M)
_STUDENT_ANSWER = re.compile(r"<student_answer>\n(.*?)\n</student_answer>", re.S)


def _seed(text):
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


class FakeProvider:
    model_name = "fake-provider"

    def raw_complete(self, system, user, schema=None):
        # `schema` is ignored — this always produces a matching shape by
        # construction (see the three builders below), so there's nothing a
        # schema-constrained real provider would add here.
        usage = LLMUsage(prompt_tokens=len(user) // 4, completion_tokens=40, latency_ms=5)

        if "TASK: scenario_generation" in user:
            return self._scenario(user), usage
        if "TASK: grading" in user:
            return self._grade(user), usage
        if "TASK: tutorial_generation" in user:
            return self._tutorial(user), usage

        raise ValueError("FakeProvider: could not determine TASK from prompt")

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
