import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosde import agents, git as capture_git
from autosde.db import Store
from autosde.knowledge import Knowledge, KnowledgeError, chunk, context_block, extract, tokens
from autosde.llm import ScriptedModel
from autosde.team import Team
from helpers import FINDING, PLAN, clean_scan, findings, repository, verdict

GUIDE = """# Money handling

## Totals
Always accumulate prices with += inside loops. Assigning with = keeps only the last price.

## Rounding
Round currency with Decimal, never float, and only at the end of a calculation.

# Logging
Never log customer emails or card numbers.
"""


class ChunkingTests(unittest.TestCase):
    def test_chunks_follow_headings_and_carry_their_path(self):
        pieces = chunk(GUIDE, title="guide.md", chunk_tokens=200)
        self.assertEqual([p.heading for p in pieces],
                         ["guide.md > Money handling > Totals", "guide.md > Money handling > Rounding",
                          "guide.md > Logging"])
        self.assertIn("+=", pieces[0].text)

    def test_documents_starting_below_level_one_keep_siblings_apart(self):
        pieces = chunk("## A\n\none\n\n## B\n\ntwo\n\n### B1\n\nthree\n\n# C\n\nfour", title="d")
        self.assertEqual([p.heading for p in pieces], ["d > A", "d > B", "d > B > B1", "d > C"])

    def test_long_sections_pack_to_size_with_overlap_and_keep_code_fences_whole(self):
        paragraphs = "\n\n".join(f"Paragraph {n} " + "word " * 60 for n in range(12))
        fence = "```python\n" + "\n".join(f"x{n} = {n}" for n in range(40)) + "\n```"
        pieces = chunk(f"# Big\n\n{paragraphs}\n\n{fence}", title="t", chunk_tokens=200, overlap=0.2)
        self.assertGreater(len(pieces), 3)
        self.assertTrue(all(p.tokens <= 260 for p in pieces[:-1]))
        joined = [p.text for p in pieces]
        # Overlap: each chunk starts with the tail of the one before it.
        for before, after in zip(joined[:3], joined[1:4]):
            self.assertIn(after.split("\n\n", 1)[0], before)
        self.assertEqual(sum(fence in p.text for p in pieces), 1)  # the fence was never split

    def test_code_files_split_at_definitions(self):
        code = "\n".join(f"def f{n}(x):\n" + "    y = x\n" * 30 + "    return y\n" for n in range(6))
        pieces = chunk(code, title="m.py", chunk_tokens=120, code=True)
        self.assertTrue(all(p.text.lstrip().startswith("def ") for p in pieces))

    def test_oversized_line_is_split(self):
        pieces = chunk("x" * 20000, title="t", chunk_tokens=200)
        self.assertTrue(all(tokens(p.text) <= 200 for p in pieces))
        self.assertGreaterEqual(sum(len(p.text) for p in pieces), 20000)  # nothing lost; overlap adds


