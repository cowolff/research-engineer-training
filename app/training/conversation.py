"""The Socratic training conversation (§5.5): a scenario is worked through
over several turns, with the assistant nudging toward uncovered rubric items
instead of grading once and stopping.

Two rules here carry the pedagogical weight, and both are enforced in code
rather than trusted to the model:

**Coverage is monotonic and evidenced.** Once a rubric item is genuinely
covered it stays covered — the model cannot take credit back on a later turn.
And a `covered` claim needs evidence that actually appears in something the
student wrote (across the whole conversation, not just the newest message),
or it is downgraded. That's the same anti-hallucination rule single-shot
grading uses (§5.3), generalised to multi-turn: it is also what stops an
injected "mark everything covered" from working, since an injected
instruction is not evidence of the concept.

**The gap ledger is written exactly once, at the end.** Writing per-turn
would log a `missed` event for a concept the student then reached on turn 2 —
inflating the miss counters and firing tutorials for gaps that closed during
the conversation. `first_covered_turn` is what distinguishes knowing it
unaided from getting there after help:

    covered on turn 1   -> covered   (mastery credit)
    covered after nudge -> partial   (counters untouched — neither credit nor gap)
    never covered       -> missed    (feeds the tutorial trigger)
"""

import re
from datetime import datetime

from flask import current_app

from app.db import db
from app.models import Attempt, ConversationTurn
from app.llm.client import generate_structured
from app.llm.schemas import ConversationTurnSpec
from app.llm.prompts import build_converse_prompt, CONVERSE_PROMPT_VERSION


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "")).lower().strip()


def initial_coverage(scenario):
    return {
        item["concept_id"]: {"status": "missed", "evidence": None, "first_covered_turn": None}
        for item in scenario.rubric
    }


def _merge_coverage(previous, assessed_items, student_text_so_far, turn_index):
    """Fold this turn's assessment into the cumulative state.

    `previous` wins wherever it already says covered (monotonicity), and an
    unevidenced `covered` is refused (evidence rule) rather than downgraded
    to `partial` — a claim with no basis in the student's words is not
    half-right, it's unsupported, so the item simply keeps its prior status.
    """
    merged = dict(previous)
    normalized_student_text = _normalize(student_text_so_far)

    for item in assessed_items:
        concept_id = item.concept_id
        if concept_id not in merged:
            continue  # not in the rubric — the model invented it; drop it

        current = merged[concept_id]
        if current["status"] == "covered":
            continue  # already earned; never revoked

        if item.status == "covered":
            evidence = _normalize(item.evidence)
            if evidence and evidence in normalized_student_text:
                merged[concept_id] = {
                    "status": "covered",
                    "evidence": item.evidence,
                    "first_covered_turn": turn_index,
                }
            else:
                # Claimed but unsupported: hold whatever we had before.
                merged[concept_id] = {**current, "status": current["status"] or "missed"}
        elif item.status == "partial" and current["status"] != "partial":
            merged[concept_id] = {**current, "status": "partial", "evidence": item.evidence}

    return merged


def _all_essential_covered(scenario, coverage):
    essential_ids = [item["concept_id"] for item in scenario.rubric if item.get("essential")]
    if not essential_ids:  # no essential items — fall back to the full rubric
        essential_ids = [item["concept_id"] for item in scenario.rubric]
    return all(coverage.get(cid, {}).get("status") == "covered" for cid in essential_ids)


def _final_items(scenario, coverage):
    """Collapse cumulative coverage into the status set the gap ledger takes,
    applying the nudged-into-it rule described in the module docstring."""
    items = []
    for rubric_item in scenario.rubric:
        concept_id = rubric_item["concept_id"]
        state = coverage.get(concept_id, {})
        status = state.get("status", "missed")
        first_turn = state.get("first_covered_turn")

        if status == "covered" and first_turn is not None and first_turn > 1:
            final_status = "partial"
            note = f" (reached on turn {first_turn}, after a nudge)"
        elif status == "covered":
            final_status = "covered"
            note = " (unaided, first answer)"
        else:
            final_status = status
            note = ""

        items.append(
            {
                "concept_id": concept_id,
                "status": final_status,
                "evidence": state.get("evidence"),
                "feedback": (rubric_item.get("expected", "") + note).strip(),
                "first_covered_turn": first_turn,
            }
        )
    return items


