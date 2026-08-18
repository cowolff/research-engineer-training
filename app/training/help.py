"""The side chat (§5.6): a small ask-what-a-term-means window beside the
scenario, so an unfamiliar word is never the thing that stops a student
attempting the exercise.

**It cannot leak the answer, structurally.** `build_help_prompt` is handed
only what the student is already looking at — the scenario text and its
artifacts. The rubric, the target concepts, the running coverage state and the
nudges the main conversation has offered are all withheld, so there is no
answer in the prompt to spill. The prompt's own "explain the term, don't do
the exercise" rules are the second layer, not the only one; a model that
ignored them entirely still has nothing to give away beyond what is on screen.

**It cannot move the grade.** A help exchange never enters `coverage_json`,
never becomes a `ConversationTurn`, and is never fed back into
`build_converse_prompt`. Asking what a word means is not an attempt at the
scenario and must not read as one — in either direction: it earns no credit,
and it costs none.

Two budgets apply, because every question is a real LLM call: the shared daily
`MAX_LLM_CALLS_PER_DAY` quota (enforced at the route, before the job is
enqueued, like every other generation) and a per-scenario cap, so a student
can't spend a whole day's allowance on the glossary and have nothing left to
train with.
"""

from flask import current_app

from app.db import db
from app.models import HelpExchange
from app.llm.client import generate_structured
from app.llm.prompts import build_help_prompt, HELP_PROMPT_VERSION
from app.llm.schemas import HelpAnswerSpec


class HelpLimitReached(Exception):
    pass


def exchanges_for(scenario, user_id):
    """Oldest first — this is a transcript, and it is also what gets replayed
    into the next question's prompt for follow-up continuity."""
    return list(
        db.session.query(HelpExchange)
        .filter_by(scenario_id=scenario.id, user_id=user_id)
        .order_by(HelpExchange.created_at.asc())
        .all()
    )


def questions_remaining(scenario, user_id):
    used = db.session.query(HelpExchange).filter_by(scenario_id=scenario.id, user_id=user_id).count()
    return max(0, current_app.app_config.max_help_questions_per_scenario - used)


def answer_question(scenario, user_id, question, provider):
    """Runs one side-chat question. Called from the job worker, never inline
    on a request: it is the same LLM latency as a conversation turn, and the
    student is expected to keep typing their real answer while it runs."""
    if questions_remaining(scenario, user_id) <= 0:
        # Also checked at the route, so this only fires on a genuine race
        # (two questions submitted from two tabs) — but the job worker must
        # not be the place where an over-budget call slips through.
        raise HelpLimitReached("No questions left for this scenario.")

    system, user_msg = build_help_prompt(scenario, question, exchanges_for(scenario, user_id))
    spec = generate_structured(provider, "term_help", system, user_msg, HelpAnswerSpec, user_id)

    exchange = HelpExchange(
        scenario_id=scenario.id,
        user_id=user_id,
        question=question,
        answer_md=spec.answer_md,
        declined=spec.declined,
        model=provider.model_name,
        prompt_version=HELP_PROMPT_VERSION,
    )
    db.session.add(exchange)
    db.session.commit()
    return exchange
