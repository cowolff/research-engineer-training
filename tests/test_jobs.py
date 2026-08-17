from datetime import datetime

from app.db import db
from app.models import User, Job
from app.jobs.reaper import reap_stale_jobs


def test_running_job_from_previous_boot_is_reaped_to_failed(app):
    with app.app_context():
        db.session.add(User(id="u1", email="l@example.com", password_hash="x"))
        db.session.flush()
        db.session.add(
            Job(id="j1", user_id="u1", kind="generate_scenario", status="running", started_at=datetime.utcnow())
        )
        db.session.add(Job(id="j2", user_id="u1", kind="generate_scenario", status="queued"))
        db.session.commit()

        count = reap_stale_jobs()

        assert count == 1
        assert db.session.get(Job, "j1").status == "failed"
        assert db.session.get(Job, "j2").status == "queued"  # untouched
