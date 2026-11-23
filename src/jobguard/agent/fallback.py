"""Provider fallback, per-run token budget, and circuit breaker (Phase 4).

Free-tier reality: providers rate-limit and fail. `FallbackClient` tries the
primary then each secondary on `LLMError`. `TokenBudget` caps estimated spend
per worker run. `CircuitBreaker` stops hammering a provider that keeps
failing. All three are deterministic and unit-tested with fakes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jobguard.llm.client import LLMClient, LLMError, LLMResponse


class FallbackClient:
    """Try clients in order; first success wins, else raise the last error."""

    def __init__(self, primary: LLMClient, *secondaries: LLMClient) -> None:
        if not secondaries:
            raise ValueError("FallbackClient needs at least one secondary")
        self._clients = [primary, *secondaries]

    @property
    def name(self) -> str:
        return "fallback(" + ",".join(c.name for c in self._clients) + ")"

    def complete(self, *, system: str, user: str) -> LLMResponse:
        error: LLMError = LLMError("no clients configured")
        for client in self._clients:
            try:
                return client.complete(system=system, user=user)
            except LLMError as exc:
                error = exc
        raise error


class BudgetExhausted(RuntimeError):
    """Raised when the per-run token budget would be exceeded."""


@dataclass(eq=False)
class TokenBudget:
    """Cap estimated token spend per worker run (free-tier protection).

    Thread-safe: the worker pool charges from multiple threads.
    """

    max_tokens: int
    used_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, *, system: str, user: str, response: LLMResponse) -> None:
        """Account one call; estimates when the provider reports no usage."""
        reported = response.input_tokens + response.output_tokens
        estimate = (len(system) + len(user) + len(response.text)) // 4
        cost = reported if reported > 0 else estimate
        with self._lock:
            if self.used_tokens + cost > self.max_tokens:
                raise BudgetExhausted(
                    f"token budget {self.max_tokens} exceeded (used {self.used_tokens})"
                )
            self.used_tokens += cost


@dataclass(eq=False)
class CircuitBreaker:
    """Open the circuit after N consecutive failures; fail fast while open.

    Thread-safe: counters are guarded for the worker pool.
    """

    inner: LLMClient
    threshold: int = 3
    cooldown_s: float = 300.0
    failures: int = 0
    opened_at: float | None = None
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def name(self) -> str:
        return f"circuit({self.inner.name})"

    @property
    def is_open(self) -> bool:
        """True while the circuit is open (cooling down after failures)."""
        with self._lock:
            if self.opened_at is None:
                return False
            if self._clock() - self.opened_at >= self.cooldown_s:
                self.opened_at = None
                self.failures = 0
                return False
            return True

    def complete(self, *, system: str, user: str) -> LLMResponse:
        if self.is_open:
            raise LLMError(f"circuit open for {self.inner.name}")
        try:
            response = self.inner.complete(system=system, user=user)
        except LLMError:
            with self._lock:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = self._clock()
            raise
        with self._lock:
            self.failures = 0
        return response
