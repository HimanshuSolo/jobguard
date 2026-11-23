"""Deterministic Fake client for unit tests, per-push CI, and DEMO_MODE.

Matches the ``user`` prompt against an ordered list of (substring, response)
rules; the first match wins, otherwise ``default`` is returned. No network,
no randomness — the same prompt always yields the same response.
"""

from __future__ import annotations

from jobguard.llm.client import LLMResponse


class FakeClient:
    """Canned, deterministic LLMClient."""

    def __init__(
        self,
        rules: list[tuple[str, str]] | None = None,
        *,
        default: str = "{}",
        model: str = "fake-test-model",
    ) -> None:
        self._rules = list(rules or [])
        self._default = default
        self._model = model
        self.calls: list[dict[str, str]] = []

    @property
    def name(self) -> str:
        return "fake"

    def complete(self, *, system: str, user: str) -> LLMResponse:
        self.calls.append({"system": system, "user": user})
        for needle, response in self._rules:
            if needle in user:
                return LLMResponse(model=self._model, text=response)
        return LLMResponse(model=self._model, text=self._default)
