"""Private S3 staging and durable, owner-bound direct-upload tickets (AWS only)."""
import base64
import hashlib
import re
import time
import uuid

from autosde.knowledge import KnowledgeError, MAX_BYTES, document_metadata

UPLOAD_SECONDS = 600


class S3Uploads:
    def __init__(self, s3, bucket, table=None, *, clock=time.time):
        self.s3, self.bucket, self.table, self.clock = s3, bucket, table, clock

    def put(self, doc_id, data):
        key = f"uploads/{doc_id}-{uuid.uuid4().hex}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
        return key

    def get(self, key, version=None):
        options = {"VersionId": version} if version else {}
        response = self.s3.get_object(Bucket=self.bucket, Key=key, **options)
        try:
            data = response["Body"].read(MAX_BYTES + 1)
        finally:
            response["Body"].close()
        if len(data) > MAX_BYTES:
            raise KnowledgeError("Files must be under 20 MB.")
        return data

    def delete(self, key, version=None):
        options = {"VersionId": version} if version else {}
        self.s3.delete_object(Bucket=self.bucket, Key=key, **options)

    def _ticket(self, upload_id):
        if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise KnowledgeError("No such upload.")
        return self.table.get_item(Key={"PK": f"UPLOAD#{upload_id}", "SK": "META"},
                                   ConsistentRead=True).get("Item")

    def initiate(self, kb, kb_id, owner, name, size, digest):
        kb.base(kb_id)
        name = document_metadata(name, size, digest)
        if kb.store.has_document(kb_id, digest):
            raise KnowledgeError("That document is already in this knowledge base.")
        upload_id = uuid.uuid4().hex
        key = f"uploads/direct/{upload_id}"
        now = int(self.clock())
        ticket = {"PK": f"UPLOAD#{upload_id}", "SK": "META", "id": upload_id,
                  "kb_id": kb_id, "owner": owner, "name": name, "bytes": size, "sha256": digest,
                  "key": key, "state": "pending", "expires": now + UPLOAD_SECONDS, "ttl": now + 86400}
        checksum = base64.b64encode(bytes.fromhex(digest)).decode()
        fields = {"Content-Type": "application/octet-stream", "x-amz-server-side-encryption": "AES256",
                  "x-amz-checksum-algorithm": "SHA256", "x-amz-checksum-sha256": checksum,
                  "x-amz-meta-upload-id": upload_id}
        form = self.s3.generate_presigned_post(Bucket=self.bucket, Key=key, Fields=fields,
            Conditions=[{field: value} for field, value in fields.items()] + [["content-length-range", size, size]],
            ExpiresIn=UPLOAD_SECONDS)
        # Persist before returning a capability; no file contents or signing credentials are stored.
        self.table.put_item(Item=ticket, ConditionExpression="attribute_not_exists(PK)")
        return {"upload_id": upload_id, "url": form["url"], "fields": form["fields"], "expires_in": UPLOAD_SECONDS}

    def complete(self, kb, kb_id, owner, upload_id, jobs):
        from botocore.exceptions import ClientError
        from autosde.dynamo import to_dynamo

        ticket = self._ticket(upload_id)
        if not ticket or ticket["owner"] != owner or ticket["kb_id"] != kb_id:
            raise KnowledgeError("No such upload.")
        kb.base(kb_id)
        if ticket["state"] == "pending":
            if ticket["expires"] < self.clock():
                raise KnowledgeError("Upload expired. Choose the file again.")
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=ticket["key"], ChecksumMode="ENABLED")
            except ClientError:
                raise KnowledgeError("Upload is not available. Finish uploading the file first.") from None
            checksum = base64.b64encode(bytes.fromhex(ticket["sha256"])).decode()
            version = head.get("VersionId")
            if (head["ContentLength"] != ticket["bytes"] or head.get("ChecksumSHA256") != checksum
                    or head.get("Metadata", {}).get("upload-id") != upload_id
                    or not version or version == "null"):
                raise KnowledgeError("Uploaded file does not match its size, checksum or upload ticket.")
            if kb.store.has_document(kb_id, ticket["sha256"]):
                # Another completion of this same ticket may have won while HEAD was in flight.
                if self._ticket(upload_id)["state"] != "pending":
                    return self.complete(kb, kb_id, owner, upload_id, jobs)
                raise KnowledgeError("That document is already in this knowledge base.")
            doc_id = kb.store.t.next_id("document")
            item = kb.store.document_item(doc_id, kb_id, ticket["name"], int(ticket["bytes"]),
                                          ticket["sha256"], self.clock())
            try:
                self.table.meta.client.transact_write_items(TransactItems=[
                    {"Put": {"TableName": self.table.name, "Item": to_dynamo(item),
                             "ConditionExpression": "attribute_not_exists(PK)"}},
                    {"Update": {"TableName": self.table.name,
                        "Key": {"PK": ticket["PK"], "SK": "META"},
                        "UpdateExpression": "SET #state = :ready, doc_id = :doc, version_id = :version",
                        "ConditionExpression": "#state = :pending AND expires >= :now",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {":ready": "ready", ":pending": "pending", ":doc": doc_id,
                                                      ":version": version, ":now": int(self.clock())}}},
                ])
            except ClientError as error:
                if error.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                current = self._ticket(upload_id)
                if not current or current["state"] == "pending":
                    raise KnowledgeError("Upload could not be completed. Try again.") from None
            ticket = self._ticket(upload_id)
        if ticket["state"] == "ready":
            # Retrying a lost completion response can queue again; the worker's conditional claim
            # prevents it from scanning or embedding the same document twice.
            jobs.submit("ingest", upload_ticket=upload_id)
        return {"id": int(ticket["doc_id"]), "status": "processing"}

    def ingest(self, kb, upload_id):
        from botocore.exceptions import ClientError
        ticket = self._ticket(upload_id)
        if not ticket or ticket["state"] != "ready":
            return
        try:
            self.table.update_item(Key={"PK": ticket["PK"], "SK": "META"},
                UpdateExpression="SET #state = :processing", ConditionExpression="#state = :ready",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":processing": "processing", ":ready": "ready"})
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return
            raise
        doc_id = int(ticket["doc_id"])
        doc = None
        try:
            doc = kb.store.locate_document(int(ticket["kb_id"]), doc_id)
            if doc:
                data = self.get(ticket["key"], ticket["version_id"])
                if len(data) != ticket["bytes"] or hashlib.sha256(data).hexdigest() != ticket["sha256"]:
                    raise KnowledgeError("Uploaded file failed checksum verification.")
                kb.ingest(doc_id, data)
        except Exception:
            if doc:
                kb.store.update_document(doc_id, status="failed", error="Could not process the verified upload. Upload it again.")
        finally:
            try:
                self.delete(ticket["key"], ticket["version_id"])
            finally:
                self.table.update_item(Key={"PK": ticket["PK"], "SK": "META"},
                    UpdateExpression="SET #state = :done", ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={":done": "done"})
