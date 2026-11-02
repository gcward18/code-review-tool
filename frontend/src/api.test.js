import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { api, configureApi } from "./api.js";

const origin = "https://documents.s3.us-east-1.amazonaws.com";
const ticket = { upload_id: "abc123", url: `${origin}/`, fields: { key: "uploads/direct/abc123", policy: "signed-policy" } };
const ok = (data) => ({ ok: true, json: async () => data });

function cloud(fetcher) {
  configureApi({ config: { mode: "cloud", apiBaseUrl: "", uploadOrigin: origin },
    auth: { accessToken: async () => "test-access" }, fetcher });
}

test("cloud files larger than the old request cap go directly to S3 without bearer credentials or base64 JSON", async () => {
  const calls = [];
  cloud(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/complete")) return ok({ id: 9, status: "processing" });
    return ok(url === origin + "/" ? {} : ticket);
  });
  const bytes = Buffer.alloc(4_000_000, 97);
  const result = await api.uploadDocument(1, "notes.txt", new Blob([bytes]));
  assert.equal(result.id, 9);
  assert.equal(calls.length, 3);
  const metadata = JSON.parse(calls[0].options.body);
  assert.deepEqual(metadata, { name: "notes.txt", size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
  assert.equal(calls[0].options.headers.get("Authorization"), "Bearer test-access");
  const upload = calls[1].options;
  assert.ok(upload.body instanceof FormData);
  assert.equal(upload.body.get("file").size, bytes.length);
  assert.equal(upload.body.get("key"), ticket.fields.key);
  assert.equal(upload.headers, undefined);
  assert.equal(upload.credentials, "omit");
  assert.equal(upload.redirect, "error");
  assert.equal(calls[2].options.body, "{}");
});

test("both modes reject files over 20 MB before making a request", async () => {
  for (const mode of ["cloud", "local"]) {
    let calls = 0;
    configureApi({ config: { mode, apiBaseUrl: "" }, fetcher: async () => { calls++; return ok({}); } });
    await assert.rejects(api.uploadDocument(1, "notes.txt", new Blob([Buffer.alloc(20_000_001)])), /under 20 MB/);
    assert.equal(calls, 0);
  }
});

test("local files and pasted Unicode text keep the existing inline API and session token", async () => {
  const calls = [];
  configureApi({ config: { mode: "local", apiBaseUrl: "" }, fetcher: async (url, options) => {
    calls.push({ url, options });
    return ok(url === "/api/session" ? { token: "local-token" } : { id: 2 });
  } });
  await api.session();
  const text = "Notes 日本語 📝";
  await api.uploadDocument(1, "notes.txt", new Blob([text], { type: "text/plain" }));
  assert.equal(calls[1].url, "/api/knowledge/1/documents");
  assert.equal(Buffer.from(JSON.parse(calls[1].options.body).data, "base64").toString("utf8"), text);
  assert.equal(calls[1].options.headers.get("X-AutoSDE-Token"), "local-token");
});

test("failed S3 uploads do not call the completion API", async () => {
  const calls = [];
  cloud(async (url) => { calls.push(url); return url === origin + "/" ? { ok: false } : ok(ticket); });
  await assert.rejects(api.uploadDocument(1, "notes.txt", new Blob(["notes"])), /upload failed/);
  assert.equal(calls.length, 2);
});

test("a signed form cannot redirect document content to an unexpected origin", async () => {
  const calls = [];
  cloud(async (url) => { calls.push(url); return ok({ ...ticket, url: "https://unexpected.example.test/" }); });
  await assert.rejects(api.uploadDocument(1, "notes.txt", new Blob(["notes"])), /Unexpected upload destination/);
  assert.equal(calls.length, 1);
});

test("a transient completion failure retries only the idempotent completion step", async () => {
  const calls = [];
  let completion = 0;
  cloud(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/complete")) {
      completion++;
      return completion === 1 ? { ok: false, status: 500, json: async () => ({ error: "Try again" }) } : ok({ id: 8 });
    }
    return ok(url === origin + "/" ? {} : ticket);
  });
  assert.equal((await api.uploadDocument(1, "notes.txt", new Blob(["notes"]))).id, 8);
  assert.equal(calls.filter((call) => call.url === origin + "/").length, 1);
  assert.equal(calls.filter((call) => call.url.endsWith("/complete")).length, 2);
});
