import test from "node:test";
import assert from "node:assert/strict";
import { loadRuntimeConfig, validateConfig } from "./runtimeConfig.js";
const origin = "https://app.example.test";
const cloud = { mode: "cloud", uploadOrigin: "https://documents.s3.us-east-1.amazonaws.com", apiBaseUrl: "https://api.example.test/stage/", cognito: {
  domain: "https://login.example.test", clientId: "public-client",
} };

test("local config preserves same-origin behavior", () => {
  assert.deepEqual(validateConfig({}, origin), { mode: "local", apiBaseUrl: "" });
});
test("cloud config normalizes stage URLs and same-origin callback defaults", () => {
  const config = validateConfig(cloud, origin);
  assert.equal(config.apiBaseUrl, "https://api.example.test/stage");
  assert.equal(config.uploadOrigin, cloud.uploadOrigin);
  assert.equal(config.cognito.redirectUri, `${origin}/`);
  assert.equal(config.cognito.logoutUri, `${origin}/`);
  assert.deepEqual(config.cognito.scopes, ["openid", "email", "profile"]);
  assert.equal(validateConfig({ ...cloud, apiBaseUrl: "" }, origin).apiBaseUrl, "");
});
test("unsafe or incomplete configurations fail closed", () => {
  for (const input of [null, [], { mode: "typo" }, { mode: "local", apiBaseUrl: "https://other.test" },
    { ...cloud, apiBaseUrl: "http://api.example.test" },
    { ...cloud, apiBaseUrl: "https://user:password@api.example.test" },
    { ...cloud, cognito: {} },
    { ...cloud, uploadOrigin: undefined },
    { ...cloud, uploadOrigin: "https://bucket.example.test/path" },
    { ...cloud, cognito: { ...cloud.cognito, redirectUri: "https://evil.test/" } },
    { ...cloud, cognito: { ...cloud.cognito, redirectUri: `${origin}/#/callback` } },
    { ...cloud, cognito: { ...cloud.cognito, scopes: ["email"] } },
  ]) assert.throws(() => validateConfig(input, origin));
});
test("runtime config bypasses caches and never silently falls back after a failed load", async () => {
  const config = await loadRuntimeConfig(async (path, options) => {
    assert.equal(path, "/config.json");
    assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => ({ mode: "local" }) };
  }, origin);
  assert.equal(config.mode, "local");
  await assert.rejects(loadRuntimeConfig(async () => ({ ok: false }), origin));
});
