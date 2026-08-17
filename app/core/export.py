"""Full-fidelity JSON export of one user's data — the only durability
guarantee against atlasflow's ephemeral disk (docs §9). Ships in Phase 2, not
'later', because it's the thing that makes the SQLite tradeoff survivable."""

from sqlalchemy import inspect

from app.db import db
from app.models import Scenario, Attempt, ConceptMastery, Tutorial, TutorialRead


def _row_to_dict(obj):
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


def export_user_data(user):
    scenarios = db.session.query(Scenario).filter_by(user_id=user.id).all()
    attempts = db.session.query(Attempt).filter_by(user_id=user.id).all()
    mastery = db.session.query(ConceptMastery).filter_by(user_id=user.id).all()
    reads = db.session.query(TutorialRead).filter_by(user_id=user.id).all()
    tutorials = [db.session.get(Tutorial, r.tutorial_id) for r in reads]

    return {
        "user": {"email": user.email, "created_at": str(user.created_at), "role": user.role},
        "scenarios": [_row_to_dict(s) for s in scenarios],
        "attempts": [_row_to_dict(a) for a in attempts],
        "concept_mastery": [_row_to_dict(m) for m in mastery],
        "tutorials": [_row_to_dict(t) for t in tutorials if t],
        "tutorial_reads": [_row_to_dict(r) for r in reads],
    }
