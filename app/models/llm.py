import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import db


def _uuid():
    return uuid.uuid4().hex


class LLMCall(db.Model):
    """One row per LLM API call — tokens, latency, and an estimated cost.
    Never stores prompt or completion text by default (§10 Security: no
    secret or student text logged beyond what's already in scenarios/attempts).
    Surfaced at /admin/usage, which doubles as a teaching artefact about LLM
    cost accounting."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # scenario|converse|tutorial|term_help
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_estimate_cents: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
