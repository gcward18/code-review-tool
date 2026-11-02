import { MAX_DOCUMENT_BYTES } from "./uploads.js";
import { SignInRequired } from "./auth.js";

export function createTransport({ config, auth, onSignedOut = () => {}, fetcher = fetch }) {
  let localToken = null;
  const request = async (path, options = {}) => {
    if (!path.startsWith("/api/")) throw new Error("Invalid API path.");
    const headers = new Headers(options.headers);
    if (options.body) headers.set("Content-Type", "application/json");
    if (config.mode === "cloud") {
      try { headers.set("Authorization", `Bearer ${await auth.accessToken()}`); }
      catch (error) { if (error instanceof SignInRequired) onSignedOut(); throw error; }
    } else if (options.body) headers.set("X-AutoSDE-Token", localToken || "");
    const response = await fetcher(`${config.apiBaseUrl}${path}`, {
      ...options, headers, credentials: config.mode === "cloud" ? "omit" : "same-origin",
      cache: "no-store", redirect: "error",
    });
    if (response.status === 401 && config.mode === "cloud") {
      auth.clear();
      onSignedOut();
      throw new SignInRequired();
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(data.error || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response;
  };
  return {
    request,
    maxDocumentBytes: MAX_DOCUMENT_BYTES,
    directUploads: config.mode === "cloud",
    async uploadForm(url, fields, blob, name) {
      const target = new URL(url);
      if (!config.uploadOrigin || target.origin !== config.uploadOrigin || target.protocol !== "https:"
          || target.username || target.password) throw new Error("Unexpected upload destination.");
      const form = new FormData();
      for (const [key, value] of Object.entries(fields)) form.append(key, value);
      form.append("file", blob, name);
      const response = await fetcher(url, { method: "POST", body: form, credentials: "omit", redirect: "error" });
      if (!response.ok) throw new Error("The file upload failed. Choose the file again to retry.");
    },
    async call(path, options) {
      const data = await (await request(path, options)).json();
      if (path === "/api/session" && config.mode === "local") localToken = data.token;
      return data;
    },
  };
}
