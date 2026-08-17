from datetime import date

from flask import current_app

from app.db import db


class QuotaExceeded(Exception):
    pass


def check_and_increment(user):
    """Enforced BEFORE a job is enqueued, not after — see docs §10 Security,
    'LLM cost abuse'. Resets the counter on the first call of a new UTC day."""
    cfg = current_app.app_config
    today = date.today()
    if user.daily_window_start != today:
        user.daily_window_start = today
        user.daily_llm_calls = 0

    if user.daily_llm_calls >= cfg.max_llm_calls_per_day:
        raise QuotaExceeded(f"Daily limit of {cfg.max_llm_calls_per_day} generations reached. Try again tomorrow.")

    user.daily_llm_calls += 1
    db.session.commit()
