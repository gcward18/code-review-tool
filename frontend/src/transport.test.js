import test from "node:test";
import assert from "node:assert/strict";
import { createTransport } from "./transport.js";
import { SignInRequired } from "./auth.js";

test("local API learns the per-start token from the session and sends it on writes", async () => {
  const calls = [];
  const client = createTransport({ config: { mode: "local", apiBaseUrl: "" }, fetcher: async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => url === "/api/session" ? { token: "local-token" } : {} };
  } });
  await client.call("/api/session");
  await client.call("/api/reviews", { method: "POST", body: "{}" });
  assert.equal(calls[0].options.headers.has("Authorization"), false);
  assert.equal(calls[1].options.headers.get("X-AutoSDE-Token"), "local-token");
  assert.equal(calls[1].options.credentials, "same-origin");
});
test("cloud API sends an access token on GETs, writes, and SARIF downloads with the API stage prefix", async () => {
  const calls = [];
  const client = createTransport({ config: { mode: "cloud", apiBaseUrl: "https://api.test/dev" },
    auth: { accessToken: async () => "access" }, fetcher: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({}) };
    } });
  await client.call("/api/session");
  await client.call("/api/reviews", { method: "POST", body: "{}" });
  await client.request("/api/reviews/1.sarif");
  for (const call of calls) {
    assert.ok(call.url.startsWith("https://api.test/dev/api/"));
    assert.equal(call.options.headers.get("Authorization"), "Bearer access");
    assert.equal(call.options.headers.has("X-AutoSDE-Token"), false);
    assert.equal(call.options.credentials, "omit");
    assert.equal(call.options.redirect, "error");
  }
});
test("401 clears auth and requests sign-in without replaying a paid write", async () => {
  let requests = 0, cleared = 0, signedOut = 0;
  const client = createTransport({ config: { mode: "cloud", apiBaseUrl: "" },
    auth: { accessToken: async () => "access", clear: () => cleared++ }, onSignedOut: () => signedOut++,
    fetcher: async () => { requests++; return { ok: false, status: 401 }; } });
  await assert.rejects(client.call("/api/reviews", { method: "POST", body: "{}" }), SignInRequired);
  assert.equal(requests, 1);
  assert.equal(cleared, 1);
  assert.equal(signedOut, 1);
});
test("missing tokens prevent API requests; permission errors do not log the user out", async () => {
  let calls = 0, signedOut = 0;
  const auth = { accessToken: async () => { throw new SignInRequired(); } };
  const client = createTransport({ config: { mode: "cloud", apiBaseUrl: "" }, auth,
    onSignedOut: () => signedOut++, fetcher: async () => { calls++; return { ok: false, status: 403, json: async () => ({ error: "Forbidden" }) }; } });
  await assert.rejects(client.call("/api/session"), SignInRequired);
  assert.equal(calls, 0);
  assert.equal(signedOut, 1);
  auth.accessToken = async () => "access";
  await assert.rejects(client.call("/api/session"), /Forbidden/);
  assert.equal(signedOut, 1);
});


test("direct cloud and inline local uploads share the 20 MB document limit", () => {
  assert.equal(createTransport({ config: { mode: "cloud", apiBaseUrl: "" } }).maxDocumentBytes, 20_000_000);
  assert.equal(createTransport({ config: { mode: "local", apiBaseUrl: "" } }).maxDocumentBytes, 20_000_000);
});