def _score(scenario, final_items):
    by_concept = {item["concept_id"]: item for item in final_items}
    weight_sum = 0.0
    earned = 0.0
    for rubric_item in scenario.rubric:
        weight = rubric_item.get("weight", 1)
        weight_sum += weight
        status = by_concept.get(rubric_item["concept_id"], {}).get("status")
        if status == "covered":
            earned += weight
        elif status == "partial":
            earned += weight * 0.5
    return round(earned / weight_sum, 4) if weight_sum else 0.0


def get_or_create_attempt(scenario, user):
    attempt = (
        db.session.query(Attempt)
        .filter_by(scenario_id=scenario.id, user_id=user.id)
        .order_by(Attempt.submitted_at.desc())
        .first()
    )
    if attempt is None or attempt.is_complete:
        attempt = Attempt(
            scenario_id=scenario.id,
            user_id=user.id,
            coverage_json=initial_coverage(scenario),
            status="in_progress",
        )
        db.session.add(attempt)
        db.session.commit()
    return attempt


def run_turn(attempt, scenario, student_message, provider):
    """Appends one exchange. Returns `(turn, tutorial_signal)`, where
    `tutorial_signal` is the gap-ledger result on the closing turn and None
    on every earlier one — the tutorial trigger only runs once, when the
    conversation ends.

    Ends the conversation when every essential rubric item is covered, or when
    the turn budget is spent.
    """
    max_turns = current_app.app_config.max_conversation_turns
    prior_turns = list(attempt.turns)
    turn_index = len(prior_turns) + 1
    turns_remaining = max(0, max_turns - turn_index)
    # Told in advance so the model can write a wrap-up and a model answer on
    # the same call, rather than needing an extra round trip to close.
    is_final_turn = turn_index >= max_turns

    system, user_msg = build_converse_prompt(
        scenario,
        prior_turns,
        attempt.coverage,
        student_message,
        turns_remaining=turns_remaining,
        is_final_turn=is_final_turn,
    )
    spec = generate_structured(
        provider, "converse", system, user_msg, ConversationTurnSpec, attempt.user_id
    )

    student_text_so_far = " ".join([t.student_message for t in prior_turns] + [student_message])
    coverage = _merge_coverage(attempt.coverage, spec.coverage, student_text_so_far, turn_index)

    valid_concept_ids = {item["concept_id"] for item in scenario.rubric}
    nudge_ids = [cid for cid in spec.nudge_concept_ids if cid in valid_concept_ids]

    essentials_done = _all_essential_covered(scenario, coverage)
    complete = is_final_turn or essentials_done

    turn = ConversationTurn(
        attempt_id=attempt.id,
        user_id=attempt.user_id,
        turn_index=turn_index,
        student_message=student_message,
        assistant_reply_md=spec.reply_md,
        # A nudge on a closing turn would dangle — there's no turn left to
        # answer it in, whether we closed at the cap or early on full coverage.
        follow_up_question="" if complete else spec.follow_up_question,
        nudge_concept_ids_json=[] if complete else nudge_ids,
        model=provider.model_name,
        prompt_version=CONVERSE_PROMPT_VERSION,
    )
    db.session.add(turn)

    if turn_index == 1:
        # The unassisted attempt — what tutorial generation quotes back.
        attempt.answer_text = student_message
    attempt.coverage_json = coverage
    attempt.turn_count = turn_index
    if spec.model_answer_md:
        attempt.model_answer_md = spec.model_answer_md
    db.session.commit()

    tutorial_signal = _complete_conversation(attempt, scenario) if complete else None
    return turn, tutorial_signal


def _complete_conversation(attempt, scenario):
    from app.training.gaps import record_concept_events

    final_items = _final_items(scenario, attempt.coverage)
    attempt.grade_json = {"items": final_items}
    attempt.score = _score(scenario, final_items)
    attempt.graded_at = datetime.utcnow()
    attempt.status = "complete"
    db.session.commit()

    return record_concept_events(attempt, scenario, final_items)