def make_pdf(pages):
    """A minimal valid PDF with one line of Helvetica text per page (or none)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class PdfTests(unittest.TestCase):
    def test_pdf_pages_become_sections_that_chunk_and_cite_by_page(self):
        text = extract("handbook.pdf", make_pdf(["Accumulate totals with plus equals.",
                                                 "Round currency with Decimal."]))
        self.assertEqual(text.splitlines()[0], "## Page 1")
        self.assertIn("Round currency with Decimal.", text)
        pieces = chunk(text, title="handbook.pdf")
        self.assertEqual([p.heading for p in pieces], ["handbook.pdf > Page 1", "handbook.pdf > Page 2"])

    def test_image_only_corrupt_and_missing_pypdf_explain_themselves(self):
        with self.assertRaises(KnowledgeError) as scanned:
            extract("scan.pdf", make_pdf([""]))
        self.assertIn("OCR", str(scanned.exception))
        with self.assertRaises(KnowledgeError) as corrupt:
            extract("bad.pdf", b"%PDF-1.4 not really")
        self.assertIn("could not be read", str(corrupt.exception))
        with patch.dict("sys.modules", {"pypdf": None}):
            with self.assertRaises(KnowledgeError) as missing:
                extract("a.pdf", make_pdf(["x"]))
        self.assertIn(".[pdf]", str(missing.exception))


class ExtractTests(unittest.TestCase):
    def test_html_docx_and_refusals(self):
        page = b"<html><head><style>x{}</style></head><body><h2>Setup</h2><p>Run it.</p><script>bad()</script></body></html>"
        text = extract("page.html", page)
        self.assertIn("## Setup", text)
        self.assertNotIn("bad()", text)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml",
                             '<w:document><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                             '<w:r><w:t>Title</w:t></w:r></w:p><w:p><w:r><w:t>Body &amp; more</w:t></w:r></w:p>'
                             '</w:body></w:document>')
        self.assertEqual(extract("spec.docx", buffer.getvalue()), "# Title\n\nBody & more")
        for name, data in (("a.bin", b"\x00\x01\x02"), ("big.md", b"x" * 20_000_001)):
            with self.assertRaises(KnowledgeError):
                extract(name, data)


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = Store(Path(tmp.name) / "k.db")
        self.model = ScriptedModel({})
        self.kb = Knowledge(self.store.db, embed=self.model.embed, scan=clean_scan)
        self.base = self.kb.create("conventions", "team rules", {"chunk_tokens": 200, "dimensions": 256})

    def ingest(self, name, text, kb_id=None):
        data = text.encode()
        doc = self.kb.add_document(kb_id or self.base["id"], name, data)
        self.kb.ingest(doc, data)
        return doc

    def test_ingest_and_hybrid_search_rank_the_relevant_section_first(self):
        self.ingest("guide.md", GUIDE)
        self.ingest("other.md", "# Deploys\n\nShip on Tuesdays after the build is green.")
        base = self.kb.base(self.base["id"])
        self.assertEqual([d["status"] for d in base["documents"]], ["ready", "ready"])
        results = self.kb.search([self.base["id"]], "loop accumulate prices total")
        self.assertEqual(results[0]["heading"], "guide.md > Money handling > Totals")
        self.assertTrue(results[0]["keyword"])
        self.assertIsNotNone(results[0]["vector"])
        self.assertIn("[1] guide.md > Money handling > Totals", context_block(results))

    def test_duplicate_uploads_are_refused_but_failed_ones_can_be_retried(self):
        self.ingest("guide.md", GUIDE)
        with self.assertRaises(KnowledgeError):
            self.kb.add_document(self.base["id"], "copy.md", GUIDE.encode())
        broken = Knowledge(self.store.db, embed=self.model.embed, scan=lambda text: ["rule"])
        doc = broken.add_document(self.base["id"], "n.md", b"# N\n\nnote")
        broken.ingest(doc, b"# N\n\nnote")
        self.ingest("n.md", "# N\n\nnote")  # a failed upload doesn't block a retry

    def test_flagged_documents_are_not_embedded_and_deletes_clean_up(self):
        flagging = Knowledge(self.store.db, embed=self.model.embed,
                             scan=lambda text: ["generic-api-key"] if "private-sentinel" in text else [])
        data = b"token = private-sentinel"
        doc = flagging.add_document(self.base["id"], "secrets.md", data)
        before = getattr(self.model, "embedded", 0)
        flagging.ingest(doc, data)
        failed = self.kb.base(self.base["id"])["documents"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("private-sentinel", failed["error"])
        self.assertEqual(getattr(self.model, "embedded", 0), before)
        good = self.ingest("guide.md", GUIDE)
        self.kb.delete_document(good)
        self.assertEqual(self.kb.search([self.base["id"]], "prices"), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0], 0)

    def test_embedding_failures_record_the_error_class_only(self):
        def broken(texts, model, dims):
            raise RuntimeError("private-sentinel")
        kb = Knowledge(self.store.db, embed=broken, scan=clean_scan)
        doc = kb.add_document(self.base["id"], "g.md", b"# A\n\ntext")
        kb.ingest(doc, b"# A\n\ntext")
        error = kb.base(self.base["id"])["documents"][0]["error"]
        self.assertEqual(error, "Embedding failed (RuntimeError).")

    def test_settings_are_validated_and_the_model_locks_once_embedded(self):
        with self.assertRaises(KnowledgeError):
            self.kb.create("bad", settings={"chunk_tokens": 5})
        with self.assertRaises(KnowledgeError):
            self.kb.create("conventions")
        self.ingest("guide.md", GUIDE)
        self.kb.update(self.base["id"], settings={"top_k": 3})
        with self.assertRaises(KnowledgeError):
            self.kb.update(self.base["id"], settings={"dimensions": 128})

    def test_specialists_with_knowledge_get_reference_material(self):
        self.ingest("guide.md", GUIDE)
        files = capture_git.capture(repository(self.tmp), "HEAD~1..HEAD").files
        config = agents.validate({"agents": {"python": {"knowledge": [self.base["id"]]}}})
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        Team(model, config=config, retriever=lambda ids, q: self.kb.search(ids, q)).run(files)
        prompt = next(c["prompt"] for c in model.calls if c["role"] == "python")
        self.assertTrue(prompt.startswith("=== Reference material from knowledge bases"))
        self.assertIn("Always accumulate prices", prompt)
        plain = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        Team(plain, config=agents.validate({"agents": {}}),
             retriever=lambda ids, q: self.kb.search(ids, q)).run(files)
        self.assertNotIn("Reference material", next(c["prompt"] for c in plain.calls if c["role"] == "python"))


class KnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from autosde.web import App, handler_for
        from helpers import settings

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prompts = Path(tmp.name) / "prompts"
        shutil.copytree(agents.PROMPTS, prompts, ignore=shutil.ignore_patterns("agents.json"))
        patcher = patch.object(agents, "PROMPTS", prompts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)
        model = ScriptedModel({"chat": [{"answer": "Use +=. [1]"}]})
        self.model = model
        self.app = App(self.store, self.settings, model_factory=lambda: model, port=0,
                       browse_root=tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.app.port = server.server_address[1]
        self.url = f"http://127.0.0.1:{self.app.port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        scan = patch("autosde.web.scanner", return_value=clean_scan)
        scan.start()
        self.addCleanup(scan.stop)

    def call(self, path, body=None):
        import urllib.request

        headers = {"Origin": self.url}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers |= {"Content-Type": "application/json", "X-AutoSDE-Token": self.app.token}
        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_concurrent_deletes_and_reads_never_lose_the_base(self):
        import base64
        from concurrent.futures import ThreadPoolExecutor

        base = self.call("/api/knowledge", {"name": "busy"})
        ids = []
        for n in range(6):
            data = base64.b64encode(f"# Note {n}\n\nbody {n}".encode()).decode()
            ids.append(self.call(f"/api/knowledge/{base['id']}/documents", {"name": f"n{n}.md", "data": data})["id"])
        with ThreadPoolExecutor(8) as pool:
            jobs = [pool.submit(self.call, f"/api/knowledge/documents/{i}/delete", {}) for i in ids]
            jobs += [pool.submit(self.call, f"/api/knowledge/{base['id']}") for _ in range(30)]
            results = [job.result() for job in jobs]  # a 400 would raise here
        self.assertTrue(all("name" in r for r in results[len(ids):]))
        self.assertEqual(self.call(f"/api/knowledge/{base['id']}")["documents"], [])

    def test_bases_work_without_gitleaks_until_a_document_is_embedded(self):
        import base64
        import threading

        from autosde.scan import ScanUnavailable

        with patch("autosde.web.scanner", side_effect=ScanUnavailable("gitleaks missing")):
            base = self.call("/api/knowledge", {"name": "no-scanner"})
            self.assertEqual(self.call("/api/knowledge")[0]["name"], "no-scanner")
            self.call(f"/api/knowledge/{base['id']}/documents",
                      {"name": "g.md", "data": base64.b64encode(b"# A\n\ntext").decode()})
            for _ in range(50):
                doc = self.call(f"/api/knowledge/{base['id']}")["documents"][0]
                if doc["status"] != "processing":
                    break
                threading.Event().wait(0.1)
        self.assertEqual(doc["status"], "failed")
        self.assertIn("AUTOSDE_GITLEAKS", doc["error"])

    def test_create_upload_search_and_chat_with_reference(self):
        import base64
        import threading

        base = self.call("/api/knowledge", {"name": "conventions"})
        upload = self.call(f"/api/knowledge/{base['id']}/documents",
                           {"name": "guide.md", "data": base64.b64encode(GUIDE.encode()).decode()})
        self.assertEqual(upload["status"], "processing")
        for _ in range(50):
            docs = self.call(f"/api/knowledge/{base['id']}")["documents"]
            if docs[0]["status"] != "processing":
                break
            threading.Event().wait(0.1)
        self.assertEqual(docs[0]["status"], "ready")
        chunks = self.call(f"/api/knowledge/documents/{docs[0]['id']}/chunks")["chunks"]
        self.assertEqual(len(chunks), docs[0]["chunks"])
        found = self.call("/api/knowledge/search", {"kb_ids": [base["id"]], "query": "rounding decimal"})
        self.assertIn("Rounding", found["results"][0]["heading"])

        agents.save_config({"agents": {"chat": {"knowledge": [base["id"]]}}})
        self.call("/api/chat", {"message": "How should totals be accumulated?"})
        sent = json.loads(self.model.calls[-1]["prompt"])
        self.assertTrue(sent["reference"])
        self.assertIn("guide.md", sent["reference"][0]["source"])
        self.call(f"/api/knowledge/{base['id']}/delete", {})
        self.assertEqual(self.call("/api/knowledge"), [])


if __name__ == "__main__":
    unittest.main()
