"""Direct upload tickets are durable, bounded and consumed once before embedding."""
import base64
import hashlib
import json
import unittest
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from autosde.dynamo import DynamoKnowledgeStore, create_table
from autosde.knowledge import Knowledge, KnowledgeError, MAX_BYTES
from autosde.uploads import S3Uploads


class Jobs:
    def __init__(self):
        self.calls = []

    def submit(self, name, **args):
        self.calls.append((name, args))


@mock_aws
class DirectUploadsTests(unittest.TestCase):
    def setUp(self):
        self.table = create_table(boto3.resource('dynamodb', region_name='us-east-1'), 'uploads-test')
        self.s3 = boto3.client('s3', region_name='us-east-1')
        self.bucket = 'uploads-test-documents'
        self.s3.create_bucket(Bucket=self.bucket)
        self.s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={'Status': 'Enabled'})
        self.scan = unittest.mock.Mock(return_value=[])
        self.embed = unittest.mock.Mock(side_effect=lambda texts, model, dims: ([[0.0] * dims for _ in texts], 3))
        self.kb = Knowledge(DynamoKnowledgeStore(self.table, s3=self.s3, bucket=self.bucket),
                            embed=self.embed, scan=self.scan)
        self.base = self.kb.create('conventions')['id']
        self.now = 1000
        self.uploads = S3Uploads(self.s3, self.bucket, self.table, clock=lambda: self.now)
        self.jobs = Jobs()
        self.data = b'# Rules\nKeep functions focused.\n'

    def start(self, data=None, owner='user-1', kb_id=None, **changes):
        data = self.data if data is None else data
        args = {'name': 'rules.md', 'size': len(data), 'digest': hashlib.sha256(data).hexdigest()}
        return self.uploads.initiate(self.kb, kb_id or self.base, owner, **(args | changes))

    def put(self, form, data=None, **changes):
        data = self.data if data is None else data
        fields = form['fields']
        options = {'Bucket': self.bucket, 'Key': fields['key'], 'Body': data,
                   'Metadata': {'upload-id': form['upload_id']},
                   'ChecksumAlgorithm': 'SHA256',
                   'ChecksumSHA256': base64.b64encode(hashlib.sha256(data).digest()).decode(),
                   'ServerSideEncryption': 'AES256'}
        return self.s3.put_object(**(options | changes))

    def finish(self, form, owner='user-1', kb_id=None):
        return self.uploads.complete(self.kb, kb_id or self.base, owner, form['upload_id'], self.jobs)

    def test_policy_is_short_lived_and_binds_one_key_size_checksum_and_encryption(self):
        form = self.start()
        self.assertEqual(form['expires_in'], 600)
        policy = json.loads(base64.b64decode(form['fields']['policy']))
        conditions = policy['conditions']
        for condition in [
            ['content-length-range', len(self.data), len(self.data)],
            {'key': form['fields']['key']}, {'x-amz-server-side-encryption': 'AES256'},
            {'x-amz-checksum-sha256': base64.b64encode(hashlib.sha256(self.data).digest()).decode()},
        ]:
            self.assertIn(condition, conditions)
        self.assertTrue(form['fields']['key'].startswith('uploads/direct/'))
        self.assertFalse(self.jobs.calls)
        self.assertEqual(self.kb.base(self.base)['documents'], [])
        ticket = self.uploads._ticket(form['upload_id'])
        self.assertNotIn('fields', ticket)
        self.assertNotIn('data', ticket)
        self.assertEqual(ticket['ttl'], self.now + 86400)

    def test_restart_completion_and_duplicate_workers_create_and_embed_only_one_document(self):
        form = self.start()
        version = self.put(form)['VersionId']
        # All state is in DynamoDB/S3, so a new API or worker instance has what it needs.
        self.uploads = S3Uploads(self.s3, self.bucket, self.table, clock=lambda: self.now)
        first = self.finish(form)
        second = self.finish(form)
        self.assertEqual(first, second)
        self.assertEqual(len(self.kb.base(self.base)['documents']), 1)
        self.uploads.ingest(self.kb, form['upload_id'])
        self.uploads.ingest(self.kb, form['upload_id'])
        self.assertEqual(self.kb.store.get_document(first['id'])['status'], 'ready')
        self.assertEqual(self.scan.call_count, 1)
        self.assertEqual(self.embed.call_count, 1)
        with self.assertRaises(ClientError):
            self.s3.head_object(Bucket=self.bucket, Key=form['fields']['key'], VersionId=version)

    def test_file_overwritten_after_completion_does_not_change_the_version_processed(self):
        form = self.start()
        self.put(form)
        started = self.finish(form)
        self.put(form, data=b'changed bytes')
        self.uploads.ingest(self.kb, form['upload_id'])
        self.assertEqual(self.kb.store.get_document(started['id'])['status'], 'ready')
        self.assertEqual(self.scan.call_args.args[0], self.data.decode())

    def test_large_file_bytes_bypass_the_api_and_worker_job_payload(self):
        data = b"notes\n" + b" " * 4_000_000
        form = self.start(data=data)
        self.put(form, data=data)
        started = self.finish(form)
        self.assertEqual(self.kb.store.get_document(started['id'])['bytes'], len(data))
        self.assertLess(len(json.dumps(self.jobs.calls)), 200)
        with patch.object(self.kb, 'ingest') as ingest:
            self.uploads.ingest(self.kb, form['upload_id'])
            self.assertEqual(len(ingest.call_args.args[1]), len(data))

    def test_racing_completion_transactions_cannot_create_two_documents(self):
        form = self.start()
        self.put(form)
        original = self.table.meta.client.transact_write_items
        racing = True
        def interleave(**kwargs):
            nonlocal racing
            if racing:
                racing = False
                self.finish(form)
            return original(**kwargs)
        with patch.object(self.table.meta.client, 'transact_write_items', side_effect=interleave):
            started = self.finish(form)
        self.assertEqual(len(self.kb.base(self.base)['documents']), 1)
        self.assertEqual(self.kb.base(self.base)['documents'][0]['id'], started['id'])

    def test_owner_base_expiry_and_missing_object_are_rejected_before_queuing(self):
        form = self.start()
        other = self.kb.create('other')['id']
        for args in [{'owner': 'different-user'}, {'kb_id': other}]:
            with self.assertRaisesRegex(KnowledgeError, 'No such upload'):
                self.finish(form, **args)
        with self.assertRaisesRegex(KnowledgeError, 'not available'):
            self.finish(form)
        self.now += 601
        with self.assertRaisesRegex(KnowledgeError, 'expired'):
            self.finish(form)
        self.assertFalse(self.jobs.calls)

    def test_mismatched_size_checksum_or_ticket_metadata_cannot_create_a_document(self):
        for changes in [{'data': b'incorrect'}, {'Metadata': {'upload-id': 'wrong'}},
                        {'ChecksumSHA256': base64.b64encode(b'x' * 32).decode()}]:
            form = self.start()
            self.put(form, **changes)
            with self.assertRaisesRegex(KnowledgeError, 'does not match'):
                self.finish(form)
        self.assertEqual(self.kb.base(self.base)['documents'], [])
        self.assertFalse(self.jobs.calls)

    def test_server_rejects_oversize_and_malformed_metadata(self):
        for changes in [{'size': MAX_BYTES + 1}, {'size': True}, {'size': -1},
                        {'digest': 'not-sha256'}, {'name': ['invalid']}]:
            with self.assertRaises(KnowledgeError):
                self.start(**changes)
        self.assertFalse(self.jobs.calls)

    def test_secret_scan_failure_prevents_embedding_and_removes_temporary_version(self):
        form = self.start()
        self.put(form)
        started = self.finish(form)
        self.scan.return_value = ['flagged']
        self.uploads.ingest(self.kb, form['upload_id'])
        self.assertEqual(self.kb.store.get_document(started['id'])['status'], 'failed')
        self.embed.assert_not_called()
        self.assertEqual(self.s3.list_objects_v2(Bucket=self.bucket).get('KeyCount'), 0)

    def test_queue_failure_can_retry_completion_without_registering_a_second_document(self):
        form = self.start()
        self.put(form)
        with patch.object(self.jobs, 'submit', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                self.finish(form)
        started = self.finish(form)
        self.assertEqual(len(self.kb.base(self.base)['documents']), 1)
        self.uploads.ingest(self.kb, form['upload_id'])
        self.assertEqual(self.kb.store.get_document(started['id'])['status'], 'ready')
