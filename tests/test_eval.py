"""Tests for the eval runner: green run, regression detection, report output."""

from __future__ import annotations

import json
from pathlib import Path

from evals.metrics import compare_with_baseline, summarize
from evals.report import to_html, to_markdown
from evals.runner import run_case, run_evals


def test_all_cases_pass() -> None:
    metrics = run_evals(
        cases_dir="data/evals",
        baseline_path="evals/baseline.json",
        out_path="evals/metrics.json",
        owner_email="owner@example.com",
    )
    assert metrics["total"] == 67
    assert metrics["passed"] == 67
    assert metrics["accuracy"] == 1.0
    assert metrics["regressions"] == []


def test_unknown_task_fails_case_not_run() -> None:
    result = run_case({"id": "x", "category": "c", "task": "nope"}, "owner@example.com")
    assert result["passed"] is False


def test_regression_detected_against_stricter_baseline(tmp_path: Path) -> None:
    metrics = {
        "accuracy": 0.9,
        "total": 10,
        "passed": 9,
        "categories": {},
    }
    baseline = {
        "thresholds": {"min_accuracy": 1.0, "max_regression": 0.0},
        "metrics": {"accuracy": 1.0},
    }
    regressions = compare_with_baseline(metrics, baseline)
    assert len(regressions) == 2  # below min AND below baseline


def test_summarize_per_category() -> None:
    metrics = summarize(
        [
            {"id": "a", "category": "policy", "passed": True},
            {"id": "b", "category": "policy", "passed": False},
        ]
    )
    assert metrics["accuracy"] == 0.5
    assert metrics["categories"]["policy"] == {
        "passed": 1,
        "total": 2,
        "accuracy": 0.5,
    }
    assert metrics["pipeline_calls_per_posting"] is None


def test_summarize_pipeline_calls_per_posting() -> None:
    metrics = summarize(
        [
            {
                "id": "pipe-01",
                "category": "pipeline",
                "task": "pipeline_smoke",
                "passed": True,
                "llm_calls": 2,
            },
            {
                "id": "pipe-02",
                "category": "pipeline",
                "task": "pipeline_smoke",
                "passed": True,
                "llm_calls": 1,
            },
            {
                "id": "pol-01",
                "category": "policy",
                "task": "policy_decision",
                "passed": True,
            },
        ]
    )
    assert metrics["pipeline_calls_per_posting"] == 1.5


def test_calls_per_posting_gate() -> None:
    baseline = {
        "thresholds": {
            "min_accuracy": 0.0,
            "max_regression": 1.0,
            "max_calls_per_posting": 2.0,
        },
        "metrics": {"accuracy": 1.0},
    }
    over = {"accuracy": 1.0, "pipeline_calls_per_posting": 2.5}
    assert any("calls/posting" in reg for reg in compare_with_baseline(over, baseline))
    under = {"accuracy": 1.0, "pipeline_calls_per_posting": 1.5}
    assert compare_with_baseline(under, baseline) == []
    assert compare_with_baseline({"accuracy": 1.0}, baseline) == []
    # The 2.0 default applies when the threshold key is absent.
    no_key = {
        "thresholds": {"min_accuracy": 0.0, "max_regression": 1.0},
        "metrics": {"accuracy": 1.0},
    }
    assert compare_with_baseline(over, no_key) != []


def test_reports_render() -> None:
    results = [
        {
            "id": "pol-01",
            "category": "policy",
            "task": "policy_decision",
            "passed": True,
            "detail": "ok",
        }
    ]
    metrics = {"passed": 1, "total": 1, "accuracy": 1.0}
    md = to_markdown(results, metrics)
    assert "| pol-01 | policy | policy_decision | PASS | ok |" in md
    page = to_html(results, metrics)
    assert "<table>" in page and "pol-01" in page
    _ = json.dumps(metrics)  # metrics stay JSON-serializable


def test_eval_reports_and_live_smoke(tmp_path: Path) -> None:
    from jobguard.cli import main

    html_out = tmp_path / "report.html"
    md_out = tmp_path / "report.md"
    assert (
        main(
            [
                "eval",
                "--out",
                str(tmp_path / "metrics.json"),
                "--report-html",
                str(html_out),
                "--report-md",
                str(md_out),
                "--live-smoke",
            ]
        )
        == 0
    )
    assert "<table>" in html_out.read_text(encoding="utf-8")
    assert "| PASS |" in md_out.read_text(encoding="utf-8")


def test_workflow_and_render_yaml_parse() -> None:
    import yaml

    for name in ("ci.yml", "worker.yml", "eval-nightly.yml", "pages.yml"):
        path = Path(".github") / "workflows" / name
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "jobs" in doc, name
    render = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    assert render["services"][0]["type"] == "web"


def test_metrics_json_written_and_serializable(tmp_path: Path) -> None:
    out = tmp_path / "metrics.json"
    run_evals(
        cases_dir="data/evals",
        baseline_path="evals/baseline.json",
        out_path=str(out),
        owner_email="owner@example.com",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["accuracy"] == 1.0
    assert payload["regressions"] == []
