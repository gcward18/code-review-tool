"""LangGraph version of the agent team (sketch).

Same behavior as Team.plan + Team.execute, expressed as a graph:

    coordinator -> approve (interrupt) -> specialist x N (Send fan-out) -> aggregator

State holds paths and results only, never file contents: nodes re-read source
through `load_files`, so checkpoints stay source-free like the rest of the app.

Usage:
    graph = build_graph(team, load_files, checkpointer=SqliteSaver(...))
    cfg = {"configurable": {"thread_id": review_id}, "max_concurrency": team.workers}
    first = graph.invoke({"paths": paths}, cfg)             # stops at approval
    proposal = first["__interrupt__"][0].value              # plan, flagged, default
    final = graph.invoke(Command(resume=selection), cfg)    # runs specialists + aggregator
"""
import operator
from dataclasses import asdict
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt  # noqa: F401  (Command used by callers)

from autosde.team import Finding, TeamResult


def merge_findings(old, new):
    """Append new findings; a finding with an existing id replaces it (aggregator verdicts)."""
    by_id = {f["id"]: f for f in old}
    for f in new:
        by_id[f["id"]] = f
    return list(by_id.values())


class ReviewState(TypedDict, total=False):
    paths: list[str]
    plan: dict  # agent -> {"files": [...], "focus": "..."}
    flagged: dict  # path -> reason
    selection: list[str]
    calls: Annotated[list[dict], operator.add]
    findings: Annotated[list[dict], merge_findings]
    limitations: Annotated[list[str], operator.add]
    summary: str


class SpecialistTask(TypedDict):
    agent: str
    files: list[str]
    focus: str


def build_graph(team, load_files, checkpointer):
    """team: an autosde.team.Team. load_files(paths) -> changed-file objects (head, diff, ...)."""

    def coordinator(state: ReviewState):
        result = team.plan(load_files(state["paths"]))
        return {"plan": result.plan, "flagged": result.flagged, "summary": result.summary,
                "calls": [asdict(c) for c in result.calls], "limitations": result.limitations}

    def approve(state: ReviewState):
        planned = [p for item in state["plan"].values() for p in item["files"]]
        default = [p for p in planned if p not in state["flagged"]]
        # Pauses the run and checkpoints it; resumes with Command(resume=selection).
        selection = set(interrupt({"plan": state["plan"], "flagged": state["flagged"], "default": default}))
        skipped = [
            f"{p}: not reviewed ({state['flagged'][p]}; not approved)." if p in state["flagged"]
            else f"{p}: not reviewed (not approved)."
            for p in planned if p not in selection
        ]
        return {"selection": sorted(selection), "limitations": skipped}

    def fan_out(state: ReviewState):
        approved = set(state.get("selection", []))
        sends = [
            Send("specialist", SpecialistTask(agent=agent, files=chosen, focus=item["focus"]))
            for agent, item in state.get("plan", {}).items()
            if (chosen := [p for p in item["files"] if p in approved])
        ]
        return sends or END

    def specialist(task: SpecialistTask):
        by_path = {f.path: f for f in load_files(task["files"])}
        call, findings = team._review(task["agent"], {"files": task["files"], "focus": task["focus"]},
                                      by_path, None)
        for n, finding in enumerate(findings, 1):
            finding.id = f"{call.agent}-{n}"  # one call per agent, so ids stay unique
        failed = [f"{call.agent} specialist failed: {call.error}."] if call.status != "ok" else []
        return {"calls": [asdict(call)], "findings": [asdict(f) for f in findings], "limitations": failed}

    def aggregator(state: ReviewState):
        files = sorted({f["file"] for f in state.get("findings", [])})
        result = TeamResult(findings=[Finding(**f) for f in state.get("findings", [])])
        team._aggregate(result, {f.path: f for f in load_files(files)})
        return {"summary": result.summary, "calls": [asdict(c) for c in result.calls],
                "findings": [asdict(f) for f in result.findings], "limitations": result.limitations}

    graph = StateGraph(ReviewState)
    graph.add_node("coordinator", coordinator)
    graph.add_node("approve", approve)
    graph.add_node("specialist", specialist)
    graph.add_node("aggregator", aggregator)
    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges("coordinator", lambda s: "approve" if s.get("plan") else END)
    graph.add_conditional_edges("approve", fan_out, ["specialist", END])
    graph.add_edge("specialist", "aggregator")  # waits for every Send branch
    graph.add_edge("aggregator", END)
    return graph.compile(checkpointer=checkpointer)
