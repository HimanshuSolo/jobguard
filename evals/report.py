"""Eval reporting: Markdown table (PR comments) and HTML page (GitHub Pages)."""

from __future__ import annotations

import html
from typing import Any


def to_markdown(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    """Render results as a Markdown table plus a summary line."""
    lines = [
        "# JobGuard eval report",
        "",
        f"Accuracy: **{metrics.get('passed', 0)}/{metrics.get('total', 0)} "
        f"({float(metrics.get('accuracy', 0.0)):.1%})**",
        "",
        "| id | category | task | result | detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        mark = "PASS" if r.get("passed") is True else "FAIL"
        lines.append(
            f"| {r.get('id', '?')} | {r.get('category', '?')} | "
            f"{r.get('task', '?')} | {mark} | {r.get('detail', '')} |"
        )
    return "\n".join(lines) + "\n"


def to_html(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    """Render results as a minimal standalone HTML page."""
    rows = []
    for r in results:
        mark = "PASS" if r.get("passed") is True else "FAIL"
        cls = "pass" if r.get("passed") is True else "fail"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('id', '?')))}</td>"
            f"<td>{html.escape(str(r.get('category', '?')))}</td>"
            f"<td>{html.escape(str(r.get('task', '?')))}</td>"
            f'<td class="{cls}">{mark}</td>'
            f"<td>{html.escape(str(r.get('detail', '')))}</td>"
            "</tr>"
        )
    accuracy = float(metrics.get("accuracy", 0.0))
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>JobGuard eval report</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #999; padding: 0.4rem 0.8rem; }}
.pass {{ color: #0a0; font-weight: bold; }} .fail {{ color: #c00; font-weight: bold; }}
</style></head>
<body>
<h1>JobGuard eval report</h1>
<p>Accuracy: <strong>{metrics.get("passed", 0)}/{metrics.get("total", 0)}
({accuracy:.1%})</strong></p>
<table><tr><th>id</th><th>category</th><th>task</th><th>result</th><th>detail</th></tr>
{"".join(rows)}
</table>
</body></html>
"""
