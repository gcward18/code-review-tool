"""DynamoDB knowledge bases behave like the SQLite ones for the same documents."""

import os
import tempfile
import unittest
from pathlib import Path

import boto3
from moto import mock_aws

from autosde.db import Store
from autosde.dynamo import DynamoKnowledgeStore, create_table
from autosde.knowledge import Knowledge, KnowledgeError
from autosde.llm import ScriptedModel
from helpers import clean_scan

GUIDE = """# Money handling

## Totals
Always accumulate prices with += inside loops. Assigning with = keeps only the last price.

## Rounding
Round currency with Decimal, never float, and only at the end of a calculation.

# Logging
Never log customer emails or card numbers.
"""
NOTES = "# Deploys\n\nShip on Tuesdays after the build is green.\n"
VOLATILE = {"created_at", "id", "doc_id", "kb_id"}


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


@mock_aws
class DynamoKnowledgeParityTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing",
                          AWS_SECRET_ACCESS_KEY="testing")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        embed = ScriptedModel({}).embed
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket="autosde-docs")
        table = create_table(boto3.resource("dynamodb"), "autosde-test")
        self.kbs = {
            "sqlite": Knowledge(Store(Path(tmp.name) / "s.db").db, embed=embed, scan=clean_scan),
            "dynamo": Knowledge(DynamoKnowledgeStore(table, s3=self.s3, bucket="autosde-docs"),
                                embed=embed, scan=clean_scan),
        }
        self.ids = {}
        for name, kb in self.kbs.items():
            base = kb.create("conventions", "team rules", {"chunk_tokens": 200, "dimensions": 256})
            docs = []
            for title, text in (("guide.md", GUIDE), ("notes.md", NOTES)):
                doc = kb.add_document(base["id"], title, text.encode())
                kb.ingest(doc, text.encode())
                docs.append(doc)
            self.ids[name] = (base["id"], docs)

    def both(self, call):
        return [strip(call(self.kbs[n], *self.ids[n])) for n in ("dynamo", "sqlite")]

    def test_bases_documents_and_chunks_match(self):
        dynamo, sqlite = self.both(lambda kb, base, docs: kb.base(base))
        self.assertEqual(dynamo, sqlite)
        dynamo, sqlite = self.both(lambda kb, base, docs: kb.chunks(docs[0]))
        self.assertEqual(dynamo, sqlite)
        self.assertEqual([d["status"] for d in self.kbs["dynamo"].base(self.ids["dynamo"][0])["documents"]],
                         ["ready", "ready"])

    def test_search_ranks_the_same_section_first(self):
        for query in ("loop accumulate prices total", "rounding decimal currency", "tuesday deploys"):
            dynamo, sqlite = self.both(lambda kb, base, docs, q=query: kb.search([base], q))
            self.assertEqual(dynamo[0]["heading"], sqlite[0]["heading"], query)
            self.assertTrue(dynamo[0]["keyword"])

    def test_summaries_rename_rechunk_duplicates_and_delete(self):
        kb, (base, docs) = self.kbs["dynamo"], self.ids["dynamo"]
        complete = lambda payload, schema: ({"summaries": [{"n": 1, "summary": "Totals rule."}]}, (3, 2))
        self.assertEqual(kb.summarize(docs[0], complete, positions=[0])["summarized"], 1)
        self.assertEqual(kb.chunks(docs[0])[0]["summary"], "Totals rule.")
        self.assertEqual(kb.rename(docs[0], "money")["name"], "money.md")
        self.assertTrue(all(c["heading"].startswith("money.md") for c in kb.chunks(docs[0])))
        with self.assertRaises(KnowledgeError):
            kb.add_document(base, "copy.md", GUIDE.encode())
        with self.assertRaises(KnowledgeError):
            kb.create("conventions")
        kb.update(base, settings={"strategy": "fixed"})
        ready, skipped = kb.rechunk(base)
        kb.reindex(ready)
        self.assertEqual(skipped, [])
        self.assertTrue(all(d["strategy"] == "fixed" for d in kb.base(base)["documents"]))
        kb.delete_document(docs[1])
        self.assertEqual(len(kb.base(base)["documents"]), 1)
        kb.delete(base)
        self.assertEqual(kb.bases(), [])
        kb.create("conventions")  # the name is free again

    def test_large_document_text_goes_to_s3(self):
        kb, (base, _) = self.kbs["dynamo"], self.ids["dynamo"]
        big = ("# Section\n\n" + "word " * 80_000).encode()
        doc = kb.add_document(base, "big.md", big)
        kb.ingest(doc, big)
        stored = kb.store.get_document(doc)
        self.assertEqual(stored["status"], "ready")
        self.assertEqual(len(stored["content"]), len(big.decode()))
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket="autosde-docs").get("Contents", [])]
        self.assertEqual(keys, [f"documents/{base}/{doc}.txt"])
        kb.delete_document(doc)
        self.assertNotIn("Contents", self.s3.list_objects_v2(Bucket="autosde-docs"))


if __name__ == "__main__":
    unittest.main()
