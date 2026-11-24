"""Typed schemas for every LLM pipeline step (SPEC section 6).

Each model is the contract for one step: Classification, PostingExtraction,
MatchResult, TailoringCard — plus the merged ClassifyExtract (steps 1+2 in
one call) and MatchAndSuggest (steps 3+4). Pipeline code parses raw model
text into these with :func:`parse_strict`, retrying once on validation
failure and then failing safely (skip the item, log it).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class SchemaValidationError(RuntimeError):
    """Raised when LLM output fails Pydantic validation."""

    def __init__(self, schema: str, errors: object) -> None:
        super().__init__(f"{schema} validation failed: {errors}")
        self.schema = schema
        self.errors = errors


class Classification(BaseModel):
    """Step 1: what kind of email/feed item is this?"""

    kind: Literal["posting", "newsletter", "otp", "spam", "other"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class PostingExtraction(BaseModel):
    """Step 2: structured fields pulled from a job posting."""

    company: str = Field(min_length=1)
    role: str = Field(min_length=1)
    location: str = ""
    deadline: str = ""
    link: str = ""
    skills: list[str] = Field(default_factory=list)
    eligibility: str = ""


class ClassifyExtract(BaseModel):
    """Steps 1+2 in one call: kind first, then fields (empty unless posting).

    Validates exactly as strictly as the separate schemas: ``kind`` carries
    the Classification contract, the rest the PostingExtraction contract.
    Non-postings stop here, so the fields stay empty for them and are only
    parsed when kind == "posting".
    """

    kind: Literal["posting", "newsletter", "otp", "spam", "other"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    deadline: str = ""
    link: str = ""
    skills: list[str] = Field(default_factory=list)
    eligibility: str = ""


class MatchResult(BaseModel):
    """Step 3: fit of one posting against the owner's profile."""

    score: float = Field(ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)
    resume_item_ids: list[str] = Field(default_factory=list)


class TailoringSuggestion(BaseModel):
    """One resume-tailoring suggestion: reword, emphasize, or a labelled GAP."""

    kind: Literal["reword", "emphasize", "gap"]
    resume_item_ids: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)


class TailoringCard(BaseModel):
    """Step 4: the full per-posting tailoring card shown on the dashboard."""

    posting_key: str = Field(min_length=1)
    suggestions: list[TailoringSuggestion] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class MatchAndSuggest(BaseModel):
    """Steps 3+4 in one call: fit score plus the tailoring card.

    Flat shape (not nested) so the model emits one object with one key list.
    Validates exactly as strictly as the separate schemas: the card half is
    checked by the same fabrication checker via ``as_tailoring_card``.
    """

    score: float = Field(ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)
    resume_item_ids: list[str] = Field(default_factory=list)
    posting_key: str = Field(min_length=1)
    suggestions: list[TailoringSuggestion] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    def as_tailoring_card(self) -> TailoringCard:
        """Project the card half for the fabrication checker."""
        return TailoringCard(
            posting_key=self.posting_key,
            suggestions=list(self.suggestions),
            gaps=list(self.gaps),
        )


def parse_strict(model: type[BaseModel], text: str) -> BaseModel:
    """Parse raw LLM text into ``model``; raise SchemaValidationError if bad."""
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        raise SchemaValidationError(model.__name__, exc.errors()) from exc


def schema_hint(model: type[BaseModel]) -> str:
    """Compact JSON-schema reminder pasted into prompts.

    Large models follow instructions well but cannot honor a schema they
    never saw; without this they invent plausible-looking wrong shapes.
    """
    import json as _json

    schema = model.model_json_schema()
    schema.pop("$defs", None)
    return _json.dumps(schema, ensure_ascii=True, separators=(",", ":"))
