"""Coordinator -> parallel specialists -> aggregator, over one committed range."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
EXTENSIONS = {
    "python": (".py",),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"),
    "typescript": (".ts", ".tsx"),
    "java": (".java",),
    "go": (".go",),
    "csharp": (".cs",),
}
AGENTS = (*EXTENSIONS, "general")
SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("correctness", "security", "reliability", "performance", "maintainability")

# Strict structured-output schemas: every property required, no extras.
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignments", "rationale"],
    "properties": {
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
    text = (PROMPTS / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def route(path):
    lower = path.lower()
    return next((a for a, ext in EXTENSIONS.items() if lower.endswith(ext)), "general")


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
    plan: dict = field(default_factory=dict)
    summary: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def kept(self):
        return [f for f in self.findings if f.kept]


class Team:
    def __init__(self, model, *, workers=4):
        self.model, self.workers = model, workers

    def _ask(self, call, system, text, schema, limit):
        reply = self.model.complete(
            role=call.agent, system=system, prompt=text, schema=schema, max_output_tokens=limit
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
        call = Call(role="coordinator", agent="coordinator", files=[f.path for f in files])
        result.calls.append(call)
        listing = [
            {"path": f.path, "status": f.status, "lines": max(1, len(f.head.splitlines())),
             "changed_lines": sum(1 for l in f.diff.splitlines() if l[:1] in "+-")}
            for f in files
        ]
        data = self._ask(call, prompt("coordinator.md"), json.dumps({"changed_files": listing}),
                         PLAN_SCHEMA, 1200)
        plan, assigned = {}, set()
        for item in (data or {}).get("assignments", []):
            agent = item.get("agent")
            for path in item.get("files", []):
                if agent in AGENTS and path not in assigned and any(f.path == path for f in files):
                    plan.setdefault(agent, {"files": [], "focus": item.get("focus", "")})
                    plan[agent]["files"].append(path)
                    assigned.add(path)
        missing = [f.path for f in files if f.path not in assigned]
        if data is None:
            result.limitations.append("Coordinator unavailable; files were routed by type.")
        elif missing:
            result.limitations.append(f"Coordinator left {len(missing)} file(s) unassigned; routed by type.")
        for path in missing:
            plan.setdefault(route(path), {"files": [], "focus": ""})["files"].append(path)
        result.plan = plan
        return plan

    def _review(self, agent, assignment, by_path, result):
        files = [by_path[p] for p in assignment["files"]]
        call = Call(role="specialist", agent=agent, files=[f.path for f in files],
                    focus=assignment["focus"])
        focus_file = PROMPTS / "focus" / f"{agent}.md"
        focus = " ".join(
            part for part in (assignment["focus"], focus_file.read_text().strip() if focus_file.exists() else "")
            if part
        )
        body = "\n\n".join(
            f"=== {f.path} (head) ===\n{numbered(f.head)}\n=== {f.path} (diff) ===\n{f.diff}"
            for f in files
        )
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

    def _aggregate(self, result):
        if not result.findings:
            result.summary = "No findings were reported."
            return
        call = Call(role="aggregator", agent="aggregator", files=sorted({f.file for f in result.findings}))
        result.calls.append(call)
        listing = [
            {k: getattr(f, k) for k in ("id", "agent", "file", "start_line", "end_line",
                                        "severity", "title", "description")}
            for f in result.findings
        ]
        data = self._ask(call, prompt("aggregator.md"), json.dumps({"findings": listing}),
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

    def run(self, files):
        """Review the given ChangedFile list; files with a `skipped` reason are not sent."""
        result = TeamResult()
        reviewable = [f for f in files if not f.skipped]
        for f in files:
            if f.skipped:
                result.limitations.append(f"{f.path}: not reviewed ({f.skipped}).")
        if not reviewable:
            result.summary = "Nothing reviewable in this range."
            return result
        by_path = {f.path: f for f in reviewable}
        plan = self._plan(reviewable, result)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            outcomes = list(pool.map(
                lambda item: self._review(item[0], item[1], by_path, result), plan.items()
            ))
        for call, findings in outcomes:
            result.calls.append(call)
            if call.status != "ok":
                result.limitations.append(f"{call.agent} specialist failed: {call.error}.")
            for n, finding in enumerate(findings, 1):
                finding.id = f"{call.agent}-{n}"
                result.findings.append(finding)
        self._aggregate(result)
        return result
