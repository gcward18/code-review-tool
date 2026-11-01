// Keep URL serialization independent of React so every navigation uses one contract.
export function parseRoute(hash = "") {
  try {
    const [path, query = ""] = hash.replace(/^#/, "").split("?");
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const params = new URLSearchParams(query);
    const [kind, value] = parts;
    if (parts.length > 2) return { kind: "not-found" };
    if (!kind) return { kind: "home" };
    if (kind === "agents") return { kind, agent: value || "python" };
    if (kind === "knowledge" && (!value || /^[1-9]\d*$/.test(value))) {
      return { kind, base: value ? Number(value) : null };
    }
    if (["review", "project"].includes(kind) && /^[1-9]\d*$/.test(value)) {
      return { kind, id: Number(value) };
    }
    if (kind === "repo" && !value && params.get("path")) return { kind, repo: params.get("path") };
    if (["home", "settings"].includes(kind) && !value) return { kind };
  } catch { /* A malformed link should not crash the application. */ }
  return { kind: "not-found" };
}

export function routeHref(view) {
  switch (view.kind) {
    case "home": return "#/";
    case "agents": return `#/agents${view.agent ? `/${encodeURIComponent(view.agent)}` : ""}`;
    case "knowledge": return `#/knowledge${view.base ? `/${view.base}` : ""}`;
    case "settings": return "#/settings";
    case "review": case "project": return `#/${view.kind}/${view.id}`;
    case "repo": return `#/repo?${new URLSearchParams({ path: view.repo })}`;
    default: return "#/not-found";
  }
}
