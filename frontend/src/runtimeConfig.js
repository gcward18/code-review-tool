function secureUrl(value, label, origin) {
  const url = new URL(value, origin);
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && loopback))
      || url.username || url.password || url.search || url.hash) {
    throw new Error(`Invalid ${label} in config.json.`);
  }
  return url;
}

export function validateConfig(input, origin) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Invalid config.json.");
  const mode = input.mode || "local";
  if (!["local", "cloud"].includes(mode)) throw new Error("Unknown mode in config.json.");
  if (mode === "local") {
    if (input.apiBaseUrl || input.cognito) throw new Error("Local mode must use the same-origin API without Cognito.");
    return { mode, apiBaseUrl: "" };
  }
  if (typeof input.apiBaseUrl !== "string") throw new Error("Cloud mode requires apiBaseUrl.");
  const apiBaseUrl = input.apiBaseUrl ? secureUrl(input.apiBaseUrl, "API URL", origin).href.replace(/\/$/, "") : "";
  if (typeof input.uploadOrigin !== "string" || !input.uploadOrigin.startsWith("https://")) {
    throw new Error("Cloud mode requires uploadOrigin.");
  }
  const upload = secureUrl(input.uploadOrigin, "upload origin", origin);
  if (upload.pathname !== "/") throw new Error("Upload origin must not include a path.");
  const cognito = input.cognito;
  if (!cognito || typeof cognito.clientId !== "string" || !cognito.clientId.trim()
      || typeof cognito.domain !== "string" || !cognito.domain.startsWith("https://")) {
    throw new Error("Cloud mode requires a Cognito domain and public client ID.");
  }
  const domain = secureUrl(cognito.domain, "Cognito domain", origin);
  if (domain.pathname !== "/") throw new Error("Cognito domain must not include a path.");
  const redirect = secureUrl(cognito.redirectUri || `${origin}/`, "callback URL", origin);
  const logout = secureUrl(cognito.logoutUri || redirect.href, "logout URL", origin);
  if (redirect.origin !== origin || logout.origin !== origin) throw new Error("Login and logout URLs must use this site's origin.");
  const scopes = cognito.scopes || ["openid", "email", "profile"];
  if (!Array.isArray(scopes) || !scopes.includes("openid")
      || scopes.some((scope) => typeof scope !== "string" || !scope || /\s/.test(scope))) {
    throw new Error("Invalid Cognito scopes.");
  }
  return { mode, apiBaseUrl, uploadOrigin: upload.origin, cognito: {
    domain: domain.origin, clientId: cognito.clientId, redirectUri: redirect.href,
    logoutUri: logout.href, scopes,
  } };
}

export async function loadRuntimeConfig(fetcher = fetch, origin = window.location.origin) {
  const response = await fetcher("/config.json", { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error("Could not load config.json. Check the site's runtime configuration.");
  return validateConfig(await response.json(), origin);
}
