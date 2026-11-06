"""DynamoDB store: the same interface as the SQLite `Store`, for Review Crew on AWS.

One table, keyed by PK/SK strings, with two global secondary indexes:

    COUNTER            <entity>                 next integer id per entity
    PATH#<path>        UNIQUE                   one project per path (conditional put)
    PROJECT#<id>       META                     project (GSI2: PROJECTS)
    REVIEW#<id>        META                     review (GSI1: PROJECT#<id>#REVIEWS, GSI2: REVIEWS)
    REVIEW#<id>        RUN#<n> / FINDING#<n> / FILE#<path>
    CHAT#<id|HOME>     <time>#<n>               chat messages

Integer ids are kept so the API and UI stay the same. No source code is stored.
"""

import time
from decimal import Decimal

from boto3.dynamodb.conditions import Key

SEVERITY = {"critical": 0, "high": 1, "medium": 2}
TABLE_SPEC = {
    "AttributeDefinitions": [{"AttributeName": n, "AttributeType": "S"}
                             for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
    "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
    "GlobalSecondaryIndexes": [
        {"IndexName": name, "KeySchema": [{"AttributeName": f"{name}PK", "KeyType": "HASH"},
                                          {"AttributeName": f"{name}SK", "KeyType": "RANGE"}],
         "Projection": {"ProjectionType": "ALL"}}
        for name in ("GSI1", "GSI2")
    ],
    "BillingMode": "PAY_PER_REQUEST",
}


def create_table(resource, name):
    """Create the table (tests and first-time setup; CDK owns it in AWS)."""
    table = resource.create_table(TableName=name, **TABLE_SPEC)
    table.wait_until_exists()
    return table


def to_dynamo(value):
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def stamp(seconds, ident):
    """A sort key that orders by time, then id."""
    return f"{seconds:017.6f}#{ident:010d}"


class Table:
    """Small helpers over a boto3 Table resource."""

    def __init__(self, table):
        self.table = table

    def get(self, pk, sk, *, consistent=False):
        item = self.table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=consistent).get("Item")
        return from_dynamo(item) if item else None

    def put(self, item, **kwargs):
        self.table.put_item(Item=to_dynamo(item), **kwargs)

    def update(self, pk, sk, values=None, *, add=None, remove=(), condition=None):
        """SET `values` and ADD `add` (numbers); returns the updated item."""
        parts, names, data = [], {}, {}
        sets = [f"#{i} = :{i}" for i, _ in enumerate(values or {})]
        for i, (key, value) in enumerate((values or {}).items()):
            names[f"#{i}"], data[f":{i}"] = key, to_dynamo(value)
        if sets:
            parts.append("SET " + ", ".join(sets))
        if add:
            adds = []
            for j, (key, value) in enumerate(add.items()):
                names[f"#a{j}"], data[f":a{j}"] = key, to_dynamo(value)
                adds.append(f"#a{j} :a{j}")
            parts.append("ADD " + ", ".join(adds))
        if remove:
            for k, key in enumerate(remove):
                names[f"#r{k}"] = key
            parts.append("REMOVE " + ", ".join(f"#r{k}" for k in range(len(remove))))
        kwargs = {"Key": {"PK": pk, "SK": sk}, "UpdateExpression": " ".join(parts),
                  "ExpressionAttributeNames": names, "ReturnValues": "ALL_NEW"}
        if data:
            kwargs["ExpressionAttributeValues"] = data
        if condition is not None:
            kwargs["ConditionExpression"] = condition
        return from_dynamo(self.table.update_item(**kwargs)["Attributes"])

    def delete(self, pk, sk):
        self.table.delete_item(Key={"PK": pk, "SK": sk})

    def query(self, key, *, index=None, forward=True, limit=None):
        kwargs = {"KeyConditionExpression": key, "ScanIndexForward": forward}
        if index:
            kwargs["IndexName"] = index
        items = []
        while True:
            if limit:
                kwargs["Limit"] = limit - len(items)
            page = self.table.query(**kwargs)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page or (limit and len(items) >= limit):
                return [from_dynamo(i) for i in items]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def children(self, pk, prefix=""):
        key = Key("PK").eq(pk)
        return self.query(key & Key("SK").begins_with(prefix) if prefix else key)

    def next_id(self, entity):
        return self.update("COUNTER", entity, add={"n": 1})["n"]

    def delete_all(self, items):
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})


