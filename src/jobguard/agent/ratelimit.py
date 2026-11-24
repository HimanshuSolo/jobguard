"""Token-bucket rate limiter (Phase 8, Step 2.3).

Throttles real network calls — never local work, skips, or cache hits. Two
buckets (requests, tokens) refill continuously; `acquire` sleeps only the
deficit. Clock and sleeper are injectable so tests run in virtual time with
zero real sleeping. Thread-safe for the worker pool (Step 2.7).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Shared token bucket: `acquire` blocks (via sleeper) until allowed."""

    def __init__(
        self,
        *,
        requests_per_minute: float,
        tokens_per_minute: float,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if requests_per_minute <= 0 or tokens_per_minute <= 0:
            raise ValueError("rates must be positive")
        self._req_rate = requests_per_minute / 60.0
        self._tok_rate = tokens_per_minute / 60.0
        self._req_tokens = requests_per_minute  # burst: one minute of budget
        self._tok_tokens = tokens_per_minute
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._not_before = 0.0
        self._lock = threading.Lock()
        now = self._clock()
        self._req_stamp = now
        self._tok_stamp = now

    def _refill(self, now: float) -> None:
        self._req_tokens = min(
            self._req_tokens + (now - self._req_stamp) * self._req_rate,
            self._req_rate * 60.0,
        )
        self._tok_tokens = min(
            self._tok_tokens + (now - self._tok_stamp) * self._tok_rate,
            self._tok_rate * 60.0,
        )
        self._req_stamp = now
        self._tok_stamp = now

    def acquire(self, tokens: int = 0) -> None:
        """Consume one request token plus `tokens` usage tokens, waiting as needed."""
        while True:
            with self._lock:
                now = self._clock()
                if now < self._not_before:
                    wait = self._not_before - now
                else:
                    self._refill(now)
                    req_wait = max(0.0, (1.0 - self._req_tokens) / self._req_rate)
                    tok_wait = max(0.0, (float(tokens) - self._tok_tokens) / self._tok_rate)
                    wait = max(req_wait, tok_wait)
                    if wait <= 0:
                        self._req_tokens -= 1.0
                        self._tok_tokens -= float(tokens)
                        return
            self._sleep(wait)

    def note_retry_after(self, seconds: float) -> None:
        """Honor a provider Retry-After: block all acquires until it elapses."""
        if seconds <= 0:
            return
        with self._lock:
            self._not_before = max(self._not_before, self._clock() + seconds)

    def observe_waits(self, observer: Callable[[float], None]) -> None:
        """Chain an observer onto the sleeper (metering, incl. fake clocks).

        The observer runs before each sleep with the waited seconds. Safe
        to call once per limiter; wrapping twice would double-count.
        """
        inner = self._sleep

        def _sleep(seconds: float) -> None:
            observer(seconds)
            inner(seconds)

        self._sleep = _sleep
