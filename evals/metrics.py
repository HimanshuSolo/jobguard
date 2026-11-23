"""Eval metrics: summarization and baseline comparison."""

from __future__ import annotations

from typing import Any


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case pass/fail results into a metrics dict."""
    total = len(results)
    passed = sum(1 for r in results if r.get("passed") is True)
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        cat = str(r.get("category", "uncategorized"))
        bucket = by_category.setdefault(cat, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if r.get("passed") is True:
            bucket["passed"] += 1
    categories = {
        cat: {
            "passed": b["passed"],
            "total": b["total"],
            "accuracy": (b["passed"] / b["total"]) if b["total"] else 0.0,
        }
        for cat, b in sorted(by_category.items())
    }
    return {
        "total": total,
        "passed": passed,
        "accuracy": (passed / total) if total else 0.0,
        "categories": categories,
    }


def compare_with_baseline(metrics: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Return human-readable regressions; empty list means no regression.

    Fails CI when overall accuracy drops below ``min_accuracy`` or falls more
    than ``max_regression`` below the committed baseline accuracy. Thresholds
    live in ``baseline.json`` so they are reviewable config, not code.
    """
    regressions: list[str] = []
    thresholds = baseline.get("thresholds", {})
    if not isinstance(thresholds, dict):
        return ["baseline.json has no thresholds object"]
    min_accuracy = float(thresholds.get("min_accuracy", 1.0))
    max_regression = float(thresholds.get("max_regression", 0.0))

    accuracy = float(metrics.get("accuracy", 0.0))
    if accuracy < min_accuracy:
        regressions.append(f"accuracy {accuracy:.3f} below minimum {min_accuracy:.3f}")

    base_metrics = baseline.get("metrics", {})
    if isinstance(base_metrics, dict) and "accuracy" in base_metrics:
        base_accuracy = float(base_metrics["accuracy"])
        if accuracy < base_accuracy - max_regression:
            regressions.append(
                f"accuracy {accuracy:.3f} regressed vs baseline "
                f"{base_accuracy:.3f} (allowed drop {max_regression:.3f})"
            )
    return regressions
