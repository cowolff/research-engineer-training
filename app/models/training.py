import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import db


def _uuid():
    return uuid.uuid4().hex


class Scenario(db.Model):
    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    topic_id: Mapped[str] = mapped_column(String(64), ForeignKey("topics.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # architecture|debug_artifact|design_review|tradeoff|concept
    band: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    prompt_md: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rubric_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    target_concepts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    attempts: Mapped[list["Attempt"]] = relationship(back_populates="scenario")

    @property
    def rubric(self):
        return self.rubric_json or []

    @property
    def target_concepts(self):
        return self.target_concepts_json or []

    @property
    def artifacts(self):
        return self.artifacts_json or []


class Attempt(db.Model):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(String(32), ForeignKey("scenarios.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_answer_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    scenario: Mapped["Scenario"] = relationship(back_populates="attempts")

    @property
    def grade(self):
        return self.grade_json or {}


class ConceptEvent(db.Model):
    """Append-only ledger — the source of truth for mastery. Never updated,
    only inserted; concept_mastery is a derived rollup kept in sync alongside
    it and can always be rebuilt from this table (`flask recompute-mastery`)."""

    __tablename__ = "concept_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    concept_id: Mapped[str] = mapped_column(String(64), ForeignKey("concepts.id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), ForeignKey("attempts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # covered|partial|missed
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ConceptMastery(db.Model):
    """Maintained rollup so the tutorial-trigger check is one indexed read
    instead of scanning concept_events every time."""

    __tablename__ = "concept_mastery"
    __table_args__ = (UniqueConstraint("user_id", "concept_id", name="uq_concept_mastery_user_concept"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    concept_id: Mapped[str] = mapped_column(String(64), ForeignKey("concepts.id"), nullable=False, index=True)
    misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    covers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_miss_scenarios_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tutorial_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tutorials.id"), nullable=True)
