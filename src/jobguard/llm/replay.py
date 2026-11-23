"""Record/replay wrapper for deterministic CI.

- ``record``: delegates to the inner client and appends every (prompt, reply)
  pair to a JSONL file for later replay.
- ``replay``: serves replies from the JSONL file; raises :class:`LLMError` on
  a cache miss so unexpected new prompts fail loudly instead of hitting the
  network in CI.

Keys are SHA-256 hashes of ``system + "\\n" + user`` — content-addressed, and
replay files only ever contain prompts the suite already made.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from jobguard.llm.client import LLMClient, LLMError, LLMResponse


def prompt_key(system: str, user: str) -> str:
    """Stable content hash identifying one (system, user) prompt pair."""
    digest = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()
    return digest


class RecordReplayClient:
    """Wraps another client with JSONL record/replay."""

    def __init__(
        self,
        inner: LLMClient,
        path: str | Path,
        *,
        mode: Literal["record", "replay"],
    ) -> None:
        self._inner = inner
        self._path = Path(path)
        self._mode = mode
        self._records: dict[str, dict[str, object]] = {}
        if mode == "replay":
            self._records = self._load()

    @property
    def name(self) -> str:
        return f"{self._inner.name}+{self._mode}"

    def _load(self) -> dict[str, dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        if not self._path.exists():
            return records
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            records[str(entry["key"])] = entry
        return records

    def complete(self, *, system: str, user: str) -> LLMResponse:
        key = prompt_key(system, user)
        if self._mode == "replay":
            entry = self._records.get(key)
            if entry is None:
                raise LLMError(f"Replay miss for prompt {key[:12]}... in {self._path}")
            data = entry["response"]
            if not isinstance(data, dict):
                raise LLMError(f"Corrupt replay entry for prompt {key[:12]}...")
            return LLMResponse.model_validate(data)
        response = self._inner.complete(system=system, user=user)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"key": key, "response": response.model_dump()},
                    ensure_ascii=True,
                )
                + "\n"
            )
        return response
