"""Per-run LLM observability (Phase 9, Step 2.11).

Content-free metrics for one worker run: calls per posting, tokens in/out
per stage, latency p50/p95, seconds sleeping (limiter waits) vs. network
(call latency) vs. local (everything else), 429 count, cache-hit rate,
items per minute. Thread-safe: the worker pool records from many threads.

Retry backoff sleeps inside the client stay in the network bucket (they are
part of a call's latency); only limiter waits count as sleeping.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


def estimate_tokens(system: str, user: str, text: str) -> int:
    """Rough token estimate (len//4) when the provider reports no usage."""
    return (len(system) + len(user) + len(text)) // 4


def _percentile(sorted_xs: list[float], pct: float) -> float:
    if not sorted_xs:
        return 0.0
    rank = (len(sorted_xs) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(sorted_xs) - 1)
    frac = rank - low
    return sorted_xs[low] * (1.0 - frac) + sorted_xs[high] * frac


@dataclass(eq=False)
class RunMeter:
    """Accumulates one run's LLM metrics. All mutating methods are locked."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    calls: int = 0
    cache_hits: int = 0
    errors_429: int = 0
    sleep_s: float = 0.0
    network_s: float = 0.0
    tokens_in: dict[str, int] = field(default_factory=lambda: defaultdict(int), repr=False)
    tokens_out: dict[str, int] = field(default_factory=lambda: defaultdict(int), repr=False)
    latencies: list[float] = field(default_factory=list, repr=False)
    _start: float = field(default_factory=time.monotonic, repr=False)
    _end: float | None = field(default=None, repr=False)

    def record_call(
        self,
        stage: str,
        *,
        system: str,
        user: str,
        text: str,
        input_tokens: int,
        output_tokens: int,
        latency_s: float,
    ) -> None:
        """Account one real LLM call (retries count individually)."""
        reported_in = input_tokens if input_tokens > 0 else 0
        reported_out = output_tokens if output_tokens > 0 else 0
        if reported_in == 0 and reported_out == 0:
            estimate = estimate_tokens(system, user, text)
            reported_in, reported_out = estimate, 0
        with self._lock:
            self.calls += 1
            self.tokens_in[stage] += reported_in
            self.tokens_out[stage] += reported_out
            self.network_s += latency_s
            self.latencies.append(latency_s)

    def record_hit(self, stage: str) -> None:  # noqa: ARG002
        """Account one cache hit (zero network calls, zero budget)."""
        with self._lock:
            self.cache_hits += 1

    def record_429(self) -> None:
        """Account one 429 response seen by a client."""
        with self._lock:
            self.errors_429 += 1

    def record_sleep(self, seconds: float) -> None:
        """Account limiter-wait seconds (called from the sleeper hook)."""
        with self._lock:
            self.sleep_s += max(0.0, seconds)

    def finish(self) -> None:
        """Mark the run end (wall clock for items/min and local time)."""
        with self._lock:
            if self._end is None:
                self._end = time.monotonic()

    def summary(self, items_processed: int) -> dict[str, object]:
        """Content-free per-run metrics snapshot."""
        with self._lock:
            end = self._end if self._end is not None else time.monotonic()
            elapsed = max(end - self._start, 0.0)
            latencies = sorted(self.latencies)
            total_in = sum(self.tokens_in.values())
            total_out = sum(self.tokens_out.values())
            local = max(elapsed - self.network_s - self.sleep_s, 0.0)
            total_lookups = self.calls + self.cache_hits
            return {
                "items": items_processed,
                "calls": self.calls,
                "calls_per_posting": (self.calls / items_processed) if items_processed else 0.0,
                "cache_hits": self.cache_hits,
                "cache_hit_rate": (self.cache_hits / total_lookups) if total_lookups else 0.0,
                "tokens_in": total_in,
                "tokens_out": total_out,
                "tokens_in_per_stage": dict(self.tokens_in),
                "tokens_out_per_stage": dict(self.tokens_out),
                "latency_p50_s": round(_percentile(latencies, 50), 3),
                "latency_p95_s": round(_percentile(latencies, 95), 3),
                "sleep_s": round(self.sleep_s, 3),
                "network_s": round(self.network_s, 3),
                "local_s": round(local, 3),
                "errors_429": self.errors_429,
                "elapsed_s": round(elapsed, 3),
                "items_per_min": round(items_processed / (elapsed / 60.0), 2)
                if elapsed > 0
                else 0.0,
            }
