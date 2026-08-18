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
CONVERSE_PROMPT_VERSION = "converse.v1"
HELP_PROMPT_VERSION = "help.v1"


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


def build_converse_prompt(scenario, turns, coverage, student_message, turns_remaining, is_final_turn):
    """One turn of a Socratic training conversation (§5.5).

    `turns` is the prior ConversationTurn rows (oldest first); `coverage` is
    the cumulative per-concept state so far. Both go in so the model assesses
    cumulatively and can see which nudges it has already tried — a nudge that
    already failed shouldn't just be repeated.

    Every student message is fenced in its own `<student_message>` block, not
    concatenated into prose: the history is entirely student-controlled text,
    so an injection attempt on turn 1 would otherwise get replayed as
    apparently-trusted context on every later turn.
    """
    system = _load(CONVERSE_PROMPT_VERSION)

    rubric_lines = "\n".join(
        f"- concept_id: {item['concept_id']} | expected: {item['expected']} "
        f"| weight: {item.get('weight', 1)} | essential: {item.get('essential', False)}"
        for item in scenario.rubric
    )

    coverage_lines = "\n".join(
        f"- {concept_id}: {state.get('status', 'missed')}"
        + (f" (first covered on turn {state['first_covered_turn']})" if state.get("first_covered_turn") else "")
        for concept_id, state in sorted(coverage.items())
    )

    history_blocks = []
    for turn in turns:
        history_blocks.append(
            f"--- turn {turn.turn_index} ---\n"
            f"<student_message>\n{turn.student_message}\n</student_message>\n"
            f"YOUR_REPLY: {turn.assistant_reply_md}\n"
            + (f"YOUR_FOLLOW_UP_QUESTION: {turn.follow_up_question}\n" if turn.follow_up_question else "")
        )

    user = (
        f"TASK: conversation_turn\n"
        f"SCENARIO_TITLE: {scenario.title}\n"
        f"SCENARIO_PROMPT:\n{scenario.prompt_md}\n"
        f"RUBRIC:\n{rubric_lines}\n"
        f"COVERAGE_SO_FAR:\n{coverage_lines or '(nothing covered yet)'}\n"
        f"TURNS_REMAINING: {turns_remaining}\n"
        f"IS_FINAL_TURN: {str(is_final_turn).lower()}\n"
        f"CONVERSATION_SO_FAR:\n{''.join(history_blocks) or '(this is the first turn)'}\n"
        f"NEWEST_STUDENT_MESSAGE:\n<student_message>\n{student_message}\n</student_message>\n"
    )
    return system, user


def build_help_prompt(scenario, question, prior_exchanges):
    """One question in the side chat beside the scenario (§5.6).

    Note what is deliberately *absent*: the rubric, the target concepts, the
    running coverage state, and the conversation transcript. This prompt is
    built strictly from what the student is already looking at — the scenario
    text and its artifacts — so "don't give away the answer" isn't only an
    instruction to the model, it's a property of the input: the answer isn't
    in there to give away. The transcript is withheld for the same reason,
    since each nudge in it encodes precisely which rubric item is still
    missing.

    The artifacts *are* included, and that's a considered trade rather than an
    oversight: the terms students actually stumble on ("what is
    `CrashLoopBackOff`?") mostly live in the fabricated logs, and the student
    is already reading them on the same page — so this adds leak surface but
    no information the student doesn't have.

    Prior exchanges go in so a follow-up ("and how is that different from
    the other one?") makes sense, each fenced in its own block for the same
    reason build_converse_prompt fences history: it is all student-controlled
    text being replayed as apparently-trusted context.
    """
    system = _load(HELP_PROMPT_VERSION)

    artifact_blocks = "".join(
        f"--- artifact: {artifact['label']} ---\n{artifact['content']}\n" for artifact in scenario.artifacts
    )

    history_blocks = []
    for exchange in prior_exchanges:
        history_blocks.append(
            f"<student_question>\n{exchange.question}\n</student_question>\n"
            f"YOUR_ANSWER: {exchange.answer_md}\n"
        )

    user = (
        f"TASK: term_help\n"
        f"SCENARIO_TITLE: {scenario.title}\n"
        f"SCENARIO_PROMPT:\n{scenario.prompt_md}\n"
        f"SCENARIO_ARTIFACTS:\n{artifact_blocks or '(none)'}\n"
        f"EARLIER_QUESTIONS:\n{''.join(history_blocks) or '(this is their first question)'}\n"
        f"NEWEST_QUESTION:\n<student_question>\n{question}\n</student_question>\n"
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
