"""Model clients: OpenAI Responses API, and a scripted stand-in for tests and notebooks."""

import hashlib
import json
import re
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.openai.com/v1/responses"
MODELS_API = "https://api.openai.com/v1/models"
EMBEDDINGS_API = "https://api.openai.com/v1/embeddings"
# Model families that cannot answer a text Responses call.
_NOT_TEXT = ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search",
             "moderation", "dall-e", "whisper", "sora", "live")


@dataclass
class Reply:
    text: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self):
        return self.error is None and self.text is not None


def _key_from_secrets_manager(secret_id, profile, region):
    """Read the key on demand; never stored in os.environ, never in error messages."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            "Reading the key from Secrets Manager needs boto3: pip install -e '.[aws]'"
        ) from None
    try:
        raw = (
            boto3.Session(profile_name=profile, region_name=region)
            .client("secretsmanager")
            .get_secret_value(SecretId=secret_id)["SecretString"]
        )
        return json.loads(raw)["api_key"] if raw.lstrip().startswith("{") else raw.strip()
    except Exception:
        raise RuntimeError("OpenAI key could not be read from Secrets Manager") from None


def reasons(model):
    """Only reasoning models (o-series, gpt-5 and later) accept the `reasoning` field."""
    if re.match(r"o\d", model):
        return True
    version = re.match(r"gpt-(\d+)", model)
    return bool(version) and int(version[1]) >= 5


class OpenAIModel:
    """One HTTPS attempt per call; responses are not stored by OpenAI (`store: false`)."""

    def __init__(
        self,
        model,
        *,
        api_key=None,
        reasoning_effort="low",
        timeout=120,
        secret_id=None,  # e.g. "autosde/openai-api-key"; None = no AWS lookup
        aws_profile=None,  # e.g. "dev"
        aws_region="us-east-1",
    ):
        self.model, self.reasoning_effort, self.timeout = model, reasoning_effort, timeout
        self._key = (
            api_key
            or os.environ.get("OPENAI_API_KEY")
            or (secret_id and _key_from_secrets_manager(secret_id, aws_profile, aws_region))
        )
        if not self._key:
            raise RuntimeError(
                "Set OPENAI_API_KEY, pass api_key, or configure secret_id to run reviews"
            )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    @classmethod
    def from_settings(cls, settings):
        """The one way the CLI, web app and notebook build the model."""
        return cls(
            settings.model,
            reasoning_effort=settings.reasoning_effort,
            secret_id=settings.secret_id,
            aws_profile=settings.aws_profile,
            aws_region=settings.aws_region,
        )

    def __repr__(self):
        return f"OpenAIModel(model={self.model!r}, api_key=<redacted>)"

    def embed(self, texts, model, dimensions=None):
        """(vectors, tokens) for `texts` in input order; one attempt, no retries."""
        body = {"model": model, "input": list(texts)}
        if dimensions:
            body["dimensions"] = dimensions
        request = urllib.request.Request(
            EMBEDDINGS_API, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read(50_000_000))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Embedding request failed (HTTP {error.code})") from None
        except Exception as error:
            raise RuntimeError(f"Embedding request failed ({type(error).__name__})") from None
        rows = sorted(data.get("data") or [], key=lambda row: row["index"])
        return [row["embedding"] for row in rows], int((data.get("usage") or {}).get("total_tokens") or 0)

    def models(self):
        """Text model ids this key can use (a free listing call), newest names first."""
        request = urllib.request.Request(MODELS_API,
                                         headers={"Authorization": "Bearer " + self._key})
        try:
            with self._opener.open(request, timeout=15) as response:
                data = json.loads(response.read(2_000_000))
        except Exception:
            raise RuntimeError("Model list unavailable") from None
        ids = {m.get("id", "") for m in data.get("data") or []}
        return sorted((i for i in ids if (i.startswith("gpt-") or i[:2] in ("o1", "o3", "o4"))
                       and not any(word in i for word in _NOT_TEXT)),
                      key=lambda i: (i.startswith("gpt-"), i), reverse=True)

    def complete(self, *, role, system, prompt, schema, max_output_tokens, model=None):
        body = {
            "model": model or self.model,
            "instructions": system,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ],
            "max_output_tokens": max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": role.replace("-", "_"),
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        if self.reasoning_effort and reasons(body["model"]):
            body["reasoning"] = {"effort": self.reasoning_effort}
        request = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self._key,
                "Content-Type": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read(2_000_000))
        except urllib.error.HTTPError as error:
            code = None
            try:
                code = (json.loads(error.read(65536)).get("error") or {}).get("code")
            except Exception:
                pass
            return Reply(None, error=f"HTTP {error.code} {code or ''}".strip(),
                         seconds=time.monotonic() - started)
        except Exception as error:
            return Reply(None, error=type(error).__name__, seconds=time.monotonic() - started)
        usage = data.get("usage") or {}
        reply = Reply(
            None,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            seconds=time.monotonic() - started,
        )
        if data.get("status") != "completed":
            reason = (data.get("incomplete_details") or {}).get("reason")
            reply.error = f"{data.get('status')} {reason or ''}".strip()
            return reply
        parts = [
            part.get("text", "")
            for item in data.get("output") or []
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        ]
        reply.text = "".join(parts) or None
        if reply.text is None:
            reply.error = "empty reply"
        return reply


class ScriptedModel:
    """Replies per role from a script; records every call. Free and deterministic.

    script: {"coordinator": [reply, ...], "python": [...], "aggregator": [...]}
    A reply is a dict (sent as JSON) or a string; a missing reply is an error.
    """

    model = "scripted"

    def __init__(self, script):
        self.script = {role: list(replies) for role, replies in script.items()}
        self.calls, self._lock = [], threading.Lock()

    available = ("gpt-6-luna", "gpt-6-luna-mini")

    def embed(self, texts, model, dimensions=None):
        """Deterministic bag-of-words vectors: shared words mean similar vectors."""
        size = dimensions or 64
        vectors = []
        for text in texts:
            vector = [0.0] * size
            for word in re.findall(r"[a-z0-9_]{2,}", text.lower()):
                vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % size] += 1.0
            vectors.append(vector)
        with self._lock:
            self.embedded = getattr(self, "embedded", 0) + len(texts)
        return vectors, sum(len(t) // 4 for t in texts)

    def models(self):
        return list(self.available)

    def complete(self, *, role, system, prompt, schema, max_output_tokens, model=None):
        with self._lock:
            self.calls.append({"role": role, "system": system, "prompt": prompt, "model": model})
            queue = self.script.get(role) or []
            reply = queue.pop(0) if queue else None
        if reply is None:
            return Reply(None, error="no scripted reply")
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Reply(text, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4)
