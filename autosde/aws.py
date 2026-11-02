"""AWS Lambda entry points: `api` behind API Gateway (HTTP API, Cognito JWT authorizer) and
`worker` for background jobs, both on the same application code as the local server.

Environment (set by the CDK stack; none of these are secrets):
    TABLE_NAME       DynamoDB table
    BUCKET_NAME      S3 bucket for document text and uploads in transit
    SSM_PREFIX       Parameter Store prefix with settings and secret names, e.g. /autosde/dev
    WORKER_FUNCTION  worker function name (the api function invokes it)
    GITLEAKS_PATH    gitleaks binary packaged with the functions
"""

import base64
import json
import os

from autosde.api import body_limit, dispatch, json_response
from autosde.uploads import S3Uploads

_APP = None
# JSON uploads carry base64; leave room under the 6 MB Lambda invocation envelope.
CLOUD_UPLOAD_LIMIT = 4_100_000

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def build_app():
    import boto3
    from botocore.config import Config

    from autosde.app import App
    from autosde.cloud import Cloud
    from autosde.jobs import LambdaJobs

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    bucket = os.environ["BUCKET_NAME"]
    cloud = Cloud(table=boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]), ssm=boto3.client("ssm"),
                  secretsmanager=boto3.client("secretsmanager"), prefix=os.environ["SSM_PREFIX"],
                  s3=s3, bucket=bucket, gitleaks=os.environ.get("GITLEAKS_PATH"))
    return App(cloud.store, cloud.settings, model_factory=cloud.model, github_client=cloud.github_client(),
               knowledge_store=cloud.knowledge_store, remote_store=cloud.remote_store,
               batch_store=cloud.batch_store, uploads=S3Uploads(s3, bucket, cloud.table), cloud=True,
               jobs=LambdaJobs(boto3.client("lambda"), os.environ["WORKER_FUNCTION"]))


def app():
    """One application per Lambda container (settings and clients are reused across calls)."""
    global _APP
    if _APP is None:
        _APP = build_app()
    return _APP


def _reply(response):
    return {"statusCode": response.status, "headers": {"Content-Type": response.content_type, **HEADERS,
                                                       **response.headers},
            "body": response.body.decode("utf-8"), "isBase64Encoded": False}


def api(event, context=None):
    """API Gateway HTTP API (payload 2.0) -> the shared router."""
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    claims = (event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}) or {}).get("claims")
    if not claims:  # API Gateway's authorizer should already have refused this
        return _reply(json_response(401, {"error": "Sign in first."}))
    body = None
    if method == "POST":
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        if headers.get("content-type", "").split(";")[0] != "application/json":
            return _reply(json_response(415, {"error": "JSON required."}))
        raw = event.get("body") or ""
        raw = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
        if len(raw) > min(body_limit(path), CLOUD_UPLOAD_LIMIT):
            return _reply(json_response(413, {"error": "Too large."}))
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _reply(json_response(400, {"error": "Invalid JSON."}))
    query = event.get("queryStringParameters") or {}
    try:
        return _reply(dispatch(app(), method, path, query, body, user=claims))
    except Exception as error:  # never echo internals; the class name is enough to debug
        print(json.dumps({"event": "api_error", "path": path, "error": type(error).__name__}), flush=True)
        return _reply(json_response(500, {"error": "Something went wrong."}))


def worker(event, context=None):
    """Asynchronous invocations from the api function (and batch steps re-invoking themselves)."""
    name = event.get("job")
    try:
        app().run_job(name, event.get("args") or {})
        outcome = "ok"
    except Exception as error:
        outcome = type(error).__name__
    print(json.dumps({"event": "job", "job": name, "outcome": outcome}), flush=True)
    return {"job": name, "outcome": outcome}
