import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import db


def _uuid():
    return uuid.uuid4().hex


class Job(db.Model):
    """The job queue. No Redis/Celery on Atlasflow (no volumes, no managed
    queue service) — this table plus an in-process ThreadPoolExecutor is the
    whole queue. A `running` job left over from a previous boot (the process
    died mid-job) is reaped to `failed` on startup; see app/jobs/reaper.py."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # generate_scenario|grade_attempt|generate_tutorial
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)  # queued|running|done|failed
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def payload(self):
        return self.payload_json or {}

    @property
    def result(self):
        return self.result_json or {}
