"""Source adapter interface. All content from adapters is untrusted data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    """One raw item exactly as an adapter returned it (pre-normalization)."""

    source: str = Field(description="Adapter name, e.g. 'gmail' or 'board:greenhouse:acme'.")
    external_id: str = Field(default="", description="Stable ID in the upstream system.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Untrusted upstream data.")
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SourceAdapter(Protocol):
    """Fetch new raw items since an opaque cursor."""

    @property
    def source_name(self) -> str: ...

    def fetch(self, since: str) -> list[SourceItem]:
        """Return items newer than ``since``. Never raises for empty results."""
        ...

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        """New cursor to persist after a successful fetch."""
        ...
