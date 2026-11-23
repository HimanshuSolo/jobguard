"""Structured resume loading (Phase 4).

The real resume lives at ``data/private/resume.yaml`` (gitignored); code and
tests only ever touch ``data/resume.example.yaml`` or explicit paths. Item
IDs (``exp-1``, ``proj-1``, ``skill-1``, ...) are the only tokens the LLM
pipeline may cite — everything else is a GAP.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ResumeItem(BaseModel):
    """One citable resume entry."""

    id: str
    title: str = ""
    detail: str = ""


class Resume(BaseModel):
    """Structured resume with a stable, citable item inventory."""

    owner: str = ""
    headline: str = ""
    items: list[ResumeItem] = Field(default_factory=list)

    def known_ids(self) -> set[str]:
        """Every ID the fabrication checker accepts."""
        return {item.id for item in self.items}


def _resume_from_dict(raw: dict[str, Any]) -> Resume:
    """Build a Resume from an already-parsed mapping (example-shape YAML)."""
    items: list[ResumeItem] = []
    for section in ("education", "experience", "projects"):
        entries = raw.get(section, [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("id"):
                    bullets = entry.get("bullets", [])
                    detail = "; ".join(b for b in bullets if isinstance(b, str))
                    title = str(
                        entry.get("title") or entry.get("name") or entry.get("degree") or ""
                    )
                    items.append(ResumeItem(id=str(entry["id"]), title=title, detail=detail))
    skills = raw.get("skills", [])
    if isinstance(skills, list):
        for skill in skills:
            if isinstance(skill, dict) and skill.get("id"):
                items.append(ResumeItem(id=str(skill["id"]), title=str(skill.get("name", ""))))
            elif isinstance(skill, str) and skill.strip():
                items.append(ResumeItem(id=f"skill:{skill.strip().casefold()}"))
    return Resume(
        owner=str(raw.get("owner", "")),
        headline=str(raw.get("headline", "")),
        items=items,
    )


def load_resume(path: str) -> Resume:
    """Load and validate a resume YAML file (example shape in resume.example.yaml)."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return _resume_from_dict(raw)


def resume_dict_from_text(text: str) -> dict[str, Any]:
    """Best-effort resume mapping from pasted text or an uploaded file.

    Structured YAML in the example shape passes through validated; plain
    text becomes one experience section (one item per bullet line) plus a
    skills section parsed from `Skill:`/`Skills:` lines, so the result is
    always re-loadable by `load_resume`.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        raw = None
    if isinstance(raw, dict) and any(
        key in raw for key in ("education", "experience", "projects", "skills")
    ):
        _resume_from_dict(raw)  # validate IDs/shape now, fail fast
        return raw
    skills: list[str] = []
    bullets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*• ").strip()
        if not stripped:
            continue
        skill_match = re.match(r"(?i)^skills?\s*[:\-]\s*(.+)$", stripped)
        if skill_match:
            skills.extend(s.strip() for s in re.split(r"[,|/]", skill_match.group(1)) if s.strip())
        elif len(stripped) >= 4:
            bullets.append(stripped[:300])
    experience = (
        [{"id": "exp-u1", "title": "Uploaded resume", "bullets": bullets}] if bullets else []
    )
    return {
        "owner": "",
        "headline": "Uploaded resume",
        "education": [],
        "experience": experience,
        "projects": [],
        "skills": [{"id": f"skill-u{i + 1}", "name": name} for i, name in enumerate(skills[:40])],
    }


def write_resume_yaml(data: dict[str, Any], path: str) -> None:
    """Persist a resume mapping after validating it loads cleanly."""
    _resume_from_dict(data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
