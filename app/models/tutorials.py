import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import db


def _uuid():
    return uuid.uuid4().hex


class Tutorial(db.Model):
    """Canonical, shared across students — one row per concept. Generated once
    by whichever student first trips the trigger; every later trigger for the
    same concept reuses this row instead of calling the LLM again. See
    docs/IMPLEMENTATION_PLAN.md §5.4 / §6.2 for why."""

    __tablename__ = "tutorials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    concept_id: Mapped[str] = mapped_column(String(64), ForeignKey("concepts.id"), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    exercise_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cited_resource_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reading_order_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    related_concept_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_attempt_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    reads: Mapped[list["TutorialRead"]] = relationship(back_populates="tutorial")

    @property
    def cited_resource_ids(self):
        return self.cited_resource_ids_json or []

    @property
    def reading_order(self):
        return self.reading_order_json or []

    @property
    def related_concept_ids(self):
        return self.related_concept_ids_json or []


class TutorialRead(db.Model):
    """Thin per-user state on top of a shared tutorial: has this student been
    assigned it, read it, and re-tested successfully."""

    __tablename__ = "tutorial_reads"
    __table_args__ = (UniqueConstraint("user_id", "tutorial_id", name="uq_tutorial_reads_user_tutorial"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    tutorial_id: Mapped[str] = mapped_column(String(32), ForeignKey("tutorials.id"), nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retested_passed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tutorial: Mapped["Tutorial"] = relationship(back_populates="reads")

    @property
    def state(self):
        if self.retested_passed_at:
            return "retested"
        if self.read_at:
            return "read"
        return "unread"


class TutorialLink(db.Model):
    """A resolved cross-link from a tutorial to a concept. `kind` records where
    the link came from: a curriculum-declared `related` edge, an LLM
    suggestion, or a backlink written when another tutorial links here."""

    __tablename__ = "tutorial_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    from_tutorial_id: Mapped[str] = mapped_column(String(32), ForeignKey("tutorials.id"), nullable=False, index=True)
    to_concept_id: Mapped[str] = mapped_column(String(64), ForeignKey("concepts.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # curriculum|llm|backlink
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class ResourceCitation(db.Model):
    """Backlink from a tutorial to a resource in the (separate, read-only)
    resource index — lets a resource page list who cites it."""

    __tablename__ = "resource_citations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tutorial_id: Mapped[str] = mapped_column(String(32), ForeignKey("tutorials.id"), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ResourceReport(db.Model):
    """The curation queue: a student flagging a resource as dead or unhelpful."""

    __tablename__ = "resource_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
