let token = null;

async function call(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json", "X-AutoSDE-Token": token } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

const q = (params) => new URLSearchParams(params).toString();

export const api = {
  async session() {
    const data = await call("/api/session");
    token = data.token;
    return data;
  },
  workspaces: () => call("/api/workspaces"),
  tree: (path) => call(`/api/tree?${q(path ? { path } : {})}`),
  repo: (path) => call(`/api/repo?${q({ path })}`),
  older: (path, from) => call(`/api/older?${q({ path, from })}`),
  projects: () => call("/api/projects"),
  project: (id) => call(`/api/projects/${id}`),
  reviews: () => call("/api/reviews"),
  review: (id) => call(`/api/reviews/${id}`),
  submit: (repo, from, to) =>
    call("/api/reviews", { method: "POST", body: JSON.stringify({ repo, from, to }) }),
  estimate: (id, files) =>
    call(`/api/reviews/${id}/estimate`, { method: "POST", body: JSON.stringify({ files }) }),
  approve: (id, files) =>
    call(`/api/reviews/${id}/approve`, { method: "POST", body: JSON.stringify({ files }) }),
  agents: () => call("/api/agents"),
  models: () => call("/api/models"),
  saveAgents: (draft) => call("/api/agents", { method: "POST", body: JSON.stringify(draft) }),
  cancel: (id) => call(`/api/reviews/${id}/cancel`, { method: "POST", body: "{}" }),
};
