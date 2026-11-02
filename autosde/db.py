"""SQLite store: projects, reviews, agent runs and findings. No source code is stored."""

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    range_spec TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL,           -- planning | awaiting_approval | running | completed | failed | cancelled | expired
    model TEXT NOT NULL,
    summary TEXT,
    limitations TEXT,               -- newline-separated
    error TEXT,
    files_changed INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    role TEXT NOT NULL,             -- coordinator | specialist | aggregator
    agent TEXT NOT NULL,
    files TEXT NOT NULL,            -- newline-separated paths
    focus TEXT,
    status TEXT NOT NULL,
    error TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    seconds REAL DEFAULT 0,
    model TEXT                      -- the model this call actually used
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    finding_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    file TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    kept INTEGER NOT NULL,
    rejection TEXT
);
CREATE TABLE IF NOT EXISTS review_files (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    agent TEXT,                     -- planned specialist (NULL when not reviewable)
    flag TEXT,                      -- why it is probably not worth tokens
    skipped TEXT,                   -- why it could not be sent (binary, size, secret)
    included INTEGER                -- NULL until approved; 1 reviewed, 0 not approved
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL,             -- user | assistant
    content TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS home_chat (     -- the Home page chat about all projects
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_project ON chat_messages(project_id, id);
CREATE INDEX IF NOT EXISTS files_review ON review_files(review_id);
CREATE INDEX IF NOT EXISTS reviews_project ON reviews(project_id, started_at);
CREATE INDEX IF NOT EXISTS runs_review ON agent_runs(review_id);
CREATE INDEX IF NOT EXISTS findings_review ON findings(review_id);
"""


# A review's position within its project, by when it ran (1 = the project's first review).
NUMBER = ("(SELECT COUNT(*) FROM reviews n WHERE n.project_id = r.project_id AND"
          " (n.started_at < r.started_at OR (n.started_at = r.started_at AND n.id <= r.id))) AS number")


class Store:
    def __init__(self, path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.exists():
                # Findings quote reviewed code; keep the database owner-only.
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(agent_runs)")}
        if "model" not in columns:  # databases created before per-run models
            self.db.execute("ALTER TABLE agent_runs ADD COLUMN model TEXT")

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    def project(self, path, name):
        self.db.execute(
            "INSERT OR IGNORE INTO projects(name, path, created_at) VALUES (?, ?, ?)",
            (name, str(path), time.time()),
        )
        return self.db.execute("SELECT id FROM projects WHERE path = ?", (str(path),)).fetchone()[0]

    def start_review(self, project_id, changes, model):
        return self.db.execute(
            "INSERT INTO reviews(project_id, range_spec, base_sha, head_sha, status, model,"
            " files_changed, started_at) VALUES (?, ?, ?, ?, 'planning', ?, ?, ?)",
            (project_id, changes.range_spec, changes.base, changes.head, model,
             len(changes.files), time.time()),
        ).lastrowid

    def _calls(self, review_id, result):
        for c in result.calls:
            if c.stored:
                continue
            self.db.execute(
                "INSERT INTO agent_runs(review_id, role, agent, files, focus, status, error,"
                " input_tokens, output_tokens, seconds, model) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, c.role, c.agent, "\n".join(c.files), c.focus, c.status, c.error,
                 c.input_tokens, c.output_tokens, round(c.seconds, 3), c.model),
            )
            c.stored = True

    @staticmethod
    def _totals(result, input_price, output_price):
        incoming = sum(c.input_tokens for c in result.calls)
        outgoing = sum(c.output_tokens for c in result.calls)
        return incoming, outgoing, round((incoming * input_price + outgoing * output_price) / 1e6, 6)

    def save_plan(self, review_id, result, files, *, input_price, output_price):
        """Store the coordinator call and the per-file plan; await approval."""
        agent_of = {p: agent for agent, item in result.plan.items() for p in item["files"]}
        incoming, outgoing, cost = self._totals(result, input_price, output_price)
        self.db.execute("BEGIN")
        try:
            self._calls(review_id, result)
            for f in files:
                self.db.execute(
                    "INSERT INTO review_files(review_id, path, agent, flag, skipped)"
                    " VALUES (?,?,?,?,?)",
                    (review_id, f.path, agent_of.get(f.path), result.flagged.get(f.path), f.skipped),
                )
            self.db.execute(
                "UPDATE reviews SET status = 'awaiting_approval', input_tokens = ?,"
                " output_tokens = ?, cost_usd = ? WHERE id = ?",
                (incoming, outgoing, cost, review_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def mark_running(self, review_id):
        self.db.execute("UPDATE reviews SET status = 'running' WHERE id = ?", (review_id,))

    def cancel_review(self, review_id, result=None, status="cancelled"):
        self.db.execute(
            "UPDATE review_files SET included = 0 WHERE review_id = ? AND agent IS NOT NULL",
            (review_id,),
        )
        self.db.execute(
            "UPDATE reviews SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
            (status, "Not run: the plan was not approved." if status == "cancelled"
             else "Not run: the plan expired before approval.", time.time(), review_id),
        )

    def finish_review(self, review_id, result, *, input_price, output_price):
        incoming, outgoing, cost = self._totals(result, input_price, output_price)
        failed = any(c.role == "specialist" for c in result.calls) and all(
            c.status != "ok" for c in result.calls if c.role == "specialist"
        )
        self.db.execute("BEGIN")
        try:
            self._calls(review_id, result)
            self.db.execute(
                "UPDATE review_files SET included = (path IN (SELECT value FROM json_each(?)))"
                " WHERE review_id = ? AND agent IS NOT NULL",
                (json.dumps(sorted(result.approved)), review_id),
            )
            for f in result.findings:
                self.db.execute(
                    "INSERT INTO findings(review_id, finding_key, agent, file, start_line, end_line,"
                    " severity, category, title, description, recommendation, confidence, kept,"
                    " rejection) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (review_id, f.id, f.agent, f.file, f.start_line, f.end_line, f.severity,
                     f.category, f.title, f.description, f.recommendation, f.confidence,
                     int(f.kept), f.rejection),
                )
            self.db.execute(
                "UPDATE reviews SET status = ?, summary = ?, limitations = ?, input_tokens = ?,"
                " output_tokens = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
                ("failed" if failed else "completed", result.summary,
                 "\n".join(result.limitations), incoming, outgoing, cost,
                 time.time(), review_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def fail_review(self, review_id, error):
        self.db.execute(
            "UPDATE reviews SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error, time.time(), review_id),
        )

    # --- queries -----------------------------------------------------------

    def projects(self):
        return self._all(
            "SELECT p.*, COUNT(r.id) AS reviews, MAX(r.started_at) AS last_review"
            " FROM projects p LEFT JOIN reviews r ON r.project_id = p.id"
            " GROUP BY p.id ORDER BY last_review DESC NULLS LAST, p.name"
        )

    def history(self, project_id, *, limit=30):
        """Reviews with kept and rejected findings, newest first: what the chat agent knows."""
        reviews = self._all(
            f"SELECT r.id, {NUMBER}, r.range_spec, r.status, r.summary, r.limitations, r.started_at"
            " FROM reviews r WHERE r.project_id = ? ORDER BY r.started_at DESC LIMIT ?", project_id, limit)
        for review in reviews:
            review["findings"] = self._all(
                "SELECT file, start_line, end_line, severity, category, title, description,"
                " recommendation, kept, rejection, agent FROM findings WHERE review_id = ?"
                " ORDER BY kept DESC, id", review["id"])
        return reviews

    # project_id None is the Home chat about all projects.
    def chat(self, project_id):
        columns = "role, content, model, input_tokens, output_tokens, created_at"
        if project_id is None:
            return self._all(f"SELECT {columns} FROM home_chat ORDER BY id")
        return self._all(f"SELECT {columns} FROM chat_messages WHERE project_id = ? ORDER BY id",
                         project_id)

    def add_chat(self, project_id, role, content, *, model=None, input_tokens=0,
                 output_tokens=0):
        values = (role, content, model, input_tokens, output_tokens, time.time())
        if project_id is None:
            self.db.execute("INSERT INTO home_chat(role, content, model, input_tokens,"
                            " output_tokens, created_at) VALUES (?,?,?,?,?,?)", values)
        else:
            self.db.execute("INSERT INTO chat_messages(project_id, role, content, model,"
                            " input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?,?,?)",
                            (project_id, *values))

    def clear_chat(self, project_id):
        if project_id is None:
            self.db.execute("DELETE FROM home_chat")
        else:
            self.db.execute("DELETE FROM chat_messages WHERE project_id = ?", (project_id,))

    def find_project(self, ref):
        """A project by numeric id, exact name or path."""
        rows = self._all(
            "SELECT * FROM projects WHERE CAST(id AS TEXT) = ? OR name = ? OR path = ?",
            str(ref), str(ref),
            str(ref) if str(ref).startswith("https://") else str(Path(str(ref)).expanduser().resolve()),
        )
        return rows[0] if rows else None

    def reviews(self, project_id=None, limit=50):
        where, args = ("WHERE r.project_id = ?", (project_id,)) if project_id else ("", ())
        return self._all(
            f"SELECT r.*, {NUMBER}, p.name AS project, p.path AS project_path,"
            " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.kept) AS findings,"
            " (SELECT GROUP_CONCAT(DISTINCT a.agent) FROM agent_runs a"
            "   WHERE a.review_id = r.id AND a.role = 'specialist') AS agents"
            f" FROM reviews r JOIN projects p ON p.id = r.project_id {where}"
            " ORDER BY r.started_at DESC LIMIT ?",
            *args, limit,
        )

    def review(self, review_id):
        rows = self.reviews_by_id(review_id)
        if not rows:
            return None
        review = rows[0]
        review["agent_runs"] = self._all(
            "SELECT * FROM agent_runs WHERE review_id = ? ORDER BY id", review_id
        )
        review["files"] = self._all(
            "SELECT path, agent, flag, skipped, included FROM review_files"
            " WHERE review_id = ? ORDER BY flag IS NOT NULL, path",
            review_id,
        )
        review["findings"] = self._all(
            "SELECT * FROM findings WHERE review_id = ? ORDER BY kept DESC,"
            " CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,"
            " file, start_line",
            review_id,
        )
        return review

    def reviews_by_id(self, review_id):
        return self._all(
            f"SELECT r.*, {NUMBER}, p.name AS project, p.path AS project_path FROM reviews r"
            " JOIN projects p ON p.id = r.project_id WHERE r.id = ?",
            review_id,
        )

    def agents(self, project_id=None):
        """Which agents ran (against one project, or all), how often and how well."""
        where, args = ("WHERE r.project_id = ?", (project_id,)) if project_id else ("", ())
        return self._all(
            "SELECT p.name AS project, a.role, a.agent, COUNT(*) AS runs,"
            " SUM(a.status = 'ok') AS succeeded, SUM(a.status != 'ok') AS failed,"
            " SUM(a.input_tokens) AS input_tokens, SUM(a.output_tokens) AS output_tokens,"
            " (SELECT COUNT(*) FROM findings f JOIN reviews r2 ON r2.id = f.review_id"
            "   WHERE f.agent = a.agent AND r2.project_id = p.id AND f.kept) AS findings_kept,"
            " MAX(r.started_at) AS last_run"
            " FROM agent_runs a JOIN reviews r ON r.id = a.review_id"
            f" JOIN projects p ON p.id = r.project_id {where}"
            " GROUP BY p.id, a.role, a.agent ORDER BY p.name, a.role, a.agent",
            *args,
        )
