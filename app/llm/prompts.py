"""Builds the (system, user) text pairs sent to a provider. The static
instructional half of each prompt lives in app/llm/prompts/*.md (the
versioned templates); this module interpolates the actual data.

The user-message layout below (the `TASK:` marker, the labelled sections) is
a contract with app/llm/fake.py, which parses it back out to build
deterministic canned responses without ever calling a real API — see
docs/IMPLEMENTATION_PLAN.md §5.1 on why FakeProvider is not optional.
"""

import os

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")

SCENARIO_PROMPT_VERSION = "scenario.v1"
GRADE_PROMPT_VERSION = "grade.v1"
TUTORIAL_PROMPT_VERSION = "tutorial.v1"


def _load(name):
    with open(os.path.join(_PROMPT_DIR, f"{name}.md")) as f:
        return f.read()


def build_scenario_prompt(topic, band, scenario_type, target_concepts, weak_concept_ids, recent_dedupe_hashes):
    system = _load(SCENARIO_PROMPT_VERSION)

    concept_lines = "\n".join(
        f"- id: {c.id} | name: {c.name} | essential: {c.essential} | probe: {c.probe}" for c in target_concepts
    )
    user = (
        f"TASK: scenario_generation\n"
        f"TOPIC: {topic.title} ({topic.id})\n"
        f"BAND: {band}\n"
        f"SCENARIO_TYPE: {scenario_type}\n"
        f"TARGET_CONCEPTS:\n{concept_lines}\n"
        f"STUDENT_WEAK_CONCEPTS: {', '.join(weak_concept_ids) or '(none yet)'}\n"
        f"RECENT_DEDUPE_HASHES: {', '.join(recent_dedupe_hashes) or '(none)'}\n"
    )
    return system, user


def build_grade_prompt(scenario, answer_text):
    system = _load(GRADE_PROMPT_VERSION)

    rubric_lines = "\n".join(
        f"- concept_id: {item['concept_id']} | expected: {item['expected']} "
        f"| weight: {item.get('weight', 1)} | essential: {item.get('essential', False)}"
        for item in scenario.rubric
    )
    user = (
        f"TASK: grading\n"
        f"SCENARIO_TITLE: {scenario.title}\n"
        f"SCENARIO_PROMPT:\n{scenario.prompt_md}\n"
        f"RUBRIC:\n{rubric_lines}\n"
        f"<student_answer>\n{answer_text}\n</student_answer>\n"
    )
    return system, user


def build_tutorial_prompt(concept, source_scenarios, resource_shortlist):
    """source_scenarios: list of (Scenario, Attempt) pairs. resource_shortlist:
    list of dict rows from the resource index reader (§7.3)."""
    system = _load(TUTORIAL_PROMPT_VERSION)

    scenario_blocks = []
    for i, (scenario, attempt) in enumerate(source_scenarios, start=1):
        scenario_blocks.append(
            f"--- scenario {i} ---\n"
            f"TITLE: {scenario.title}\n"
            f"PROMPT: {scenario.prompt_md}\n"
            f"STUDENT_ANSWER: {attempt.answer_text}\n"
        )

    resource_lines = "\n".join(
        f"{i}. id: {r['id']} | title: {r['title']} | kind: {r['kind']} | "
        f"minutes: {r['minutes']} | summary: {r['summary']}"
        for i, r in enumerate(resource_shortlist, start=1)
    )

    user = (
        f"TASK: tutorial_generation\n"
        f"CONCEPT: {concept.id} | {concept.name} | {concept.probe}\n"
        f"RELATED_CONCEPTS: {', '.join(concept.related) or '(none)'}\n"
        f"SOURCE_SCENARIOS:\n{''.join(scenario_blocks)}\n"
        f"RESOURCE_SHORTLIST:\n{resource_lines or '(none available)'}\n"
    )
    return system, user
