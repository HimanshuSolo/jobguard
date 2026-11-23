"""Tests for resume loading and the fabrication checker."""

from __future__ import annotations

from jobguard.llm.schemas import TailoringCard, TailoringSuggestion
from jobguard.resume import Resume, ResumeItem, check_card, load_resume


def test_load_example_resume() -> None:
    resume = load_resume("data/resume.example.yaml")
    assert resume.owner == "Alex Example"
    ids = resume.known_ids()
    assert {"edu-1", "exp-1", "proj-1", "skill-1", "skill-2"} <= ids


def _card(suggestions: list[TailoringSuggestion], gaps: list[str] | None = None) -> TailoringCard:
    return TailoringCard(posting_key="k", suggestions=suggestions, gaps=gaps or [])


def test_clean_card_passes() -> None:
    resume = Resume(owner="T", items=[ResumeItem(id="exp-1", title="Intern")])
    card = _card(
        [
            TailoringSuggestion(
                kind="reword", resume_item_ids=["exp-1"], text="Emphasize FastAPI work."
            ),
            TailoringSuggestion(kind="gap", resume_item_ids=[], text="Requires Go; not in resume."),
        ],
        gaps=["Go"],
    )
    assert check_card(card, resume) == []


def test_unknown_id_is_violation() -> None:
    resume = Resume(owner="T", items=[])
    card = _card([TailoringSuggestion(kind="emphasize", resume_item_ids=["exp-99"], text="X")])
    violations = check_card(card, resume)
    assert len(violations) == 1
    assert "exp-99" in violations[0]


def test_gap_citing_ids_is_violation() -> None:
    resume = Resume(owner="T", items=[ResumeItem(id="exp-1")])
    card = _card([TailoringSuggestion(kind="gap", resume_item_ids=["exp-1"], text="Y")])
    assert any("GAP" in v for v in check_card(card, resume))


def test_reword_without_ids_is_violation() -> None:
    resume = Resume(owner="T", items=[])
    card = _card([TailoringSuggestion(kind="reword", resume_item_ids=[], text="Z")])
    assert len(check_card(card, resume)) == 1
