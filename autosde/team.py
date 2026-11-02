"""Coordinator -> parallel specialists -> aggregator, over one committed range."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from autosde import agents
from autosde.agents import SPECIALISTS, load_config, specialists
from autosde.knowledge import context_block
from autosde.noise import flag

PROMPTS = Path(__file__).parent / "prompts"
AGENTS = tuple(SPECIALISTS)
SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("correctness", "security", "reliability", "performance", "maintainability")

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
    "required": ["findings", "summary"],
    "properties": {
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
    "required": ["rejected", "summary"],
    "properties": {
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
    text = (agents.PROMPTS / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


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
        return [path for item in self.plan.values() for path in item["files"]]

    def default_selection(self):
        """Proposed approval: every planned file except flagged ones."""
        return {path for path in self.files() if path not in self.flagged}

    @property
    def kept(self):
        return [f for f in self.findings if f.kept]


class Team:
    def __init__(self, model, *, workers=4, config=None, retriever=None):
        # retriever(kb_ids, query) -> ranked chunks; None disables knowledge bases.
        self.model, self.workers, self.retriever = model, workers, retriever
        self._fixed = config  # tests pass one; otherwise agents.json is re-read per review
        self.config = config or load_config()

    def _ask(self, call, system, text, schema, limit):
        override = self.config["agents"].get(call.agent, {}).get("model")
        call.model = override or getattr(self.model, "model", None)
        reply = self.model.complete(
            role=call.agent, system=system, prompt=text, schema=schema, max_output_tokens=limit,
            model=override,
        )
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
        data = self._ask(call, prompt("coordinator.md"), json.dumps({"changed_files": listing}),
                         plan_schema(table), 1500)
        paths = {f.path for f in files}
        for item in (data or {}).get("flagged", []):
            if item.get("file") in paths and item["file"] not in result.flagged:
                result.flagged[item["file"]] = f"coordinator: {item.get('reason', '')}".strip()
        plan, assigned = {}, set()
        for item in (data or {}).get("assignments", []):
            agent = item.get("agent")
            for path in item.get("files", []):
                if agent in table and path not in assigned and any(f.path == path for f in files):
                    plan.setdefault(agent, {"files": [], "focus": item.get("focus", "")})
                    plan[agent]["files"].append(path)
                    assigned.add(path)
        missing = [f.path for f in files if f.path not in assigned]
        if data is None:
            result.limitations.append("Coordinator unavailable; files were routed by type.")
        elif missing:
            result.limitations.append(f"Coordinator left {len(missing)} file(s) unassigned; routed by type.")
        for path in missing:
            plan.setdefault(route(path, table), {"files": [], "focus": ""})["files"].append(path)
        result.plan = plan
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
        files = [by_path[p] for p in assignment["files"]]
        call = Call(role="specialist", agent=agent, files=[f.path for f in files],
                    focus=assignment["focus"])
        focus_file = agents.PROMPTS / "focus" / f"{agent}.md"
        focus = " ".join(
            part for part in (assignment["focus"], focus_file.read_text().strip() if focus_file.exists() else "")
            if part
        )
        body = "\n\n".join(
            f"=== {f.path} (head) ===\n{numbered(f.head)}\n=== {f.path} (diff) ===\n{f.diff}"
            for f in files
        )
        reference = self._reference(agent, files)
        if reference:
            body = reference + "\n\n" + body
        data = self._ask(call, prompt("specialist.md", agent=agent, focus=focus or "general correctness"),
                         body, FINDINGS_SCHEMA, 4000)
        findings = []
        for item in (data or {}).get("findings", []):
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
        return call, findings

    def _aggregate(self, result, by_path=None):
        if not result.findings:
            result.summary = "No findings were reported."
            return
        call = Call(role="aggregator", agent="aggregator", files=sorted({f.file for f in result.findings}))
        result.calls.append(call)
        listing = [
            {k: getattr(f, k) for k in ("id", "agent", "file", "start_line", "end_line",
                                        "severity", "title", "description", "recommendation")}
            for f in result.findings
        ]
        changed = {path: changed_lines(f.diff) for path, f in (by_path or {}).items()
                   if path in call.files}
        data = self._ask(call, prompt("aggregator.md"),
                         json.dumps({"changed_lines": changed, "findings": listing}),
                         VERDICT_SCHEMA, 1500)
        if data is None:
            result.limitations.append("Aggregator unavailable; all findings were kept.")
            return
        by_id = {f.id: f for f in result.findings}
        for item in data.get("rejected", []):
            if item.get("id") in by_id:
                by_id[item["id"]].kept = False
                by_id[item["id"]].rejection = item.get("reason", "")
        result.summary = data.get("summary", "")

    def plan(self, files):
        """Coordinator step (paths only, cheap). Returns a result awaiting approval."""
        result = TeamResult()
        reviewable = [f for f in files if not f.skipped]
        for f in files:
            if f.skipped:
                result.limitations.append(f"{f.path}: not reviewed ({f.skipped}).")
        if reviewable:
            self._plan(reviewable, result)
        else:
            result.summary = "Nothing reviewable in this range."
        return result

    def estimate(self, result, files, selection):
        """Rough upper bound for the approval step: (input tokens, output tokens, agents)."""
        by_path = {f.path: f for f in files}
        chars, agents = 0, 0
        for agent, item in result.plan.items():
            chosen = [by_path[p] for p in item["files"] if p in selection]
            if chosen:
                agents += 1
                chars += len(prompt("specialist.md", agent=agent, focus=item["focus"]))
                chars += sum(len(numbered(f.head)) + len(f.diff) + 60 for f in chosen)
        return chars // 4, agents * 4000 + (1500 if agents else 0), agents

    def execute(self, result, files, selection=None):
        """Specialists and aggregator on the approved paths only (default: unflagged)."""
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
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            outcomes = list(pool.map(lambda item: self._review(item[0], item[1], by_path, result), work))
        for call, findings in outcomes:
            result.calls.append(call)
            if call.status != "ok":
                result.limitations.append(f"{call.agent} specialist failed: {call.error}.")
            for n, finding in enumerate(findings, 1):
                finding.id = f"{call.agent}-{n}"
                result.findings.append(finding)
        self._aggregate(result, by_path)
        return result

    def run(self, files, selection=None):
        """Plan and execute in one go (notebooks/tests). Flagged files are skipped
        unless `selection` names them."""
        result = self.plan(files)
        if not result.plan:
            return result
        return self.execute(result, files, selection)
