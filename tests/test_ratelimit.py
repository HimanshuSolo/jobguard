"""Tests for the token-bucket limiter (fake clock, recording sleeper, no network)."""

from __future__ import annotations

import threading

import pytest

from jobguard.agent.ratelimit import RateLimiter


class FakeClock:
    """Manual clock; sleeps advance it instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(clock: FakeClock, req: float = 60.0, tok: float = 6000.0) -> RateLimiter:
    return RateLimiter(
        requests_per_minute=req,
        tokens_per_minute=tok,
        clock=clock.clock,
        sleeper=clock.sleep,
    )


def test_acquire_within_budget_never_sleeps() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(60):
        limiter.acquire(50)
    assert clock.sleeps == []


def test_request_bucket_throttles() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, req=60.0, tok=10**9)
    for _ in range(60):
        limiter.acquire(0)
    # Bucket refills continuously: the 61st call waits ~1s for one token.
    limiter.acquire(0)
    assert clock.sleeps == [1.0]


def test_token_bucket_throttles() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, req=10**9, tok=600.0)
    limiter.acquire(600)  # spends the whole minute bucket at once
    assert clock.sleeps == []
    limiter.acquire(600)
    assert clock.sleeps == [60.0]


def test_retry_after_blocks_acquires() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    limiter.note_retry_after(30.0)
    limiter.acquire(0)
    assert clock.sleeps == [30.0]
    assert clock.now == 1030.0


def test_retry_after_nonpositive_ignored() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    limiter.note_retry_after(0)
    limiter.note_retry_after(-5)
    limiter.acquire(0)
    assert clock.sleeps == []


def test_invalid_rates_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(requests_per_minute=0, tokens_per_minute=1)
    with pytest.raises(ValueError):
        RateLimiter(requests_per_minute=1, tokens_per_minute=-1)


def test_thread_safe_shared_limiter() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, req=120.0, tok=10**9)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(70):
                limiter.acquire(0)
        except BaseException as exc:  # pragma: no cover - safety net
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert not any(t.is_alive() for t in threads)
    # 120 allowed instantly; the rest waited without deadlocking.
    assert clock.now > 1000.0


def test_refill_over_virtual_time() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, req=60.0, tok=10**9)
    for _ in range(60):
        limiter.acquire(0)
    clock.now += 30.0  # half a minute passes with no calls
    for _ in range(30):
        limiter.acquire(0)
    assert clock.sleeps == []  # refilled 30 tokens, no waiting needed
