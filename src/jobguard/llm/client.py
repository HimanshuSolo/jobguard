"""Provider-agnostic LLM interface.

Every provider adapter implements :class:`LLMClient`. The pipeline (Phase 4)
only ever talks to this protocol, so providers stay swappable and tests can
substitute the Fake / record-replay clients with zero network.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class LLMResponse(BaseModel):
    """Metadata + text returned by any LLM call. No prompt content is stored."""

    model: str = Field(description="Model identifier that produced the text.")
    text: str = Field(description="Raw model output text.")
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)


class LLMError(RuntimeError):
    """Raised for transport, provider, or replay failures."""


class LLMClient(Protocol):
    """Sync, deterministic-friendly interface all providers implement."""

    @property
    def name(self) -> str:
        """Human-readable provider name (used in audit logs)."""
        ...

    def complete(self, *, system: str, user: str) -> LLMResponse:
        """Run one chat completion. ``user`` content is untrusted data."""
        ...
