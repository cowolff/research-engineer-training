"""No Redis/Celery on atlasflow (no volumes, no managed queue service) — this
is the whole job queue. A single dispatcher thread claims work from the
`jobs` table (safe without row locking, since it's the only claimer) and
hands each job to a small thread pool sized to match the LLM client's own
concurrency cap. See docs §3 and §5.1.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("app.jobs")

_POLL_INTERVAL_SECONDS = 0.5
_MAX_CONCURRENT_JOBS = 2

_executor = None
_stop_event = threading.Event()


def start_worker(app):
    global _executor
    _executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_JOBS, thread_name_prefix="job-worker")
    thread = threading.Thread(target=_poll_loop, args=(app,), daemon=True, name="job-dispatcher")
    thread.start()
    logger.info("job_worker_started")


def _poll_loop(app):
    from app.jobs.dispatch import claim_next_job

    while not _stop_event.is_set():
        try:
            with app.app_context():
                job_id = claim_next_job()
        except Exception:  # noqa: BLE001 - the dispatcher loop must never die
            logger.exception("job_claim_failed")
            job_id = None

        if job_id is None:
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        _executor.submit(_run_with_context, app, job_id)


def _run_with_context(app, job_id):
    from app.jobs.dispatch import run_job

    with app.app_context():
        run_job(job_id)
