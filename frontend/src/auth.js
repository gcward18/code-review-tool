const SESSION_KEY = "autosde-auth";
const TRANSACTION_KEY = "autosde-login";

export class SignInRequired extends Error {
  constructor() { super("Your session has ended. Sign in to continue."); }
}

const base64url = (bytes) => btoa(String.fromCharCode(...bytes))
  .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");

// Cognito validates tokens at API Gateway. The browser never uses decoded claims as authority.
export function createAuth(config, env = window) {
  const { cognito } = config;
  const storage = env.sessionStorage;
  const identity = JSON.stringify([config.apiBaseUrl, cognito.domain, cognito.clientId, cognito.redirectUri]);
  let tokens = null;
  let refreshing = null;
  const clear = () => { tokens = null; storage.removeItem(SESSION_KEY); };
  try {
    const saved = JSON.parse(storage.getItem(SESSION_KEY) || "null");
    if (saved?.identity === identity) tokens = saved.tokens;
    else storage.removeItem(SESSION_KEY);
  } catch { storage.removeItem(SESSION_KEY); }

  const random = () => base64url(env.crypto.getRandomValues(new Uint8Array(32)));
  const save = (data, previous = null) => {
    if (typeof data.access_token !== "string" || !data.access_token
        || !Number.isFinite(data.expires_in) || data.expires_in <= 0
        || data.token_type?.toLowerCase() !== "bearer") throw new Error("Invalid sign-in response.");
    tokens = { accessToken: data.access_token, refreshToken: data.refresh_token || previous?.refreshToken,
      expiresAt: Date.now() + data.expires_in * 1000 };
    storage.setItem(SESSION_KEY, JSON.stringify({ identity, tokens }));
  };
  const exchange = async (fields) => {
    const response = await env.fetch(`${cognito.domain}/oauth2/token`, {
      method: "POST", credentials: "omit", redirect: "error",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: cognito.clientId, ...fields }),
    });
    if (!response.ok) throw new Error("Sign-in could not be completed. Please sign in again.");
    return response.json();
  };
  return {
    clear,
    async signIn() {
      clear();
      const state = random();
      const verifier = random();
      const digest = await env.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
      storage.setItem(TRANSACTION_KEY, JSON.stringify({ identity, state, verifier,
        hash: env.location.hash, created: Date.now() }));
      const query = new URLSearchParams({ client_id: cognito.clientId, response_type: "code",
        redirect_uri: cognito.redirectUri, scope: cognito.scopes.join(" "), state,
        code_challenge_method: "S256", code_challenge: base64url(new Uint8Array(digest)) });
      env.location.assign(`${cognito.domain}/oauth2/authorize?${query}`);
    },
    async completeSignIn() {
      const url = new URL(env.location.href);
      if (!url.searchParams.has("code") && !url.searchParams.has("error")) return;
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state");
      const denied = url.searchParams.has("error");
      // Consume the transaction and remove credentials from the address bar even on failure.
      const raw = storage.getItem(TRANSACTION_KEY);
      storage.removeItem(TRANSACTION_KEY);
      for (const key of ["code", "state", "error", "error_description", "error_uri"]) url.searchParams.delete(key);
      env.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
      let transaction;
      try { transaction = JSON.parse(raw); } catch { /* handled below */ }
      if (!transaction || transaction.identity !== identity || !state || transaction.state !== state
          || typeof transaction.verifier !== "string" || Date.now() - transaction.created > 600_000
          || !Number.isFinite(transaction.created) || transaction.created > Date.now()) {
        clear();
        throw new Error("Sign-in expired or could not be verified. Please sign in again.");
      }
      if (denied || !code) { clear(); throw new Error("Sign-in was cancelled or refused. Please try again."); }
      clear();
      save(await exchange({ grant_type: "authorization_code", code, redirect_uri: cognito.redirectUri,
        code_verifier: transaction.verifier }));
      const hash = typeof transaction.hash === "string" && transaction.hash.startsWith("#/") ? transaction.hash : "#/";
      env.history.replaceState(null, "", `${url.pathname}${url.search}${hash}`);
    },
    async accessToken() {
      if (tokens?.accessToken && tokens.expiresAt > Date.now() + 30_000) return tokens.accessToken;
      if (!tokens?.refreshToken) { clear(); throw new SignInRequired(); }
      if (!refreshing) {
        const previous = tokens;
        refreshing = exchange({ grant_type: "refresh_token", refresh_token: previous.refreshToken })
          .then((data) => { if (tokens === previous) save(data, previous); })
          .catch(() => { clear(); throw new SignInRequired(); })
          .finally(() => { refreshing = null; });
      }
      await refreshing;
      if (!tokens) throw new SignInRequired();
      return tokens.accessToken;
    },
    signOut() {
      clear();
      storage.removeItem(TRANSACTION_KEY);
      env.location.assign(`${cognito.domain}/logout?${new URLSearchParams({
        client_id: cognito.clientId, logout_uri: cognito.logoutUri,
      })}`);
    },
  };
}
