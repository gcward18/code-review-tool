import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { createAuth, SignInRequired } from "./auth.js";

const config = { mode: "cloud", apiBaseUrl: "https://api.example.test", cognito: {
  domain: "https://login.example.test", clientId: "public-client", redirectUri: "https://app.example.test/",
  logoutUri: "https://app.example.test/", scopes: ["openid", "email"],
} };
function environment() {
  const values = new Map();
  let current = new URL("https://app.example.test/#/review/42");
  const calls = [];
  const env = {
    crypto: webcrypto, calls,
    sessionStorage: { getItem: (k) => values.get(k) || null, setItem: (k, v) => values.set(k, v), removeItem: (k) => values.delete(k) },
    location: { get href() { return current.href; }, get hash() { return current.hash; }, assign: (url) => { env.assigned = url; } },
    history: { replaceState: (_, __, url) => { current = new URL(url, current); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({ access_token: "access", refresh_token: "refresh", expires_in: 3600, token_type: "Bearer" }) };
    },
    navigate: (url) => { current = new URL(url); }, values,
  };
  return env;
}
async function login(env) {
  const auth = createAuth(config, env);
  await auth.signIn();
  const authorize = new URL(env.assigned);
  env.navigate(`https://app.example.test/?code=one-time-code&state=${authorize.searchParams.get("state")}`);
  await auth.completeSignIn();
  return auth;
}

test("PKCE uses S256, random state, a public client, and restores the review bookmark", async () => {
  const env = environment();
  const auth = await login(env);
  const authorize = new URL(env.assigned);
  assert.equal(authorize.pathname, "/oauth2/authorize");
  assert.equal(authorize.searchParams.get("response_type"), "code");
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
  const body = env.calls[0].options.body;
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(body.get("code_verifier")))).toString("base64url");
  assert.equal(authorize.searchParams.get("code_challenge"), digest);
  assert.equal(body.get("code"), "one-time-code");
  assert.equal(body.get("client_secret"), null);
  assert.equal(env.location.href, "https://app.example.test/#/review/42");
  assert.equal(await auth.accessToken(), "access");
  assert.equal(await createAuth(config, env).accessToken(), "access");
  assert.equal(env.values.has("autosde-login"), false);
});

test("invalid callback state is refused before token exchange and removed from the URL", async () => {
  const env = environment();
  const auth = createAuth(config, env);
  await auth.signIn();
  env.navigate("https://app.example.test/?code=secret&state=wrong");
  await assert.rejects(auth.completeSignIn(), /could not be verified/);
  assert.equal(env.calls.length, 0);
  assert.equal(new URL(env.location.href).search, "");
  await assert.rejects(auth.accessToken(), SignInRequired);
});

test("callbacks without a pending login, expired logins and denied logins are recoverable", async () => {
  for (const scenario of ["missing", "expired", "denied"]) {
    const env = environment();
    const auth = createAuth(config, env);
    await auth.signIn();
    const transaction = JSON.parse(env.values.get("autosde-login"));
    if (scenario === "missing") env.values.delete("autosde-login");
    if (scenario === "expired") env.values.set("autosde-login", JSON.stringify({ ...transaction, created: Date.now() - 700_000 }));
    env.navigate(`https://app.example.test/?${scenario === "denied" ? "error=access_denied&error_description=untrusted" : "code=code"}&state=${transaction.state}`);
    await assert.rejects(auth.completeSignIn());
    assert.equal(env.calls.length, 0);
    assert.equal(env.location.href, "https://app.example.test/");
  }
});

test("expired tokens refresh once for concurrent requests, preserving the refresh token", async () => {
  const env = environment();
  await login(env);
  const saved = JSON.parse(env.values.get("autosde-auth"));
  saved.tokens.expiresAt = 0;
  env.values.set("autosde-auth", JSON.stringify(saved));
  env.calls.length = 0;
  env.fetch = async (url, options) => {
    env.calls.push({ url, options });
    return { ok: true, json: async () => ({ access_token: "renewed", expires_in: 3600, token_type: "Bearer" }) };
  };
  const auth = createAuth(config, env);
  assert.deepEqual(await Promise.all([auth.accessToken(), auth.accessToken()]), ["renewed", "renewed"]);
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].options.body.get("grant_type"), "refresh_token");
  assert.equal(JSON.parse(env.values.get("autosde-auth")).tokens.refreshToken, "refresh");
});

test("failed refresh clears credentials; config changes cannot reuse a previous session", async () => {
  const env = environment();
  await login(env);
  const saved = JSON.parse(env.values.get("autosde-auth"));
  saved.tokens.expiresAt = 0;
  env.values.set("autosde-auth", JSON.stringify(saved));
  env.fetch = async () => ({ ok: false });
  await assert.rejects(createAuth(config, env).accessToken(), SignInRequired);
  assert.equal(env.values.has("autosde-auth"), false);
  const otherEnv = environment();
  await login(otherEnv);
  await assert.rejects(createAuth({ ...config, apiBaseUrl: "https://other.test" }, otherEnv).accessToken(), SignInRequired);
});

test("logout clears local credentials and redirects through Cognito", async () => {
  const env = environment();
  const auth = await login(env);
  auth.signOut();
  assert.equal(new URL(env.assigned).pathname, "/logout");
  assert.equal(new URL(env.assigned).searchParams.get("logout_uri"), config.cognito.logoutUri);
  assert.equal(env.values.has("autosde-auth"), false);
  await assert.rejects(auth.accessToken(), SignInRequired);
});


test("token exchange failures do not expose provider responses or save credentials", async () => {
  const env = environment();
  const auth = createAuth(config, env);
  await auth.signIn();
  const transaction = JSON.parse(env.values.get("autosde-login"));
  env.navigate(`https://app.example.test/?code=code&state=${transaction.state}`);
  env.fetch = async () => ({ ok: false, json: async () => ({ error_description: "private detail" }) });
  await assert.rejects(auth.completeSignIn(), /Sign-in could not be completed/);
  assert.equal(env.values.has("autosde-auth"), false);
  assert.equal(env.values.has("autosde-login"), false);
  assert.equal(new URL(env.location.href).search, "");
});
