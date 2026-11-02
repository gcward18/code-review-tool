import { useEffect, useState } from "react";
import App from "./App.jsx";
import { loadRuntimeConfig } from "./runtimeConfig.js";
import { createAuth, SignInRequired } from "./auth.js";
import { configureApi } from "./api.js";

export default function Bootstrap() {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    let active = true;
    const update = (next) => { if (active) setState(next); };
    (async () => {
      const config = await loadRuntimeConfig();
      if (config.mode === "local") {
        configureApi({ config });
        update({ status: "ready" });
        return;
      }
      const auth = createAuth(config);
      configureApi({ config, auth, onSignedOut: () => update({ status: "signed-out", auth }) });
      try {
        await auth.completeSignIn();
        await auth.accessToken();
        update({ status: "ready", auth });
      } catch (error) {
        update({ status: "signed-out", auth, error: error instanceof SignInRequired ? "" : error.message });
      }
    })().catch(() => update({ status: "error", error: "Could not start AutoSDE. Check config.json and reload." }));
    return () => { active = false; };
  }, []);

  const signIn = () => {
    setState((previous) => ({ ...previous, error: "", busy: true }));
    state.auth.signIn().catch(() => setState((previous) => ({ ...previous, busy: false,
      error: "Could not start sign-in. Allow session storage and try again." })));
  };
  if (state.status === "ready") return <App onSignOut={state.auth ? () => state.auth.signOut() : undefined} />;
  return (
    <main className="card" style={{ maxWidth: 480, margin: "12vh auto" }}>
      <h1>AutoSDE</h1>
      {state.status === "loading" ? <p role="status">Loading…</p> : <>
        {state.error && <p className="error" role="alert">{state.error}</p>}
        {state.status === "signed-out" ? <>
          <p>Sign in to open your projects and reviews.</p>
          <button onClick={signIn} disabled={state.busy}>{state.busy ? "Opening sign-in…" : "Sign in"}</button>
        </> : <button onClick={() => window.location.reload()}>Reload</button>}
      </>}
    </main>
  );
}
