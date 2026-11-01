import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosde.chunking import STRATEGIES, split, stats
from autosde.db import Store
from autosde.knowledge import DEFAULTS, Knowledge, KnowledgeError, settings_for, tokens
from autosde.llm import ScriptedModel
from helpers import clean_scan

DOC = """# Billing

## Totals
Prices are summed in a loop. Each price adds to the running total. The total starts at zero.
Refunds subtract from the total. Discounts apply before tax.

## Rounding
Currency is rounded with Decimal. Rounding happens once at the end. Floats are never used for money.

# Deployment
Deploys run on Tuesdays. The pipeline builds containers. Rollbacks use the previous image tag.
""" + "\n".join(f"Filler sentence number {n} about deployment pipelines and container images."
                for n in range(60))

words = lambda text: re.findall(r"\w+", text)


def settings(**change):
    return settings_for({"chunk_tokens": 100, "overlap": 0.15, **change})


class StrategyTests(unittest.TestCase):
    def test_every_strategy_stays_under_size_keeps_all_text_and_labels_headings(self):
        model = ScriptedModel({})
        for name in STRATEGIES:
            with self.subTest(strategy=name):
                pieces = split(DOC, title="doc.md", settings=settings(strategy=name), embed=model.embed)
                self.assertGreater(len(pieces), 2)
                self.assertTrue(all(p.tokens <= 110 for p in pieces), [p.tokens for p in pieces])
                covered = set(words(" ".join(p.text for p in pieces)))
                self.assertLessEqual(set(words(DOC)) - covered - {"Billing", "Totals", "Rounding", "Deployment"}, set())
                self.assertTrue(any(p.heading.startswith("doc.md > Billing") for p in pieces))
                self.assertTrue(any(p.heading == "doc.md > Deployment" for p in pieces))
                self.assertEqual([p.position for p in pieces], list(range(len(pieces))))

    def test_fixed_windows_are_even_and_overlap(self):
        text = " ".join(f"w{n}" for n in range(2000))
        pieces = split(text, title="t", settings=settings(strategy="fixed", overlap=0.2), embed=None)
        sizes = [p.tokens for p in pieces[:-1]]
        self.assertLess(max(sizes) - min(sizes), 10)
        first, second = pieces[0].text.split(), pieces[1].text.split()
        self.assertIn(second[0], first)  # the next window starts inside the previous one

    def test_sentence_chunks_end_on_sentence_boundaries(self):
        pieces = split(DOC, title="t", settings=settings(strategy="sentence", overlap=0), embed=None)
        for p in pieces:
            self.assertRegex(p.text.rstrip(), r"[.!?:a-z0-9]$")

    def test_semantic_breaks_where_the_topic_changes(self):
        topic_a = " ".join(f"Invoices and payments are reconciled monthly item {n}." for n in range(4))
        topic_b = " ".join(f"Kubernetes pods restart when probes fail item {n}." for n in range(4))
        pieces = split(f"{topic_a} {topic_b}", title="t",
                       settings=settings(strategy="semantic", chunk_tokens=400, semantic_percentile=85),
                       embed=ScriptedModel({}).embed)
        self.assertEqual(len(pieces), 2)
        self.assertNotIn("Kubernetes", pieces[0].text)
        self.assertNotIn("Invoices", pieces[1].text)

    def test_stats(self):
        pieces = split(DOC, title="t", settings=settings(strategy="recursive"), embed=None)
        summary = stats(pieces)
        self.assertEqual(summary["chunks"], len(pieces))
        self.assertLessEqual(summary["min_tokens"], summary["avg_tokens"])
        self.assertLessEqual(summary["avg_tokens"], summary["max_tokens"])


class SettingsTests(unittest.TestCase):
    def test_strategies_models_and_dimensions_are_validated(self):
        self.assertEqual(DEFAULTS["strategy"], "structure")
        with self.assertRaises(KnowledgeError):
            settings_for({"strategy": "magic"})
        with self.assertRaises(KnowledgeError):
            settings_for({"embed_model": "made-up"})
        with self.assertRaises(KnowledgeError):
            settings_for({"embed_model": "text-embedding-3-small", "dimensions": 3072})
        ada = settings_for({"embed_model": "text-embedding-ada-002"})
        self.assertEqual(ada["dimensions"], 1536)  # snapped to the model's only size
        with self.assertRaises(KnowledgeError):
            settings_for({"semantic_percentile": 20})


class LabTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "k.db")
        self.calls = []
        model = ScriptedModel({})

        def embed(texts, name, dims):
            self.calls.append((name, dims))
            return model.embed(texts, name, dims)
        self.kb = Knowledge(self.store.db, embed=embed, scan=clean_scan)
        self.base = self.kb.create("lab", settings={"chunk_tokens": 100})
        data = DOC.encode()
        self.doc = self.kb.add_document(self.base["id"], "doc.md", data)
        self.kb.ingest(self.doc, data)

    def test_preview_compares_strategies_without_storing(self):
        stored = self.kb.base(self.base["id"])["documents"][0]
        self.assertEqual((stored["strategy"], stored["has_text"]), ("structure", 1))
        fixed = self.kb.preview(self.base["id"], doc_id=self.doc, settings={"strategy": "fixed"})
        sentence = self.kb.preview(self.base["id"], text="One. Two. Three.", settings={"strategy": "sentence"})
        self.assertEqual(fixed["strategy"], "fixed")
        self.assertEqual(fixed["stats"]["chunks"], len(fixed["chunks"]))
        self.assertEqual(sentence["chunks"][0]["heading"], "pasted text")
        self.assertEqual(self.kb.base(self.base["id"])["documents"][0]["chunks"], stored["chunks"])

    def test_rechunk_rebuilds_documents_with_the_new_strategy(self):
        before = self.kb.chunks(self.doc)
        self.kb.update(self.base["id"], settings={"strategy": "fixed", "chunk_tokens": 200})
        docs, skipped = self.kb.rechunk(self.base["id"])
        self.assertEqual((len(docs), skipped), (1, []))
        self.kb.reindex(docs)
        doc = self.kb.base(self.base["id"])["documents"][0]
        self.assertEqual((doc["status"], doc["strategy"]), ("ready", "fixed"))
        self.assertNotEqual(len(self.kb.chunks(self.doc)), len(before))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0],
                         doc["chunks"])  # old keyword rows were removed

    def test_fixed_size_models_are_called_without_dimensions(self):
        base = self.kb.create("ada", settings={"embed_model": "text-embedding-ada-002"})
        doc = self.kb.add_document(base["id"], "a.md", b"# A\n\nSome text.")
        self.calls.clear()
        self.kb.ingest(doc, b"# A\n\nSome text.")
        self.assertEqual(self.calls, [("text-embedding-ada-002", None)])


class OldSettingsTests(unittest.TestCase):
    def test_bases_saved_before_strategies_still_index_and_failed_documents_retry(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "k.db")
            kb = Knowledge(store.db, embed=ScriptedModel({}).embed, scan=clean_scan)
            base = kb.create("old")
            old = {k: v for k, v in json.loads(store.db.execute(
                "SELECT settings FROM knowledge_bases").fetchone()[0]).items()
                if k not in ("strategy", "semantic_percentile")}
            store.db.execute("UPDATE knowledge_bases SET settings = ?", (json.dumps(old),))
            doc = kb.add_document(base["id"], "a.md", b"# A\n\nSome text here.")
            store.db.execute("UPDATE kb_documents SET content = ?, status = 'failed',"
                             " error = 'Embedding failed (KeyError).' WHERE id = ?",
                             ("# A\n\nSome text here.", doc))
            row = kb.retry(doc)
            kb.reindex([row])
            ready = kb.base(base["id"])["documents"][0]
            self.assertEqual((ready["status"], ready["strategy"], ready["error"]), ("ready", "structure", None))


class MigrationTests(unittest.TestCase):
    def test_older_databases_gain_document_text_and_strategy_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "old.db")
            db.execute("CREATE TABLE kb_documents (id INTEGER PRIMARY KEY, kb_id INTEGER, name TEXT,"
                       " bytes INTEGER, sha256 TEXT, status TEXT, error TEXT, chunks INTEGER,"
                       " tokens INTEGER, embed_tokens INTEGER, created_at REAL)")
            Knowledge(db, embed=None, scan=None)
            columns = {r[1] for r in db.execute("PRAGMA table_info(kb_documents)")}
            self.assertLessEqual({"content", "strategy"}, columns)
            db.close()


if __name__ == "__main__":
    unittest.main()
