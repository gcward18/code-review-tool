"""Render stored reviews as text, JSON or SARIF 2.1.0."""

import json
from datetime import datetime

LEVELS = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}


def when(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "-"


def plan_text(result, files, selection, estimate, settings):
    """The approval view: who reviews what, what is flagged, and roughly what it costs."""
    agent_of = {p: agent for agent, item in result.plan.items() for p in item["files"]}
    lines = ["", "Plan:"]
    for f in files:
        if f.skipped:
            lines.append(f"  -   {f.path}  (not sendable: {f.skipped})")
            continue
        mark = "[x]" if f.path in selection else "[ ]"
        why = f"  flagged: {result.flagged[f.path]}" if f.path in result.flagged else ""
        lines.append(f"  {mark} {f.path} -> {agent_of.get(f.path, '?')}{why}")
    tokens_in, tokens_out, agents = estimate
    cost = (tokens_in * settings.input_price + tokens_out * settings.output_price) / 1e6
    lines.append(
        f"Estimate: {agents} specialist(s), ~{tokens_in:,} input tokens, "
        f"up to {tokens_out:,} output tokens, at most ${cost:.4f}."
    )
    return "\n".join(lines)


def text(review):
    lines = [
        f"Review #{review['id']}  {review['project']}  {review['range_spec']}"
        f"  ({review['base_sha'][:7]}..{review['head_sha'][:7]})",
        f"Status: {review['status']}  model: {review['model']}  files: {review['files_changed']}"
        f"  tokens: {review['input_tokens']} in / {review['output_tokens']} out"
        f"  cost: ${review['cost_usd']:.4f}",
    ]
    if review.get("error"):
        lines.append(f"Error: {review['error']}")
    if review.get("summary"):
        lines += ["", review["summary"]]
    reviewed = [f for f in review.get("files", []) if f["agent"]]
    if reviewed:
        lines += ["", "Files:"]
        for f in review["files"]:
            state = ("reviewed" if f["included"] == 1 else "not approved" if f["included"] == 0
                     else "pending" if f["agent"] else f"skipped: {f['skipped']}")
            flag = f"  (flagged: {f['flag']})" if f["flag"] else ""
            lines.append(f"  {f['path']:<40} {state}{flag}")
    lines += ["", "Agents:"]
    for run in review["agent_runs"]:
        detail = f" ({run['error']})" if run["error"] else ""
        files = run["files"].replace("\n", ", ")
        lines.append(
            f"  {run['role']:<11} {run['agent']:<11} {run['status']:<6} {run['seconds']:>5.1f}s"
            f"  {files}{detail}"
        )
    kept = [f for f in review["findings"] if f["kept"]]
    rejected = [f for f in review["findings"] if not f["kept"]]
    lines += ["", f"Findings ({len(kept)}):" if kept else "Findings: none"]
    for f in kept:
        lines += [
            f"  {f['file']}:{f['start_line']}: {f['severity'].upper()} {f['title']}  [{f['agent']}]",
            f"      {f['description']}",
            f"      Fix: {f['recommendation']}",
        ]
    if rejected:
        lines += ["", f"Rejected by the aggregator ({len(rejected)}):"]
        lines += [f"  {f['file']}:{f['start_line']}: {f['title']} ({f['rejection']})" for f in rejected]
    if review.get("limitations"):
        lines += ["", "Limits:"] + [f"  {item}" for item in review["limitations"].splitlines()]
    return "\n".join(lines) + "\n"


def sarif(review):
    kept = [f for f in review["findings"] if f["kept"]]
    rules = sorted({f["category"] for f in kept})
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "AutoSDE",
                        "rules": [{"id": f"autosde/{r}", "name": r.title()} for r in rules],
                    }
                },
                "automationDetails": {"id": f"autosde/review-{review['id']}"},
                "results": [
                    {
                        "ruleId": f"autosde/{f['category']}",
                        "level": LEVELS.get(f["severity"], "warning"),
                        "message": {
                            "text": f"{f['title']}: {f['description']}\n\nFix: {f['recommendation']}"
                        },
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": f["file"]},
                                    "region": {"startLine": f["start_line"], "endLine": f["end_line"]},
                                }
                            }
                        ],
                        "properties": {"agent": f["agent"], "severity": f["severity"],
                                       "confidence": f["confidence"]},
                    }
                    for f in kept
                ],
                "properties": {"base": review["base_sha"], "head": review["head_sha"]},
            }
        ],
    }


def as_json(value):
    return json.dumps(value, indent=2, default=str)