def _clean(item, *drop):
    hidden = {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", *drop}
    return {k: v for k, v in item.items() if k not in hidden}


class DynamoStore:
    """Projects, reviews, agent runs, findings, review files and chat in DynamoDB."""

    REVIEW_DEFAULTS = {"summary": None, "limitations": None, "error": None, "finished_at": None,
                       "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def __init__(self, table, *, clock=time.time):
        self.t = Table(table)
        self.clock = clock

    # --- writes --------------------------------------------------------------

    def project(self, path, name):
        path = str(path)
        unique = self.t.get(f"PATH#{path}", "UNIQUE")
        if unique:
            return unique["project_id"]
        project_id = self.t.next_id("project")
        try:
            self.t.put({"PK": f"PATH#{path}", "SK": "UNIQUE", "project_id": project_id},
                       ConditionExpression="attribute_not_exists(PK)")
        except self.t.table.meta.client.exceptions.ConditionalCheckFailedException:
            return self.t.get(f"PATH#{path}", "UNIQUE")["project_id"]  # another request won
        self.t.put({"PK": f"PROJECT#{project_id}", "SK": "META", "GSI2PK": "PROJECTS",
                    "GSI2SK": f"{project_id:010d}", "id": project_id, "name": name, "path": path,
                    "created_at": self.clock(), "reviews": 0})
        return project_id

    def start_review(self, project_id, changes, model):
        now = self.clock()
        project = self.t.update(f"PROJECT#{project_id}", "META", {"last_review": now}, add={"reviews": 1})
        review_id = self.t.next_id("review")
        key = stamp(now, review_id)
        self.t.put({"PK": f"REVIEW#{review_id}", "SK": "META",
                    "GSI1PK": f"PROJECT#{project_id}#REVIEWS", "GSI1SK": key,
                    "GSI2PK": "REVIEWS", "GSI2SK": key,
                    "id": review_id, "project_id": project_id, "number": project["reviews"],
                    "range_spec": changes.range_spec, "base_sha": changes.base, "head_sha": changes.head,
                    "status": "planning", "model": model, "files_changed": len(changes.files),
                    "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "started_at": now,
                    "runs": 0, "findings_kept": 0, "agents": ""})
        return review_id

    def _calls(self, review_id, result):
        for c in result.calls:
            if c.stored:
                continue
            n = self.t.update(f"REVIEW#{review_id}", "META", add={"runs": 1})["runs"]
            self.t.put({"PK": f"REVIEW#{review_id}", "SK": f"RUN#{n:05d}", "id": n, "review_id": review_id,
                        "role": c.role, "agent": c.agent, "files": "\n".join(c.files), "focus": c.focus,
                        "status": c.status, "error": c.error, "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens, "seconds": round(c.seconds, 3), "model": c.model})
            c.stored = True

    @staticmethod
    def _totals(result, input_price, output_price):
        incoming = sum(c.input_tokens for c in result.calls)
        outgoing = sum(c.output_tokens for c in result.calls)
        return incoming, outgoing, round((incoming * input_price + outgoing * output_price) / 1e6, 6)

    def save_plan(self, review_id, result, files, *, input_price, output_price, record=None):
        import json

        from autosde.team import owners_of

        agent_of = {p: ", ".join(a) for p, a in owners_of(result.plan).items()}
        incoming, outgoing, cost = self._totals(result, input_price, output_price)
        self._calls(review_id, result)
        with self.t.table.batch_writer() as batch:
            for f in files:
                batch.put_item(Item=to_dynamo({
                    "PK": f"REVIEW#{review_id}", "SK": f"FILE#{f.path}", "path": f.path,
                    "agent": agent_of.get(f.path), "flag": result.flagged.get(f.path), "skipped": f.skipped}))
        self.t.update(f"REVIEW#{review_id}", "META", {"status": "awaiting_approval", "input_tokens": incoming,
                                                      "output_tokens": outgoing, "cost_usd": cost,
                                                      "plan_json": json.dumps(record) if record else None})

    def plan(self, review_id):
        import json

        item = self.t.get(f"REVIEW#{review_id}", "META")
        return json.loads(item["plan_json"]) if item and item.get("plan_json") else None

    def set_files_changed(self, review_id, count):
        self.t.update(f"REVIEW#{review_id}", "META", {"files_changed": count})

    def mark_running(self, review_id):
        self.t.update(f"REVIEW#{review_id}", "META", {"status": "running"})

    def add_progress(self, review_id, agent, text):
        """One line of what the team is doing (no code or prompts), shown while it works."""
        at, n = self.clock(), self.t.next_id("progress")
        self.t.put({"PK": f"REVIEW#{review_id}", "SK": f"PROGRESS#{stamp(at, n)}",
                    "at": at, "agent": str(agent)[:40], "text": str(text)[:300]})

    def _files(self, review_id):
        return self.t.children(f"REVIEW#{review_id}", "FILE#")

    def cancel_review(self, review_id, result=None, status="cancelled"):
        for f in self._files(review_id):
            if f.get("agent"):
                self.t.update(f["PK"], f["SK"], {"included": 0})
        summary = ("Not run: the plan was not approved." if status == "cancelled"
                   else "Not run: the plan expired before approval.")
        self.t.update(f"REVIEW#{review_id}", "META", {"status": status, "summary": summary,
                                                      "finished_at": self.clock()})

    def finish_review(self, review_id, result, *, input_price, output_price):
        incoming, outgoing, cost = self._totals(result, input_price, output_price)
        failed = any(c.role == "specialist" for c in result.calls) and all(
            c.status != "ok" for c in result.calls if c.role == "specialist")
        self._calls(review_id, result)
        approved = set(result.approved)
        for f in self._files(review_id):
            if f.get("agent"):
                self.t.update(f["PK"], f["SK"], {"included": int(f["path"] in approved)})
        with self.t.table.batch_writer() as batch:
            for n, f in enumerate(result.findings, 1):
                batch.put_item(Item=to_dynamo({
                    "PK": f"REVIEW#{review_id}", "SK": f"FINDING#{n:05d}", "id": n, "review_id": review_id,
                    "finding_key": f.id, "agent": f.agent, "file": f.file, "start_line": f.start_line,
                    "end_line": f.end_line, "severity": f.severity, "category": f.category,
                    "title": f.title, "description": f.description, "recommendation": f.recommendation,
                    "confidence": float(f.confidence), "kept": int(f.kept), "rejection": f.rejection}))
        specialists = sorted({c.agent for c in result.calls if c.role == "specialist"})
        self.t.update(f"REVIEW#{review_id}", "META", {
            "status": "failed" if failed else "completed", "summary": result.summary,
            "limitations": "\n".join(result.limitations), "input_tokens": incoming,
            "output_tokens": outgoing, "cost_usd": cost, "finished_at": self.clock(),
            "findings_kept": sum(1 for f in result.findings if f.kept), "agents": ",".join(specialists)})

    def fail_review(self, review_id, error):
        self.t.update(f"REVIEW#{review_id}", "META", {"status": "failed", "error": error,
                                                      "finished_at": self.clock()})

    # --- queries -------------------------------------------------------------

    def _project(self, project_id, cache=None):
        if cache is not None and project_id in cache:
            return cache[project_id]
        item = self.t.get(f"PROJECT#{project_id}", "META")
        if cache is not None:
            cache[project_id] = item
        return item

    def projects(self):
        rows = [_clean(p) for p in self.t.query(Key("GSI2PK").eq("PROJECTS"), index="GSI2")]
        for p in rows:
            p.setdefault("last_review", None)
            p["reviews"] = p.get("reviews", 0)
        return sorted(rows, key=lambda p: (p["last_review"] is None, -(p["last_review"] or 0), p["name"]))

    def find_project(self, ref):
        ref = str(ref)
        if ref.isdigit():
            item = self._project(int(ref))
            if item:
                return _clean(item, "reviews", "last_review")
        from pathlib import Path

        path = ref if ref.startswith("https://") else str(Path(ref).expanduser().resolve())
        unique = self.t.get(f"PATH#{path}", "UNIQUE") or self.t.get(f"PATH#{ref}", "UNIQUE")
        if unique:
            return _clean(self._project(unique["project_id"]), "reviews", "last_review")
        for p in self.t.query(Key("GSI2PK").eq("PROJECTS"), index="GSI2"):
            if p["name"] == ref:
                return _clean(p, "reviews", "last_review")
        return None

    def _review_row(self, item, cache):
        project = self._project(item["project_id"], cache) or {}
        row = {**self.REVIEW_DEFAULTS, **_clean(item, "runs", "plan_json")}
        row.update(project=project.get("name"), project_path=project.get("path"))
        return row

    def reviews(self, project_id=None, limit=50):
        key = (Key("GSI1PK").eq(f"PROJECT#{project_id}#REVIEWS") if project_id
               else Key("GSI2PK").eq("REVIEWS"))
        items = self.t.query(key, index="GSI1" if project_id else "GSI2", forward=False, limit=limit)
        cache = {}
        rows = []
        for item in items:
            row = self._review_row(item, cache)
            row["findings"] = row.pop("findings_kept", 0)
            row["agents"] = row.get("agents") or None
            rows.append(row)
        return rows

    def reviews_by_id(self, review_id):
        item = self.t.get(f"REVIEW#{review_id}", "META")
        if not item:
            return []
        row = self._review_row(item, {})
        row.pop("findings_kept", None)
        row.pop("agents", None)
        return [row]

    def review(self, review_id):
        rows = self.reviews_by_id(review_id)
        if not rows:
            return None
        review = rows[0]
        children = [c for c in self.t.children(f"REVIEW#{review_id}") if c["SK"] != "META"]
        runs = [_clean(c) for c in children if c["SK"].startswith("RUN#")]
        files = [_clean(c) for c in children if c["SK"].startswith("FILE#")]
        found = [_clean(c) for c in children if c["SK"].startswith("FINDING#")]
        review["progress"] = [{"at": c["at"], "agent": c["agent"], "text": c["text"]}
                              for c in sorted(children, key=lambda c: c["SK"])
                              if c["SK"].startswith("PROGRESS#")]
        for f in files:
            for key in ("agent", "flag", "skipped", "included"):
                f.setdefault(key, None)
        for run in runs:
            for key in ("focus", "error", "model"):
                run.setdefault(key, None)
        for f in found:
            f.setdefault("rejection", None)
        review["agent_runs"] = sorted(runs, key=lambda r: r["id"])
        review["files"] = sorted(files, key=lambda f: (f["flag"] is not None, f["path"]))
        review["findings"] = sorted(found, key=lambda f: (-f["kept"], SEVERITY.get(f["severity"], 3),
                                                          f["file"], f["start_line"]))
        return review

    def history(self, project_id, *, limit=30):
        out = []
        for item in self.t.query(Key("GSI1PK").eq(f"PROJECT#{project_id}#REVIEWS"), index="GSI1",
                                 forward=False, limit=limit):
            findings = sorted((_clean(c) for c in self.t.children(item["PK"], "FINDING#")),
                              key=lambda f: (-f["kept"], f["id"]))
            out.append({"id": item["id"], "number": item["number"], "range_spec": item["range_spec"],
                        "status": item["status"], "summary": item.get("summary"),
                        "limitations": item.get("limitations"), "started_at": item["started_at"],
                        "findings": [{k: f.get(k) for k in ("file", "start_line", "end_line", "severity",
                                                             "category", "title", "description",
                                                             "recommendation", "kept", "rejection", "agent")}
                                     for f in findings]})
        return out

    def agents(self, project_id=None):
        projects = [self._project(project_id)] if project_id else [
            p for p in self.t.query(Key("GSI2PK").eq("PROJECTS"), index="GSI2")]
        rows = {}
        for project in filter(None, projects):
            for review in self.t.query(Key("GSI1PK").eq(f"PROJECT#{project['id']}#REVIEWS"), index="GSI1"):
                children = self.t.children(review["PK"])
                kept = {}
                for f in children:
                    if f["SK"].startswith("FINDING#") and f["kept"]:
                        kept[f["agent"]] = kept.get(f["agent"], 0) + 1
                for run in (c for c in children if c["SK"].startswith("RUN#")):
                    key = (project["name"], run["role"], run["agent"])
                    row = rows.setdefault(key, {"project": project["name"], "role": run["role"],
                                                "agent": run["agent"], "runs": 0, "succeeded": 0,
                                                "failed": 0, "input_tokens": 0, "output_tokens": 0,
                                                "findings_kept": 0, "last_run": None, "_reviews": set()})
                    row["runs"] += 1
                    row["succeeded" if run["status"] == "ok" else "failed"] += 1
                    row["input_tokens"] += run.get("input_tokens", 0)
                    row["output_tokens"] += run.get("output_tokens", 0)
                    row["last_run"] = max(row["last_run"] or 0, review["started_at"])
                    if review["id"] not in row["_reviews"]:
                        row["_reviews"].add(review["id"])
                        row["findings_kept"] += kept.get(run["agent"], 0)
        out = []
        for key in sorted(rows):
            row = rows[key]
            row.pop("_reviews")
            out.append(row)
        return out

    # --- chat ----------------------------------------------------------------

    def chat(self, project_id):
        pk = f"CHAT#{'HOME' if project_id is None else project_id}"
        return [{k: m.get(k) for k in ("role", "content", "model", "input_tokens", "output_tokens", "created_at")}
                for m in self.t.children(pk)]

    def add_chat(self, project_id, role, content, *, model=None, input_tokens=0, output_tokens=0):
        pk = f"CHAT#{'HOME' if project_id is None else project_id}"
        n = self.t.next_id("chat")
        self.t.put({"PK": pk, "SK": stamp(self.clock(), n), "role": role, "content": content, "model": model,
                    "input_tokens": input_tokens, "output_tokens": output_tokens, "created_at": self.clock()})

    def clear_chat(self, project_id):
        pk = f"CHAT#{'HOME' if project_id is None else project_id}"
        self.t.delete_all(self.t.query(Key("PK").eq(pk)))


# --- knowledge bases -------------------------------------------------------------

MAX_INLINE_TEXT = 300_000  # bytes of document text kept in the item; larger text goes to S3
_WORD = __import__("re").compile(r"[a-z0-9_]{2,}")


class DynamoKnowledgeStore:
    """Knowledge bases, documents and chunks in DynamoDB.

        KB#<id>        META             base (GSI2: KBS / name); KBNAME#<name> UNIQUE
        KB#<id>        DOC#<doc id>     document (GSI1: DOC#<id> / META)
        DOC#<doc id>   CHUNK#<pos>      chunk with its embedding (GSI1: KBCHUNKS#<kb> / <doc>#<pos>)

    Keyword search is BM25 computed here over the bases' chunk text (DynamoDB has no
    full-text index); vectors are compared in memory, as in SQLite.
    """

    def __init__(self, table, *, s3=None, bucket=None):
        self.t = Table(table)
        self.s3, self.bucket = s3, bucket
        self._loaded = {}  # kb id -> chunk items, for this request
        self._document_keys = {}  # direct uploads already know the primary key

    # bases
    def list_bases(self):
        out = []
        for base in self.t.query(Key("GSI2PK").eq("KBS"), index="GSI2"):
            docs = self.t.children(base["PK"], "DOC#")
            out.append({**_clean(base), "documents": len(docs),
                        "chunks": sum(d.get("chunks", 0) for d in docs),
                        "tokens": sum(d.get("tokens", 0) for d in docs)})
        return sorted(out, key=lambda b: b["name"])

    def list_documents(self, kb_id):
        docs = [self._document(d) for d in self.t.children(f"KB#{kb_id}", "DOC#")]
        for d in docs:
            d["has_text"] = int(bool(d.pop("content_inline", None) or d.pop("content_key", None)))
            for key in ("content", "kb_id", "sha256"):
                d.pop(key, None)
        return sorted(docs, key=lambda d: d["created_at"], reverse=True)

    def insert_base(self, name, description, settings, created_at):
        kb_id = self.t.next_id("kb")
        try:
            self.t.put({"PK": f"KBNAME#{name}", "SK": "UNIQUE", "kb_id": kb_id},
                       ConditionExpression="attribute_not_exists(PK)")
        except self.t.table.meta.client.exceptions.ConditionalCheckFailedException:
            from autosde.knowledge import KnowledgeError
            raise KnowledgeError("A knowledge base with that name already exists.") from None
        self.t.put({"PK": f"KB#{kb_id}", "SK": "META", "GSI2PK": "KBS", "GSI2SK": name, "id": kb_id,
                    "name": name, "description": description, "settings": settings, "created_at": created_at})
        return kb_id

    def update_base(self, kb_id, description, settings):
        self.t.update(f"KB#{kb_id}", "META", {"description": description, "settings": settings})

    def delete_base(self, kb_id):
        base = self.t.get(f"KB#{kb_id}", "META")
        for doc in self.t.children(f"KB#{kb_id}", "DOC#"):
            self.delete_document(doc["id"])
        if base:
            self.t.delete(f"KBNAME#{base['name']}", "UNIQUE")
        self.t.delete(f"KB#{kb_id}", "META")

    # documents
    def _document(self, item):
        doc = _clean(item)
        for key in ("error", "strategy", "chunks", "tokens", "embed_tokens"):
            doc.setdefault(key, 0 if key in ("chunks", "tokens", "embed_tokens") else None)
        return doc

    def has_document(self, kb_id, digest):
        return any(d.get("sha256") == digest and d["status"] != "failed"
                   for d in self.t.children(f"KB#{kb_id}", "DOC#"))

    @staticmethod
    def document_item(doc_id, kb_id, name, size, digest, created_at):
        return {"PK": f"KB#{kb_id}", "SK": f"DOC#{doc_id}", "GSI1PK": f"DOC#{doc_id}", "GSI1SK": "META",
                "id": doc_id, "kb_id": kb_id, "name": name, "bytes": size, "sha256": digest,
                "status": "processing", "chunks": 0, "tokens": 0, "embed_tokens": 0, "created_at": created_at}

    def insert_document(self, kb_id, name, size, digest, created_at):
        doc_id = self.t.next_id("document")
        self.t.put(self.document_item(doc_id, kb_id, name, size, digest, created_at))
        self._document_keys[doc_id] = (f"KB#{kb_id}", f"DOC#{doc_id}")
        return doc_id

    def locate_document(self, kb_id, doc_id):
        """Use a known primary key instead of waiting for the document GSI to catch up."""
        self._document_keys[doc_id] = (f"KB#{kb_id}", f"DOC#{doc_id}")
        return self.get_document(doc_id)

    def _doc_item(self, doc_id):
        if doc_id in self._document_keys:
            return self.t.get(*self._document_keys[doc_id], consistent=True)
        items = self.t.query(Key("GSI1PK").eq(f"DOC#{doc_id}") & Key("GSI1SK").eq("META"), index="GSI1")
        return items[0] if items else None

    def get_document(self, doc_id):
        item = self._doc_item(doc_id)
        if not item:
            return None
        doc = self._document(item)
        inline, key = doc.pop("content_inline", None), doc.pop("content_key", None)
        if key and self.s3 is not None:
            doc["content"] = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")
        else:
            doc["content"] = inline
        return doc

    def documents_of(self, kb_id):
        return [self.get_document(d["id"]) for d in self.t.children(f"KB#{kb_id}", "DOC#")]

    def update_document(self, doc_id, *, add_embed_tokens=0, **fields):
        item = self._doc_item(doc_id)
        if not item:
            return
        if "content" in fields:
            text = fields.pop("content")
            data = (text or "").encode("utf-8")
            if len(data) <= MAX_INLINE_TEXT:
                fields["content_inline"] = text
            elif self.s3 is not None:
                key = f"documents/{item['kb_id']}/{doc_id}.txt"
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
                fields["content_key"] = key
            else:
                from autosde.knowledge import KnowledgeError
                raise KnowledgeError("That document's text is too large to store without a bucket.")
        remove = [k for k, v in fields.items() if v is None]
        values = {k: v for k, v in fields.items() if v is not None}
        self.t.update(item["PK"], item["SK"], values or None,
                      add={"embed_tokens": add_embed_tokens} if add_embed_tokens else None, remove=remove)

    def _chunks(self, doc_id):
        return self.t.children(f"DOC#{doc_id}", "CHUNK#")

    def replace_chunks(self, doc, pieces, vectors, *, document_fields, add_embed_tokens):
        from autosde.knowledge import pack

        self.t.delete_all(self._chunks(doc["id"]))
        with self.t.table.batch_writer() as batch:
            for item, vector in zip(pieces, vectors):
                batch.put_item(Item=to_dynamo({
                    "PK": f"DOC#{doc['id']}", "SK": f"CHUNK#{item.position:06d}",
                    "GSI1PK": f"KBCHUNKS#{doc['kb_id']}", "GSI1SK": f"{doc['id']:010d}#{item.position:06d}",
                    "id": f"{doc['id']}:{item.position}", "doc_id": doc["id"], "kb_id": doc["kb_id"],
                    "position": item.position, "heading": item.heading, "text": item.text,
                    "tokens": item.tokens, "embedding": pack(vector)}))
        self.update_document(doc["id"], add_embed_tokens=add_embed_tokens, **document_fields)
        self._loaded.pop(doc["kb_id"], None)

    def delete_document(self, doc_id):
        item = self._doc_item(doc_id)
        self.t.delete_all(self._chunks(doc_id))
        if item:
            if item.get("content_key") and self.s3 is not None:
                self.s3.delete_object(Bucket=self.bucket, Key=item["content_key"])
            self.t.delete(item["PK"], item["SK"])
            self._loaded.pop(item["kb_id"], None)

    # chunks
    @staticmethod
    def _split_id(chunk_id):
        doc_id, position = str(chunk_id).split(":")
        return int(doc_id), int(position)

    def chunk_rows(self, doc_id):
        return [{"id": c["id"], "position": c["position"], "heading": c["heading"], "text": c["text"],
                 "tokens": c["tokens"], "summary": c.get("summary")} for c in self._chunks(doc_id)]

    def set_chunk_summary(self, chunk_id, summary):
        doc_id, position = self._split_id(chunk_id)
        self.t.update(f"DOC#{doc_id}", f"CHUNK#{position:06d}", {"summary": summary})

    def set_chunk_heading(self, chunk_id, heading, text):
        doc_id, position = self._split_id(chunk_id)
        self.t.update(f"DOC#{doc_id}", f"CHUNK#{position:06d}", {"heading": heading})

    def unsummarized(self, doc_id):
        return sum(1 for c in self._chunks(doc_id) if not c.get("summary"))

    def _kb_chunks(self, kb_ids):
        items = []
        for kb_id in kb_ids:
            if kb_id not in self._loaded:
                self._loaded[kb_id] = self.t.query(Key("GSI1PK").eq(f"KBCHUNKS#{kb_id}"), index="GSI1")
            items += self._loaded[kb_id]
        return items

    def vectors(self, kb_ids):
        for c in self._kb_chunks(kb_ids):
            blob = c["embedding"]
            yield c["id"], bytes(blob.value if hasattr(blob, "value") else blob)

    def keyword(self, kb_ids, terms, limit, *, k1=1.2, b=0.75):
        """BM25 over heading and text (the same scoring FTS5 uses, without stemming)."""
        import math

        docs = [(c["id"], _WORD.findall(f"{c['heading']} {c['text']}".lower())) for c in self._kb_chunks(kb_ids)]
        if not docs:
            return []
        average = sum(len(words) for _, words in docs) / len(docs)
        wanted = list(dict.fromkeys(terms))
        frequency = {t: sum(1 for _, words in docs if t in words) for t in wanted}
        scored = []
        for cid, words in docs:
            counts = {}
            for word in words:
                if word in frequency:
                    counts[word] = counts.get(word, 0) + 1
            score = 0.0
            for term, n in counts.items():
                idf = math.log(1 + (len(docs) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * n * (k1 + 1) / (n + k1 * (1 - b + b * len(words) / average))
            if score > 0:
                scored.append((cid, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def chunks_by_ids(self, ids):
        wanted = set(ids)
        names, out = {}, {}
        for c in self._kb_chunks(list(self._loaded)):  # the bases this search just loaded
            if c["id"] not in wanted:
                continue
            if c["doc_id"] not in names:
                doc = self._doc_item(c["doc_id"]) or {}
                base = self.t.get(f"KB#{c['kb_id']}", "META") or {}
                names[c["doc_id"]] = (doc.get("name"), base.get("name"))
            document, knowledge_base = names[c["doc_id"]]
            out[c["id"]] = {"id": c["id"], "doc_id": c["doc_id"], "kb_id": c["kb_id"], "position": c["position"],
                            "heading": c["heading"], "text": c["text"], "tokens": c["tokens"],
                            "summary": c.get("summary"), "document": document, "knowledge_base": knowledge_base}
        return out


# --- history batches and GitHub repositories ---------------------------------------

class DynamoBatchStore:
    """BATCH#<id> META, listed per repository through GSI1 (REPO#<repo>#BATCHES)."""

    def __init__(self, table):
        self.t = Table(table)

    def get(self, batch_id):
        item = self.t.get(f"BATCH#{batch_id}", "META")
        return _clean(item) if item else None

    def for_repo(self, repo, limit=20):
        return [_clean(i) for i in self.t.query(Key("GSI1PK").eq(f"REPO#{repo}#BATCHES"), index="GSI1",
                                               forward=False, limit=limit)]

    def insert(self, row):
        batch_id = self.t.next_id("batch")
        self.t.put({"PK": f"BATCH#{batch_id}", "SK": "META", "GSI1PK": f"REPO#{row['repo']}#BATCHES",
                    "GSI1SK": stamp(row["created_at"], batch_id), "id": batch_id, **row})
        return batch_id

    def update(self, batch_id, **fields):
        self.t.update(f"BATCH#{batch_id}", "META", fields)


class DynamoRemoteStore:
    """REMOTE#<id> META, listed through GSI2 (REMOTES / owner/name)."""

    def __init__(self, table):
        self.t = Table(table)

    def list(self):
        rows = [_clean(i) for i in self.t.query(Key("GSI2PK").eq("REMOTES"), index="GSI2")]
        for row in rows:
            row.setdefault("error", None)
            row.setdefault("fetched_at", None)
        return rows

    def get(self, remote_id):
        item = self.t.get(f"REMOTE#{remote_id}", "META")
        if not item:
            return None
        row = _clean(item)
        row.setdefault("error", None)
        row.setdefault("fetched_at", None)
        return row

    def by_path(self, path):
        return next((r for r in self.list() if r["path"] == path), None)

    def insert(self, owner, name, url, created_at):
        remote_id = self.t.next_id("remote")
        try:
            self.t.put({"PK": f"REMOTEPATH#{url}", "SK": "UNIQUE", "remote_id": remote_id},
                       ConditionExpression="attribute_not_exists(PK)")
        except self.t.table.meta.client.exceptions.ConditionalCheckFailedException:
            from autosde.github import GitHubError
            raise GitHubError(f"{owner}/{name} is already added.") from None
        self.t.put({"PK": f"REMOTE#{remote_id}", "SK": "META", "GSI2PK": "REMOTES", "GSI2SK": f"{owner}/{name}",
                    "id": remote_id, "owner": owner, "name": name, "url": url, "path": url, "status": "ready",
                    "created_at": created_at})
        return remote_id

    def set_path(self, remote_id, url):
        self.t.update(f"REMOTE#{remote_id}", "META", {"path": url, "url": url, "status": "ready"}, remove=["error"])

    def delete(self, remote_id):
        item = self.t.get(f"REMOTE#{remote_id}", "META")
        if item:
            self.t.delete(f"REMOTEPATH#{item['path']}", "UNIQUE")
        self.t.delete(f"REMOTE#{remote_id}", "META")


# --- agent settings and prompts --------------------------------------------------

class DynamoConfigStore:
    """Agent settings and prompts, edited from the Agents page, with every version kept.

        CONFIG#agents     CURRENT / V#<n>      settings JSON
        PROMPT#<name>     CURRENT / V#<n>      prompt text (e.g. specialist.md, focus/python.md)
    """

    def __init__(self, table, *, clock=time.time):
        self.t = Table(table)
        self.clock = clock

    def _save(self, pk, field, value):
        version = self.t.update(pk, "CURRENT", {field: value, "saved_at": self.clock()},
                                add={"version": 1})["version"]
        self.t.put({"PK": pk, "SK": f"V#{version:06d}", field: value, "saved_at": self.clock()})
        return version

    def get_config(self):
        import json

        item = self.t.get("CONFIG#agents", "CURRENT")
        return json.loads(item["json"]) if item else None

    def put_config(self, config):
        import json

        return self._save("CONFIG#agents", "json", json.dumps(config))

    def get_prompt(self, name):
        item = self.t.get(f"PROMPT#{name}", "CURRENT")
        return item["text"] if item else None

    def put_prompt(self, name, text):
        return self._save(f"PROMPT#{name}", "text", text)

    def versions(self, name=None):
        pk = f"PROMPT#{name}" if name else "CONFIG#agents"
        return [{"version": int(i["SK"][2:]), "saved_at": i["saved_at"]} for i in self.t.children(pk, "V#")]
