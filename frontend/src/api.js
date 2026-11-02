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
  homeChat: () => call("/api/chat"),
  askHome: (message) => call("/api/chat", { method: "POST", body: JSON.stringify({ message }) }),
  clearHomeChat: () => call("/api/chat/clear", { method: "POST", body: "{}" }),
  chat: (id) => call(`/api/projects/${id}/chat`),
  ask: (id, message) =>
    call(`/api/projects/${id}/chat`, { method: "POST", body: JSON.stringify({ message }) }),
  clearChat: (id) => call(`/api/projects/${id}/chat/clear`, { method: "POST", body: "{}" }),
  knowledge: () => call("/api/knowledge"),
  knowledgeOptions: () => call("/api/knowledge/options"),
  previewChunks: (id, body) =>
    call(`/api/knowledge/${id}/preview`, { method: "POST", body: JSON.stringify(body) }),
  rechunkKnowledge: (id) => call(`/api/knowledge/${id}/rechunk`, { method: "POST", body: "{}" }),
  knowledgeBase: (id) => call(`/api/knowledge/${id}`),
  createKnowledge: (name, description) =>
    call("/api/knowledge", { method: "POST", body: JSON.stringify({ name, description }) }),
  updateKnowledge: (id, change) =>
    call(`/api/knowledge/${id}`, { method: "POST", body: JSON.stringify(change) }),
  deleteKnowledge: (id) => call(`/api/knowledge/${id}/delete`, { method: "POST", body: "{}" }),
  uploadDocument: (id, name, data) =>
    call(`/api/knowledge/${id}/documents`, { method: "POST", body: JSON.stringify({ name, data }) }),
  documentChunks: (doc) => call(`/api/knowledge/documents/${doc}/chunks`),
  renameDocument: (doc, name) =>
    call(`/api/knowledge/documents/${doc}/rename`, { method: "POST", body: JSON.stringify({ name }) }),
  summarizeChunks: (doc, positions) =>
    call(`/api/knowledge/documents/${doc}/summarize`, { method: "POST", body: JSON.stringify(positions ? { positions } : {}) }),
  retryDocument: (doc) => call(`/api/knowledge/documents/${doc}/retry`, { method: "POST", body: "{}" }),
  deleteDocument: (doc) => call(`/api/knowledge/documents/${doc}/delete`, { method: "POST", body: "{}" }),
  searchKnowledge: (kb_ids, query) =>
    call("/api/knowledge/search", { method: "POST", body: JSON.stringify({ kb_ids, query }) }),
  reviewFiles: (id) => call(`/api/reviews/${id}/files`),
  reviewFileDiff: (id, path) => call(`/api/reviews/${id}/files?${q({ path })}`),
  remotes: () => call("/api/remotes"),
  addRemote: (repo) => call("/api/remotes", { method: "POST", body: JSON.stringify({ repo }) }),
  fetchRemote: (id) => call(`/api/remotes/${id}/fetch`, { method: "POST", body: "{}" }),
  retryRemote: (id) => call(`/api/remotes/${id}/retry`, { method: "POST", body: "{}" }),
  removeRemote: (id) => call(`/api/remotes/${id}/delete`, { method: "POST", body: "{}" }),
  batchModes: () => call("/api/batches"),
  batches: (repo) => call(`/api/batches?${q({ repo })}`),
  batch: (id) => call(`/api/batches/${id}`),
  planBatch: (body) => call("/api/batches", { method: "POST", body: JSON.stringify(body) }),
  startBatch: (id, max_cost) => call(`/api/batches/${id}/start`, { method: "POST", body: JSON.stringify({ max_cost }) }),
  cancelBatch: (id) => call(`/api/batches/${id}/cancel`, { method: "POST", body: "{}" }),
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
  agentRuns: () => call("/api/agent-runs"),
  saveAgents: (draft) => call("/api/agents", { method: "POST", body: JSON.stringify(draft) }),
  cancel: (id) => call(`/api/reviews/${id}/cancel`, { method: "POST", body: "{}" }),
};
