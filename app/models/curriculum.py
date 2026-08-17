from sqlalchemy import String, Integer, Boolean, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import db


class Topic(db.Model):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, e.g. "web-auth"
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    band: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    curriculum_version: Mapped[int] = mapped_column(Integer, nullable=False)

    concepts: Mapped[list["Concept"]] = relationship(back_populates="topic", order_by="Concept.id")


class Concept(db.Model):
    __tablename__ = "concepts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, e.g. "containerization"
    topic_id: Mapped[str] = mapped_column(String(64), ForeignKey("topics.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    probe: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    aliases_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    related_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    curriculum_version: Mapped[int] = mapped_column(Integer, nullable=False)

    topic: Mapped["Topic"] = relationship(back_populates="concepts")

    @property
    def aliases(self):
        return self.aliases_json or []

    @property
    def related(self):
        return self.related_json or []
