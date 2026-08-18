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


class CoverageItem(BaseModel):
    """The model's cumulative judgement on one rubric item: has the student
    covered it at any point in the conversation so far? `evidence` must quote
    the student's own words — app/training/conversation.py enforces that
    against the real message history and downgrades an unevidenced `covered`,
    the same rule §5.3 applies to single-shot grading."""

    concept_id: str
    status: Literal[GRADE_STATUSES]  # type: ignore[valid-type]
    evidence: str | None = None


class ConversationTurnSpec(BaseModel):
    """One assistant turn in a Socratic training conversation (§5.5).

    `follow_up_question` is the nudge — the pedagogical crux: it must open the
    door to an uncovered rubric item without naming it. `model_answer_md` is
    only requested on a closing turn (the app asks for it via IS_FINAL_TURN),
    so it stays empty on ordinary turns rather than generating a full worked
    answer that gets discarded on every mid-conversation turn.
    """

    coverage: list[CoverageItem]
    reply_md: str
    follow_up_question: str = ""
    nudge_concept_ids: list[str] = Field(default_factory=list)
    model_answer_md: str = ""


class HelpAnswerSpec(BaseModel):
    """One answer from the side chat next to a scenario (§5.6).

    There is no coverage or grading field here on purpose: the side chat is
    a glossary, not a second assessor, and nothing it returns is allowed to
    move the rubric. `declined` is set when the question was an attempt to
    get the exercise answered rather than a term explained — recorded rather
    than silently swallowed so the transcript stays honest about what was
    asked and what came back.
    """

    answer_md: str
    declined: bool = False


class TutorialSpec(BaseModel):
    title: str
    body_md: str
    exercise_md: str = ""
    related_concept_ids: list[str] = Field(default_factory=list)
    cited_resource_ids: list[str] = Field(default_factory=list)
    reading_order: list[str] = Field(default_factory=list)
