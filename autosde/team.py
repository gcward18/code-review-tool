"""Coordinator -> parallel specialists -> aggregator, over one committed range.

Both steps are LangGraph graphs: planning is a one-node graph (the coordinator), and the
approved review fans out to the specialists with `Send`, collects their findings and runs the
aggregator. Each specialist call is retried up to that specialist's `retries` setting. The
aggregator sees the code at each finding; when it returns CONTINUE, the graph loops back to the
specialists it names, to answer a question, restart their work or retry after a failure, and
the aggregator may refuse the edits a follow-up made. Reviews stop after MAX_ROUNDS
aggregation rounds. Planning and review stay separate graphs because approval happens between them, possibly
in another process; no checkpointer is needed since the plan is saved with the review.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langsmith import tracing_context

from autosde import agents
from autosde.agents import FRAMEWORKS, SPECIALISTS, load_config, retries, specialists
from autosde.knowledge import context_block
from autosde.noise import flag

PROMPTS = Path(__file__).parent / "prompts"
AGENTS = tuple(SPECIALISTS)
SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("correctness", "security", "reliability", "performance", "maintainability")
# Aggregation rounds per review: the first, plus up to two follow-up rounds when the aggregator
# asks. Estimates multiply by this and by each specialist's attempts, so the approved "at most"
# cost covers every retry and follow-up.
MAX_ROUNDS = 3
ACTIONS = ("question", "restart", "retry")
# Unchanged-file reads per review, and lines per read. A specialist gets one context round per
# task: it asks, the ranges are read and scanned, and its next reply is final.
MAX_CONTEXT_READS, MAX_CONTEXT_LINES = 10, 200
# Input tokens allowed for one aggregator call (findings, their code and changed lines), and the
# share of the review budget held back so the aggregator can always judge what was found.
AGGREGATOR_INPUT, AGGREGATOR_OUTPUT = 6000, 1500
BUDGET_REACHED = "review budget reached; call skipped"
# Progress events per review: metadata only (agents, paths, counts), never code or prompts.
MAX_PROGRESS = 200


def _names(paths, shown=3):
    """A short list of paths for progress lines: "a.py, b.py and 2 more"."""
    paths = list(paths)
    head = ", ".join(paths[:shown])
    return head + (f" and {len(paths) - shown} more" if len(paths) > shown else "")


@dataclass(frozen=True)
class Budget:
    """The approved "at most" for one review: tokens and USD at per-million-token prices."""

    input_tokens: int
    output_tokens: int
    input_price: float
    output_price: float

    def cost(self, tokens_in, tokens_out):
        return (tokens_in * self.input_price + tokens_out * self.output_price) / 1_000_000

    @property
    def max_cost(self):
        return self.cost(self.input_tokens, self.output_tokens)

# Strict structured-output schemas: every property required, no extras.
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignments", "flagged", "rationale"],
    "properties": {
        "flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "reason"],
                "properties": {"file": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["agent", "files", "focus"],
                "properties": {
                    "agent": {"type": "string", "enum": list(AGENTS)},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "focus": {"type": "string"},
                },
            },
        },
        "rationale": {"type": "string"},
    },
}
FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "requested_context", "summary"],
    "properties": {
        "requested_context": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "start_line", "end_line", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "file",
                    "start_line",
                    "end_line",
                    "severity",
                    "category",
                    "title",
                    "description",
                    "recommendation",
                    "confidence",
                ],
                "properties": {
                    "file": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "rejected", "refused", "follow_ups", "requested_context", "summary"],
    "properties": {
        "requested_context": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "start_line", "end_line", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["COMPLETE", "CONTINUE"]},
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "agent", "files", "question"],
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "agent": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "question": {"type": "string"},
                },
            },
        },
        "refused": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["agent", "reason"],
                "properties": {"agent": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "reason"],
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "summary": {"type": "string"},
    },
}


def prompt(name, **values):
    """Prompt files are re-read on every call, so edits apply immediately."""
    text = agents.read_text(name)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


_ANGULAR = re.compile(r"""from\s+['"]@angular/|@(?:Component|Injectable|NgModule|Directive|Pipe)\s*\(""")
_REACT = re.compile(r"""from\s+['"](?:react|react-dom)(?:/[^'"]*)?['"]|require\(\s*['"]react['"]\s*\)""")


def frameworks(path, head):
    """Framework specialists for a file, from its extension and imports (not exclusive)."""
    lower, found = path.lower(), []
    if lower.endswith(".ts") and _ANGULAR.search(head):
        found.append("angular")
    if lower.endswith((".jsx", ".tsx")) or (lower.endswith((".js", ".ts")) and _REACT.search(head)):
        found.append("react")
    return found


def owners_of(plan):
    """path -> its specialists in plan order, e.g. {"app.ts": ["typescript", "angular"]}."""
    owners = {}
    for agent, item in plan.items():
        for path in item["files"]:
            owners.setdefault(path, []).append(agent)
    return owners


def route(path, table=None):
    """Pick the enabled specialist whose extensions match; `general` takes the rest."""
    table = specialists(load_config()) if table is None else table
    lower = path.lower()
    return next((a for a, ext in table.items() if ext and lower.endswith(tuple(ext))), "general")


def plan_schema(agents):
    schema = json.loads(json.dumps(PLAN_SCHEMA))
    item = schema["properties"]["assignments"]["items"]["properties"]["agent"]
    item["enum"] = list(agents)
    return schema


def changed_lines(diff):
    """Head-side line ranges a diff adds or modifies, e.g. [[11, 17]]."""
    ranges, line = [], 0
    for text in diff.splitlines():
        if hunk := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", text):
            line = int(hunk[1])
        elif text.startswith("+") and not text.startswith("+++"):
            if ranges and ranges[-1][1] == line - 1:
                ranges[-1][1] = line
            else:
                ranges.append([line, line])
            line += 1
        elif text.startswith(" "):
            line += 1
    return ranges


def numbered(text):
    return "\n".join(f"{n:5}| {line}" for n, line in enumerate(text.splitlines(), 1))


def excerpt(text, start, end, margin=10, limit=40):
    """Numbered head lines around a finding (`margin` either side, at most `limit` lines)."""
    lines = text.splitlines()
    first = max(1, start - margin)
    last = min(len(lines), end + margin, first + limit - 1)
    return "\n".join(f"{n:5}| {lines[n - 1]}" for n in range(first, last + 1))


def parse_plan(data, table, paths):
    """The coordinator's plan as (assignments, flagged), or None when it breaks the contract.

    Assignments are (agent, files, focus) for enabled specialists and changed paths only; each
    path goes to its first assignment. Flags name changed paths."""
    try:
        if not isinstance(data["rationale"], str):
            return None
        flagged = {}
        for item in data["flagged"]:
            if not isinstance(item["file"], str) or not isinstance(item["reason"], str):
                return None
            if item["file"] in paths:
                flagged[item["file"]] = item["reason"].strip()
        assignments, assigned = [], set()
        for item in data["assignments"]:
            agent, files, focus = item["agent"], item["files"], item["focus"]
            if not isinstance(agent, str) or not isinstance(focus, str) or not isinstance(files, list):
                return None
            if not all(isinstance(path, str) for path in files):
                return None
            chosen = [path for path in files if agent in table and path in paths
                      and path not in assigned]
            assigned.update(chosen)
            if chosen:
                assignments.append((agent, chosen, focus))
        return assignments, flagged
    except (KeyError, TypeError):
        return None


def parse_verdict(data, candidates, plan, *, edited=(), failed=()):
    """The aggregator's verdict as (rejected, refused, follow-ups, summary), or None.

    `rejected` and `refused` map finding ids and agents to reasons. Refusals name an agent whose
    follow-up edit is in `edited`. Follow-ups are (action, agent, files, question): `question` and
    `restart` name a planned specialist and files it reviewed, `retry` a specialist in `failed`.
    CONTINUE with no usable follow-up, or an unknown verdict, makes the whole verdict invalid."""
    try:
        if data["verdict"] not in ("COMPLETE", "CONTINUE"):
            return None
        refused = {item["agent"]: str(item.get("reason", "")) for item in data["refused"]
                   if item["agent"] in edited}
        ids = {f.id for f in candidates}
        rejected = {item["id"]: str(item.get("reason", "")) for item in data["rejected"]
                    if item["id"] in ids}
        follow_ups = []
        for item in data["follow_ups"] if data["verdict"] == "CONTINUE" else []:
            action, agent = item["action"], item["agent"]
            owned = plan.get(agent, {}).get("files", [])
            files = [path for path in item["files"] if path in owned] or (
                list(owned) if action == "retry" else [])
            question = str(item["question"]).strip()
            if action == "retry" and agent not in failed:
                continue  # only a specialist that failed can be retried
            if action in ACTIONS and files and (question or action != "question"):
                follow_ups.append((action, agent, files, question))
        if data["verdict"] == "CONTINUE" and not follow_ups:
            return None
        return rejected, refused, follow_ups, str(data["summary"])
    except (KeyError, TypeError, AttributeError):
        return None


def worst_case(per_agent):
    """(input, output) token bound for a review, from (prompt tokens, attempts) per specialist:
    every round can run each specialist with all of its retries, then the aggregator."""
    # Each task can take a second call with requested context (MAX_CONTEXT_LINES lines per read,
    # about 12 characters a line), so attempts count twice.
    context = MAX_CONTEXT_READS * MAX_CONTEXT_LINES * 12 // 4 if per_agent else 0
    tokens_in = sum(tokens * attempts * 2 for tokens, attempts in per_agent) * MAX_ROUNDS + context
    # The aggregator may also take a second call per round, after reading code it asked for.
    tokens_in += (2 * AGGREGATOR_INPUT * MAX_ROUNDS) if per_agent else 0
    tokens_out = (sum(4000 * attempts * 2 for _, attempts in per_agent)
                  + (2 * AGGREGATOR_OUTPUT if per_agent else 0)) * MAX_ROUNDS
    return tokens_in, tokens_out


def _outcomes(old, new):
    """Fan-in for specialist branches; None clears it for the next round."""
    return [] if new is None else [*(old or []), *new]


class ContextUnavailable(Exception):
    """A requested range cannot be sent; its message is a short, safe reason."""


@dataclass
class Call:
    """One model call by one role; stored as an agent run."""

    role: str  # coordinator | specialist | aggregator
    agent: str  # coordinator | python | ... | aggregator
    files: list[str]
    status: str = "ok"  # ok | failed
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    focus: str = ""
    model: str | None = None  # the model this call used (override or default)
    stored: bool = False  # already written to the database (planning step)


@dataclass
class Finding:
    id: str
    agent: str
    file: str
    start_line: int
    end_line: int
    severity: str
    category: str
    title: str
    description: str
    recommendation: str
    confidence: float
    kept: bool = True
    rejection: str | None = None


@dataclass
class TeamResult:
    calls: list[Call] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    plan: dict = field(default_factory=dict)  # agent -> {"files": [...], "focus": "..."}
    flagged: dict = field(default_factory=dict)  # path -> why it is probably not worth tokens
    approved: set = field(default_factory=set)  # paths the specialists actually reviewed
    summary: str = ""
    limitations: list[str] = field(default_factory=list)

    def files(self):
        """Planned paths, once each, even when two specialists review a file."""
        return list(dict.fromkeys(path for item in self.plan.values() for path in item["files"]))

    def default_selection(self):
        """Proposed approval: every planned file except flagged ones."""
        return {path for path in self.files() if path not in self.flagged}

    @property
    def kept(self):
        return [f for f in self.findings if f.kept]


class PlanState(TypedDict):
    files: list
    result: Any  # TeamResult, filled in place


class ReviewState(TypedDict):
    by_path: dict
    result: Any
    work: list  # [(agent, assignment)] for this round, on approved paths
    outcomes: Annotated[list, _outcomes]  # [(index, calls, findings)] from this round's specialists
    round: int
    edits: dict  # agent -> {"before": [ids], "after": [ids]} from this round's follow-ups
    failed: dict  # agent -> files, for specialists whose every attempt failed


class Team:
    def __init__(self, model, *, workers=4, config=None, retriever=None, reader=None,
                 progress=None):
        # retriever(kb_ids, query) -> ranked chunks; None disables knowledge bases.
        # reader(path, start, end) -> numbered, scanned text or raises ContextUnavailable;
        # None means specialists cannot read unchanged files.
        self.model, self.workers, self.retriever, self.reader = model, workers, retriever, reader
        self._reads, self._reads_lock = 0, threading.Lock()
        # progress(agent, text) reports what the team is doing; it never stops a review.
        self.progress, self._notes = progress, 0
        # Set by execute(): calls stop once the approved budget would be exceeded.
        self.budget, self._spent, self._held, self._skipped = None, [0, 0], [0, 0], 0
        self._fixed = config  # tests pass one; otherwise agents.json is re-read per review
        self.config = config or load_config()
        self.planner, self.reviewer = self._plan_graph(), self._review_graph()

    def _plan_graph(self):
        def coordinator(state):
            self._plan(state["files"], state["result"])
            return {}

        graph = StateGraph(PlanState)
        graph.add_node("coordinator", coordinator)
        graph.add_edge(START, "coordinator")
        graph.add_edge("coordinator", END)
        return graph.compile()

    def _review_graph(self):
        def fan_out(state):
            if not state["work"]:
                return END
            return [Send("specialist", {**state, "work": [(n, agent, item)]})
                    for n, (agent, item) in enumerate(state["work"])]

        def specialist(state):
            [(n, agent, item)] = state["work"]
            return {"outcomes": [(n, *self._review(agent, item, state["by_path"], state["result"]))]}

        def collect(state):
            result, edits, failed = state["result"], {}, dict(state.get("failed") or {})
            for n, calls, findings in sorted(state["outcomes"], key=lambda outcome: outcome[0]):
                result.calls.extend(calls)
                last = calls[-1]
                if last.status != "ok":
                    tries = f" after {len(calls)} attempts" if len(calls) > 1 else ""
                    result.limitations.append(f"{last.agent} specialist failed{tries}: {last.error}.")
                    failed[last.agent] = list(last.files)
                    continue
                failed.pop(last.agent, None)
                before = []
                if state["round"] > 1:
                    # A follow-up answer is the specialist's complete set for those files.
                    for earlier in result.kept:
                        if earlier.agent == last.agent and earlier.file in last.files:
                            earlier.kept, earlier.rejection = False, "replaced after follow-up"
                            before.append(earlier.id)
                numbered_ids = sum(1 for f in result.findings if f.agent == last.agent)
                after = []
                for i, finding in enumerate(findings, numbered_ids + 1):
                    finding.id = f"{last.agent}-{i}"
                    result.findings.append(finding)
                    after.append(finding.id)
                if state["round"] > 1:
                    edits[last.agent] = {"before": before, "after": after}
            return {"outcomes": None, "edits": edits, "failed": failed}

        def aggregator(state):
            follow_ups = self._aggregate(state["result"], state["by_path"], state["round"],
                                         edits=state.get("edits") or {},
                                         failed=state.get("failed") or {})
            if not follow_ups:
                return {"work": []}
            if state["round"] >= MAX_ROUNDS:
                state["result"].limitations.append(
                    f"Review stopped after {MAX_ROUNDS} aggregation rounds; follow-ups were not run."
                )
                return {"work": []}
            plan = state["result"].plan
            work = [
                (agent, {"files": files, "focus": plan[agent]["focus"], "action": action,
                         "question": question})
                for action, agent, files, question in follow_ups
            ]
            return {"work": work, "round": state["round"] + 1}

        graph = StateGraph(ReviewState)
        graph.add_node("specialist", specialist)
        graph.add_node("collect", collect)
        graph.add_node("aggregator", aggregator)
        graph.add_conditional_edges(START, fan_out, ["specialist", END])
        graph.add_edge("specialist", "collect")
        graph.add_edge("collect", "aggregator")
        graph.add_conditional_edges("aggregator", fan_out, ["specialist", END])
        return graph.compile()

    def _invoke(self, graph, state):
        # Agent calls are traced by autosde.tracing (which withholds what gitleaks flags), so
        # LangChain's own tracer stays off even if LANGSMITH_TRACING is set in the environment.
        with tracing_context(enabled=False):
            return graph.invoke(state, {"max_concurrency": self.workers})

    def _note(self, agent, text):
        if self.progress is None:
            return
        with self._reads_lock:
            if self._notes >= MAX_PROGRESS:
                return
            self._notes += 1
        try:
            self.progress(agent, text)
        except Exception:
            pass  # progress is best effort

    def _reserve(self, call, tokens_in, tokens_out):
        """Hold this call's worst case against the budget, or refuse it. Specialists may not
        use the aggregator's share, so the findings can still be judged."""
        if self.budget is None:
            return True
        keep = (0, 0) if call.role == "aggregator" else (2 * AGGREGATOR_INPUT, 2 * AGGREGATOR_OUTPUT)
        with self._reads_lock:
            would_in = self._spent[0] + self._held[0] + tokens_in + keep[0]
            would_out = self._spent[1] + self._held[1] + tokens_out + keep[1]
            if (would_in > self.budget.input_tokens or would_out > self.budget.output_tokens
                    or self.budget.cost(would_in, would_out) > self.budget.max_cost):
                self._skipped += 1
                return False
            self._held[0] += tokens_in
            self._held[1] += tokens_out
            return True

    def _settle(self, held, reply):
        """Replace the hold with reported usage. A failed call may still have been billed
        without reporting it, so it keeps its whole hold as spent."""
        if self.budget is not None:
            used = (reply.input_tokens, reply.output_tokens) if reply.ok else held
            with self._reads_lock:
                self._held[0] -= held[0]
                self._held[1] -= held[1]
                self._spent[0] += max(used[0], 0)
                self._spent[1] += max(used[1], 0)

    def _ask(self, call, system, text, schema, limit):
        override = self.config["agents"].get(call.agent, {}).get("model")
        call.model = override or getattr(self.model, "model", None)
        held = ((len(system) + len(text)) // 3 + 200, limit)  # generous: ~3 characters a token
        if not self._reserve(call, *held):
            call.status, call.error = "failed", BUDGET_REACHED
            self._note(call.agent, "Skipped: the review budget is used up")
            return None
        reply = self.model.complete(
            role=call.agent, system=system, prompt=text, schema=schema, max_output_tokens=limit,
            model=override,
        )
        self._settle(held, reply)
        call.input_tokens, call.output_tokens = reply.input_tokens, reply.output_tokens
        call.seconds = reply.seconds
        if not reply.ok:
            call.status, call.error = "failed", reply.error
            return None
        try:
            return json.loads(reply.text)
        except ValueError:
            call.status, call.error = "failed", "reply was not JSON"
            return None

    def _plan(self, files, result):
        self.config = self._fixed or load_config()
        table = specialists(self.config)
        call = Call(role="coordinator", agent="coordinator", files=[f.path for f in files])
        result.calls.append(call)
        # Free rules first; the coordinator sees them and may flag more.
        result.flagged = {f.path: reason for f in files if (reason := flag(f.path))}
        listing = [
            {"path": f.path, "status": f.status, "lines": max(1, len(f.head.splitlines())),
             "changed_lines": sum(1 for l in f.diff.splitlines() if l[:1] in "+-"),
             "rule_flag": result.flagged.get(f.path)}
            for f in files
        ]
        self._note("coordinator", f"Planning {len(files)} changed file(s)")
        data = self._ask(call, prompt("coordinator.md"), json.dumps({"changed_files": listing}),
                         plan_schema(table), 1500)
        paths = {f.path for f in files}
        parsed = None if data is None else parse_plan(data, table, paths)
        if data is not None and parsed is None:
            call.status, call.error = "failed", "plan did not match the contract"
            data = None  # treated as unavailable: every file is routed by type
        assignments, flags = parsed or ([], {})
        for path, reason in flags.items():
            if path not in result.flagged:
                result.flagged[path] = f"coordinator: {reason}".rstrip(": ")
        plan, assigned = {}, set()
        for agent, chosen, focus in assignments:
            plan.setdefault(agent, {"files": [], "focus": focus})["files"].extend(chosen)
            assigned.update(chosen)
        missing = [f.path for f in files if f.path not in assigned]
        if data is None:
            result.limitations.append("Coordinator unavailable; files were routed by type.")
        elif missing:
            result.limitations.append(f"Coordinator left {len(missing)} file(s) unassigned; routed by type.")
        for path in missing:
            plan.setdefault(route(path, table), {"files": [], "focus": ""})["files"].append(path)
        # Framework files get their framework specialist too, and a file the coordinator gave only
        # to a framework specialist keeps its language specialist.
        owners = owners_of(plan)
        for f in files:
            mine = owners.get(f.path, [])
            if mine and all(agent in FRAMEWORKS for agent in mine):
                plan.setdefault(route(f.path, table), {"files": [], "focus": ""})["files"].append(f.path)
            for agent in frameworks(f.path, f.head):
                if agent in table and agent not in mine:
                    plan.setdefault(agent, {"files": [], "focus": ""})["files"].append(f.path)
        result.plan = plan
        self._note("coordinator", f"Planned {len(owners_of(plan))} file(s) for {len(plan)} specialist(s)"
                   + (" (routed by type)" if data is None else ""))
        return plan

    def _reference(self, agent, files):
        """Knowledge base context for an agent: searched with its files' changed lines."""
        kb_ids = self.config["agents"].get(agent, {}).get("knowledge") or []
        if not kb_ids or self.retriever is None:
            return ""
        changed = "\n".join(line[1:] for f in files for line in f.diff.splitlines()
                            if line[:1] in "+-" and not line.startswith(("+++", "---")))
        query = " ".join(f.path for f in files) + "\n" + changed[:4000]
        started = time.time()
        try:
            results = self.retriever(kb_ids, query)
        except Exception:
            return ""  # retrieval never blocks a review
        hook = getattr(self.model, "trace_retrieval", None)  # LangSmith, when tracing is on
        if callable(hook):
            hook(agent, query, results, started, time.time())
        return context_block(results)

    def _review(self, agent, assignment, by_path, result):
        """One specialist task, retried up to its `retries` setting; returns (calls, findings)."""
        files = [by_path[p] for p in assignment["files"]]
        action, question = assignment.get("action"), assignment.get("question") or ""
        label = {"question": f"Follow-up: {question}", "restart": "Restart",
                 "retry": "Retry after failure"}.get(action)
        if action == "restart" and question:
            label += f": {question}"
        focus = " ".join(
            part for part in (assignment["focus"], agents.read_text(f"focus/{agent}.md").strip()) if part
        )
        body = "\n\n".join(
            f"=== {f.path} (head) ===\n{numbered(f.head)}\n=== {f.path} (diff) ===\n{f.diff}"
            for f in files
        )
        reference = self._reference(agent, files)
        if reference:
            body = reference + "\n\n" + body
        if action == "question":
            earlier = [
                {k: getattr(f, k) for k in ("id", "file", "start_line", "end_line", "title")}
                for f in result.kept if f.agent == agent and f.file in assignment["files"]
            ]
            body += (
                "\n\n=== Follow-up from the aggregator ===\n" + question
                + "\nYour current findings on these files: " + json.dumps(earlier)
                + "\nAnswer by returning your complete findings for these files: keep, correct"
                " or drop each one, and add any the question uncovers. Findings you leave out"
                " are withdrawn."
            )
        elif action == "restart":
            body += (
                "\n\n=== The aggregator asked you to review these files again from the start ===\n"
                + (question + "\n" if question else "")
                + "Your earlier findings on them are discarded; report your complete findings."
            )
        system = prompt("specialist.md", agent=agent, focus=focus or "general correctness")
        calls = []
        findings, requests = self._attempts(agent, system, body, files, by_path,
                                            label or assignment["focus"], calls)
        if findings is None:
            return calls, []
        context = self._context(agent, requests, by_path, result) if requests else ""
        if not context:
            return calls, findings
        body += "\n\n=== Requested context (unchanged files at the head commit) ===\n" + context + (
            "\nThis is your final reply: report your complete findings for your files; "
            "do not request more context.")
        final, _ = self._attempts(agent, system, body, files, by_path,
                                  (label or assignment["focus"] or "Review") + " (with context)", calls)
        return calls, findings if final is None else final

    def _attempts(self, agent, system, body, files, by_path, focus, calls):
        """One call, retried up to the specialist's limit: (findings, requests) or (None, [])."""
        tries = retries(self.config, agent) + 1
        for attempt in range(1, tries + 1):
            call = Call(role="specialist", agent=agent, files=[f.path for f in files], focus=focus)
            calls.append(call)
            note = f" (attempt {attempt} of {tries})" if attempt > 1 else ""
            self._note(agent, f"{focus or 'Reviewing'}: {_names(call.files)}{note}")
            data = self._ask(call, system, body, FINDINGS_SCHEMA, 4000)
            findings = self._findings(agent, data, by_path, call) if data is not None else None
            if findings is not None:
                requests = data.get("requested_context") if isinstance(data, dict) else None
                requests = requests if isinstance(requests, list) else []
                asked = f", asked for {len(requests)} code range(s)" if requests else ""
                self._note(agent, f"Done: {len(findings)} finding(s){asked}")
                return findings, requests
            if call.error != BUDGET_REACHED:
                self._note(agent, f"Failed: {call.error or 'reply did not match the findings contract'}")
            if call.error == BUDGET_REACHED:
                break  # retrying cannot fit either
            if call.status == "ok":
                call.status, call.error = "failed", "reply did not match the findings contract"
        return None, []

    def _context(self, agent, requests, by_path, result, *, changed=False):
        """Read requested ranges under the review's quota; returns the prompt text. With
        `changed`, approved changed files are served from what was already sent (no quota)."""
        if self.reader is None and not changed:
            return ""
        parts = []
        for item in requests:
            try:
                path, start, end = str(item["path"]), int(item["start_line"]), int(item["end_line"])
            except (KeyError, TypeError, ValueError):
                continue
            start, end = max(1, start), max(1, end)
            end = min(max(start, end), start + MAX_CONTEXT_LINES - 1)
            label = f"[{path} lines {start}-{end}]"
            if changed and path in by_path and path in result.approved:
                parts.append(f"{label}\n{excerpt(by_path[path].head, start, end, margin=0, limit=MAX_CONTEXT_LINES)}")
                continue
            if path in by_path or path in result.files() or path in result.flagged:
                parts.append(f"{label} not available: it is a changed file, sent only if approved")
                continue
            if self.reader is None:
                parts.append(f"{label} not available: unchanged files cannot be read here")
                continue
            with self._reads_lock:
                if self._reads >= MAX_CONTEXT_READS:
                    parts.append(f"{label} not available: the review's context quota is used up")
                    continue
                self._reads += 1
            try:
                text = self.reader(path, start, end)
            except ContextUnavailable as reason:
                parts.append(f"{label} not available: {reason}")
                continue
            parts.append(f"{label}\n{text}")
            self._note(agent, f"Read {path} lines {start}-{end}")
            with self._reads_lock:
                result.limitations.append(f"Context: {agent} read {path} lines {start}-{end}.")
        return "\n\n".join(parts)

    def _findings(self, agent, data, by_path, call):
        """The specialist's findings on its own files, or None when the reply breaks the schema."""
        try:
            findings = []
            for item in data["findings"]:
                source = by_path.get(item.get("file"))
                if source is None or source.path not in call.files:
                    continue  # a specialist may only report on its own files
                lines = max(1, len(source.head.splitlines()))
                start = max(1, min(int(item["start_line"]), lines))
                end = max(start, min(int(item["end_line"]), lines))
                findings.append(Finding(
                    id="", agent=agent, file=source.path, start_line=start, end_line=end,
                    severity=item["severity"], category=item["category"], title=item["title"],
                    description=item["description"], recommendation=item["recommendation"],
                    confidence=max(0.0, min(1.0, float(item["confidence"]))),
                ))
            return findings
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def _aggregate(self, result, by_path=None, round_number=1, *, edits=None, failed=None):
        """Judge the kept findings against their code, and this round's follow-up edits; returns
        follow-ups to run, if any. Runs without findings too when a specialist failed, so the
        aggregator can retry it."""
        by_path, edits, failed = by_path or {}, edits or {}, failed or {}
        candidates = result.kept
        if not candidates and not (failed and round_number < MAX_ROUNDS):
            result.summary = (
                "No findings were reported." if round_number == 1
                else result.summary or "No findings remain after the follow-up."
            )
            return []
        by_id = {f.id: f for f in result.findings}
        replaced = [by_id[i] for edit in edits.values() for i in edit["before"]]
        call = Call(role="aggregator", agent="aggregator",
                    files=sorted({f.file for f in candidates + replaced} | {p for fs in failed.values() for p in fs}))
        result.calls.append(call)

        def entry(f):
            return {**{k: getattr(f, k) for k in ("id", "agent", "file", "start_line", "end_line",
                                                  "severity", "title", "description", "recommendation")},
                    # The numbered head code, so the verdict rests on the code, not the description.
                    **({"code": excerpt(by_path[f.file].head, f.start_line, f.end_line)}
                       if f.file in by_path else {})}

        changed = {path: changed_lines(f.diff) for path, f in by_path.items() if path in call.files}
        reviewers = {agent: [p for p in item["files"] if p in result.approved]
                     for agent, item in result.plan.items()}
        reviewers = {agent: files for agent, files in reviewers.items() if files}
        payload = {"round": round_number, "max_rounds": MAX_ROUNDS, "specialists": reviewers,
                   "changed_lines": changed, "findings": [entry(f) for f in candidates]}
        if edits:
            # What each follow-up replaced, so the aggregator can refuse an edit and keep the original.
            payload["edits"] = {agent: {"replaced": [entry(by_id[i]) for i in edit["before"]],
                                        "new_ids": edit["after"]} for agent, edit in edits.items()}
        if failed:
            payload["failed_specialists"] = failed
        self._note("aggregator", f"Judging {len(candidates)} finding(s), round {round_number}")
        system, text = prompt("aggregator.md"), json.dumps(payload)
        data = self._ask(call, system, text, VERDICT_SCHEMA, 1500)
        requests = data.get("requested_context") if isinstance(data, dict) else None
        if isinstance(requests, list) and requests:
            # One read per round: the aggregator sees the code it asked for, then judges.
            context = self._context("aggregator", requests, by_path, result, changed=True)
            if context:
                self._note("aggregator", "Reading requested code before judging")
                again = Call(role="aggregator", agent="aggregator", files=call.files,
                             focus="Judging with requested code")
                result.calls.append(again)
                final = self._ask(again, system, text + "\n\n=== Requested code ===\n" + context
                                  + "\nJudge now with this code; do not request more.",
                                  VERDICT_SCHEMA, 1500)
                if final is not None:
                    data, call = final, again
        verdict = None if data is None else parse_verdict(
            data, candidates + replaced, {a: {"files": fs} for a, fs in reviewers.items()},
            edited=edits, failed=failed,
        )
        if verdict is None:
            # Unvalidated findings never reach the developer as kept.
            for finding in candidates:
                finding.kept, finding.rejection = False, "aggregator verdict unavailable"
            if call.status == "ok":
                call.status, call.error = "failed", "verdict did not match the contract"
            result.limitations.append(
                f"Aggregator unavailable; {len(candidates)} unvalidated finding(s) withheld."
            )
            return []
        rejected, refused, follow_ups, result.summary = verdict
        for agent, reason in refused.items():
            for i in edits[agent]["before"]:
                by_id[i].kept, by_id[i].rejection = True, None
            for i in edits[agent]["after"]:
                by_id[i].kept, by_id[i].rejection = False, f"edit refused: {reason}".rstrip(": ")
        for finding in result.kept:
            if finding.id in rejected:
                finding.kept, finding.rejection = False, rejected[finding.id]
        self._note("aggregator", f"Kept {len(result.kept)}, rejected {len(rejected)}"
                   + (f", refused edits by {_names(refused)}" if refused else "")
                   + "".join(f"; asking {agent} to {action}" for action, agent, _, _ in follow_ups))
        return follow_ups

    def plan(self, files):
        """Coordinator step (paths only, cheap). Returns a result awaiting approval."""
        result = TeamResult()
        reviewable = [f for f in files if not f.skipped]
        for f in files:
            if f.skipped:
                result.limitations.append(f"{f.path}: not reviewed ({f.skipped}).")
        if reviewable:
            self._invoke(self.planner, {"files": reviewable, "result": result})
        else:
            result.summary = "Nothing reviewable in this range."
        return result

    def estimate(self, result, files, selection):
        """Rough upper bound for the approval step: (input tokens, output tokens, agents)."""
        by_path = {f.path: f for f in files}
        per_agent = []
        for agent, item in result.plan.items():
            chosen = [by_path[p] for p in item["files"] if p in selection]
            if chosen:
                chars = len(prompt("specialist.md", agent=agent, focus=item["focus"]))
                chars += sum(len(numbered(f.head)) + len(f.diff) + 60 for f in chosen)
                per_agent.append((chars // 4, retries(self.config, agent) + 1))
        return worst_case(per_agent) + (len(per_agent),)

    def execute(self, result, files, selection=None, *, budget=None):
        """Specialists and aggregator on the approved paths only (default: unflagged), within
        `budget` (the approved "at most") when one is given."""
        self.budget, self._spent, self._held, self._skipped = budget, [0, 0], [0, 0], 0
        selection = result.default_selection() if selection is None else set(selection)
        by_path = {f.path: f for f in files if not f.skipped}
        result.approved = {p for p in result.files() if p in selection}
        for path in result.files():
            if path not in result.approved:
                why = result.flagged.get(path)
                result.limitations.append(
                    f"{path}: not reviewed ({why}; not approved)." if why
                    else f"{path}: not reviewed (not approved)."
                )
        work = [
            (agent, {"files": [p for p in item["files"] if p in result.approved],
                     "focus": item["focus"]})
            for agent, item in result.plan.items()
        ]
        work = [(agent, item) for agent, item in work if item["files"]]
        if not work:
            result.summary = result.summary or "No files were approved for review."
            return result
        self._reads = 0
        self._invoke(self.reviewer, {"by_path": by_path, "result": result, "work": work,
                                     "outcomes": [], "round": 1})
        if self._skipped:
            result.limitations.append(
                f"Review budget reached (at most ${budget.max_cost:.4f} approved); "
                f"{self._skipped} call(s) were skipped, so this review is partial."
            )
        return result

    def run(self, files, selection=None):
        """Plan and execute in one go (notebooks/tests). Flagged files are skipped
        unless `selection` names them."""
        result = self.plan(files)
        if not result.plan:
            return result
        return self.execute(result, files, selection)
