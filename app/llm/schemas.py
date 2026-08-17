"""Pydantic contracts for everything the LLM returns. Validation happens here,
not by trusting the model — see docs/IMPLEMENTATION_PLAN.md §5."""

from typing import Literal

from pydantic import BaseModel, Field

SCENARIO_TYPES = ("architecture", "debug_artifact", "design_review", "tradeoff", "concept")
GRADE_STATUSES = ("covered", "partial", "missed")


class Artifact(BaseModel):
    label: str
    language: str = "text"
    content: str


class RubricItem(BaseModel):
    concept_id: str
    expected: str
    weight: int = Field(ge=1, le=5, default=1)
    essential: bool = False


class ScenarioSpec(BaseModel):
    type: Literal[SCENARIO_TYPES]  # type: ignore[valid-type]
    title: str
    prompt_md: str
    artifacts: list[Artifact] = Field(default_factory=list)
    rubric: list[RubricItem]


class GradeItem(BaseModel):
    concept_id: str
    status: Literal[GRADE_STATUSES]  # type: ignore[valid-type]
    evidence: str | None = None
    feedback: str = ""


class GradeReport(BaseModel):
    items: list[GradeItem]
    score: float = Field(ge=0.0, le=1.0)
    strengths_md: str = ""
    model_answer_md: str = ""


class TutorialSpec(BaseModel):
    title: str
    body_md: str
    exercise_md: str = ""
    related_concept_ids: list[str] = Field(default_factory=list)
    cited_resource_ids: list[str] = Field(default_factory=list)
    reading_order: list[str] = Field(default_factory=list)
