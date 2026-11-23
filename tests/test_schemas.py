"""Tests for the typed LLM-output schemas."""

from __future__ import annotations

import pytest

from jobguard.llm.schemas import (
    Classification,
    PostingExtraction,
    SchemaValidationError,
    TailoringCard,
    parse_strict,
)


def test_classification_valid() -> None:
    parsed = parse_strict(Classification, '{"kind": "posting", "confidence": 0.9}')
    assert isinstance(parsed, Classification)
    assert parsed.kind == "posting"


def test_posting_extraction_requires_company_and_role() -> None:
    with pytest.raises(SchemaValidationError):
        parse_strict(PostingExtraction, '{"role": "SDE Intern"}')


def test_tailoring_card_gap_ok() -> None:
    parsed = parse_strict(
        TailoringCard,
        '{"posting_key": "k", "suggestions": '
        '[{"kind": "gap", "resume_item_ids": [], "text": "Needs Go"}], '
        '"gaps": ["Go"]}',
    )
    assert isinstance(parsed, TailoringCard)
    assert parsed.suggestions[0].kind == "gap"


def test_invalid_kind_rejected() -> None:
    with pytest.raises(SchemaValidationError):
        parse_strict(Classification, '{"kind": "not-a-kind"}')
