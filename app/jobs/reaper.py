import logging
from datetime import datetime

from app.db import db
from app.models import Job

logger = logging.getLogger("app.jobs")


def reap_stale_jobs():
    """A `running` job with no process left to finish it (the container
    restarted, redeployed, or crashed mid-job) is marked failed on the next
    boot rather than sitting invisible forever — docs §14 'Job-lifecycle
    tests'."""
    stale = db.session.query(Job).filter_by(status="running").all()
    for job in stale:
        job.status = "failed"
        job.error = "Reaped at startup: the process handling this job restarted before it finished."
        job.finished_at = datetime.utcnow()
    if stale:
        db.session.commit()
        logger.info("jobs_reaped", extra={"extra_fields": {"count": len(stale)}})
    return len(stale)
