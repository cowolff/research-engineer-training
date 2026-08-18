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
    """One student's whole conversation about a scenario, not a single answer
    (§5.5). Turns hang off it as `ConversationTurn` rows.

    `answer_text` is deliberately kept as the student's **first** message —
    their unassisted attempt, before any nudge. That's what tutorial
    generation quotes back ("you were asked X, you answered Y"), and quoting
    a later, already-nudged message there would misrepresent what they
    actually knew on their own.
    """

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

    # in_progress until the conversation closes (all essential rubric items
    # covered, or the turn budget is spent) — only then does the gap ledger
    # get written, exactly once. See app/training/conversation.py.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress", index=True)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Cumulative, monotonic per-concept coverage across all turns:
    #   {concept_id: {"status": ..., "evidence": ..., "first_covered_turn": int|None}}
    # `first_covered_turn` is what separates "knew it unaided" (turn 1) from
    # "got there after a nudge" (turn > 1) when the final grade is committed.
    coverage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    scenario: Mapped["Scenario"] = relationship(back_populates="attempts")
    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="attempt", order_by="ConversationTurn.turn_index", cascade="all, delete-orphan"
    )

    @property
    def grade(self):
        return self.grade_json or {}

    @property
    def coverage(self):
        return self.coverage_json or {}

    @property
    def is_complete(self):
        return self.status == "complete"


class ConversationTurn(db.Model):
    """One exchange: what the student said, and how the assistant replied.

    Stored per-turn rather than as one growing blob so the chat transcript can
    be rendered directly, the nudge that was actually offered stays auditable
    (`nudge_concept_ids`), and coverage progress is inspectable turn by turn.
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (UniqueConstraint("attempt_id", "turn_index", name="uq_conversation_turns_attempt_index"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    attempt_id: Mapped[str] = mapped_column(String(32), ForeignKey("attempts.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    student_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_reply_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    follow_up_question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    nudge_concept_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    attempt: Mapped["Attempt"] = relationship(back_populates="turns")

    @property
    def nudge_concept_ids(self):
        return self.nudge_concept_ids_json or []


class HelpExchange(db.Model):
    """One question asked in the side chat beside a scenario, and the answer
    it got (§5.6).

    Keyed on (scenario, user) rather than on an `Attempt`: the window is open
    from the moment the scenario loads, which is before the student has sent
    a first message and therefore before an `Attempt` row exists at all.

    Persisted rather than held in the page because three things need it: the
    per-scenario cap has to be countable, the htmx poll re-renders the whole
    panel from the database, and the closing summary shows what the student
    had to look up — a real signal about vocabulary, kept deliberately
    separate from the rubric. Nothing here ever reaches `coverage_json`; see
    app/training/help.py.
    """

    __tablename__ = "help_exchanges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(String(32), ForeignKey("scenarios.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # True when the question was an attempt to get the exercise answered
    # rather than a term explained, and the side chat sent them back to the
    # main conversation. Recorded, not swallowed — see HelpAnswerSpec.
    declined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


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
