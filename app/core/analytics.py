"""Cross-cohort numbers for the `/admin` dashboard — students, tutorials,
scenarios, and cohort-wide LLM usage. Aggregate only, same spirit as the
instructor `/cohort` view (docs §6.4): no per-student answer text, just
counts."""

from sqlalchemy import func

from app.db import db
from app.models import User, Scenario, Tutorial, TutorialRead, Attempt, LLMCall


def _bucketed_distribution(counts, n_buckets=10):
    """Split per-student counts into equal-*population* buckets (deciles by
    default) and report how much of the total each bucket accounts for.

    Bucketing by population rather than by value range is deliberate: this
    kind of usage is typically dominated by a handful of active students and
    a long tail at zero, so value-range buckets would put nearly everyone in
    one bucket. Population buckets are what answers "which percentile is
    responsible for how much" directly.
    """
    counts = sorted(counts)
    total_students = len(counts)
    total = sum(counts)
    if total_students == 0:
        return []

    n_buckets = min(n_buckets, total_students)
    buckets = []
    cumulative = 0
    for i in range(n_buckets):
        start = (i * total_students) // n_buckets
        end = ((i + 1) * total_students) // n_buckets
        group = counts[start:end]
        group_sum = sum(group)
        cumulative += group_sum
        buckets.append(
            {
                "label": f"p{round(100 * start / total_students)}–{round(100 * end / total_students)}",
                "n_students": len(group),
                "min": group[0],
                "max": group[-1],
                "sum": group_sum,
                "pct_of_total": (group_sum / total * 100) if total else 0.0,
                "cumulative_pct": (cumulative / total * 100) if total else 0.0,
            }
        )
    return buckets


def admin_overview():
    student_ids = [uid for (uid,) in db.session.query(User.id).filter_by(role="student").all()]
    n_students = len(student_ids)

    n_tutorials = db.session.query(func.count(Tutorial.id)).scalar() or 0
    n_scenarios = db.session.query(func.count(Scenario.id)).scalar() or 0
    n_attempts = db.session.query(func.count(Attempt.id)).scalar() or 0

    total_calls, total_prompt, total_completion, total_cost = db.session.query(
        func.count(LLMCall.id),
        func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
        func.coalesce(func.sum(LLMCall.completion_tokens), 0),
        func.coalesce(func.sum(LLMCall.cost_estimate_cents), 0.0),
    ).one()

    scenarios_by_user = dict(
        db.session.query(Scenario.user_id, func.count(Scenario.id)).group_by(Scenario.user_id).all()
    )
    tutorials_read_by_user = dict(
        db.session.query(TutorialRead.user_id, func.count(TutorialRead.id))
        .filter(TutorialRead.read_at.isnot(None))
        .group_by(TutorialRead.user_id)
        .all()
    )

    scenario_counts = [scenarios_by_user.get(uid, 0) for uid in student_ids]
    tutorial_counts = [tutorials_read_by_user.get(uid, 0) for uid in student_ids]

    return {
        "n_students": n_students,
        "n_tutorials": n_tutorials,
        "n_scenarios": n_scenarios,
        "n_attempts": n_attempts,
        "total_calls": total_calls,
        "total_prompt": total_prompt,
        "total_completion": total_completion,
        "total_cost": total_cost,
        "scenario_distribution": _bucketed_distribution(scenario_counts),
        "tutorial_distribution": _bucketed_distribution(tutorial_counts),
    }
