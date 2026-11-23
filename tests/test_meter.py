"""Tests for the per-run LLM meter (no network, no sleeping, fake clock-free)."""

from __future__ import annotations

import threading
from typing import cast

from jobguard.agent.meter import RunMeter, estimate_tokens


def test_estimate_tokens_fallback() -> None:
    assert estimate_tokens("s" * 100, "u" * 100, "t" * 100) == 75


def test_meter_summary_math() -> None:
    meter = RunMeter()
    meter.record_call(
        "classify_extract",
        system="s" * 100,
        user="u" * 100,
        text="t" * 100,
        input_tokens=0,
        output_tokens=0,
        latency_s=0.4,
    )
    meter.record_call(
        "match_suggest",
        system="s",
        user="u",
        text="t",
        input_tokens=200,
        output_tokens=50,
        latency_s=0.6,
    )
    meter.record_hit("match_suggest")
    meter.record_429()
    meter.record_sleep(1.5)
    meter.finish()
    summary = meter.summary(2)
    assert summary["items"] == 2
    assert summary["calls"] == 2
    assert summary["calls_per_posting"] == 1.0
    assert summary["cache_hits"] == 1
    assert summary["cache_hit_rate"] == 1 / 3
    assert summary["tokens_in"] == 75 + 200
    assert summary["tokens_out"] == 50
    assert summary["tokens_in_per_stage"] == {"classify_extract": 75, "match_suggest": 200}
    assert summary["tokens_out_per_stage"] == {"classify_extract": 0, "match_suggest": 50}
    assert summary["latency_p50_s"] == 0.5
    assert summary["latency_p95_s"] == 0.59
    assert summary["sleep_s"] == 1.5
    assert summary["network_s"] == 1.0
    assert cast(float, summary["local_s"]) >= 0.0
    assert summary["errors_429"] == 1
    assert cast(float, summary["items_per_min"]) >= 0.0


def test_meter_empty_run() -> None:
    summary = RunMeter().summary(0)
    assert summary["calls"] == 0
    assert summary["calls_per_posting"] == 0.0
    assert summary["cache_hit_rate"] == 0.0
    assert summary["latency_p50_s"] == 0.0


def test_meter_thread_safe() -> None:
    meter = RunMeter()

    def work() -> None:
        for _ in range(100):
            meter.record_call(
                "s",
                system="x",
                user="y",
                text="z",
                input_tokens=1,
                output_tokens=1,
                latency_s=0.001,
            )
            meter.record_hit("s")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    summary = meter.summary(400)
    assert summary["calls"] == 400
    assert summary["cache_hits"] == 400
    assert summary["tokens_in"] == 400
