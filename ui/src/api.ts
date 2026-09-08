import type {
  AgentInfo,
  ApiKey,
  CatalogDescribe, CatalogList, ConnectorSpec, DiscoverProposal, DispatchDetail, DispatchLogEntry, Entity, EnvScan,
  AgentPreset, AgentRun, BuiltinAgent,
  GithubCredential,
  LabelFacet, ModelUsage, QueryLogEntry,
  McpServer, Template, Project, ProjectObjectKind, ProjectSummary, ProjectUpdateReport,
  Source, SourceEvent, SourceFieldsProfile, Subscription, TestResult, Usage,
  TimelineEventRow, Trigger, View,
} from "./types";

const TOKEN_KEY = "tares_token";
export const auth = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? "",
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export function authHeader(): Record<string, string> {
  const t = auth.get();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

// The Anthropic key lives on the SERVER (Security → /api/settings/anthropic-key), never here. It
// used to sit in localStorage and ride along as a per-request header, which meant a key added on
// the Ask page made Ask work while Slack and trigger-woken agents still reported none configured —
// they have no browser to read it from. Credentials do not belong in localStorage: any script on
// the origin can read them and they persist silently.
//
// This clears anything left by that older build. Safe to delete once no browser can still be
// carrying one (say, a release or two after 1.0.2).
try { localStorage.removeItem("tares_anthropic_key"); } catch { /* private mode */ }

// Only channels the bot is a member of are listed, so a private one appearing here is expected.
// `is_private` exists because Slack never shows a private channel as "#name" — labelling it that
// way would name something the user can't find by that name.
export type SlackChannel = { id: string; name: string; is_private: boolean };
export type SlackChannels = {
  channels: SlackChannel[];
  reason: null | "no_token" | "missing_scope" | "error";
  detail?: string;
};

function unauthorized() {
  auth.clear();
  window.dispatchEvent(new Event("tares-auth-required"));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...authHeader(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (res.status === 401) {
    unauthorized();
    throw new Error("authentication required");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // login_url is present only on a cloud-managed cell (daemon TARES_LOGIN_URL) — it tells the
  // logged-out console where to send the browser to authenticate.
  // status: "ok" | "degraded" (running, storage nearly full) | "down" (no usable database — the
  // daemon is serving the console and 503s so the failure is visible). `detail` says why when it
  // isn't ok; `pct_used` is on /api/usage's 0-100 scale and null when no limit is configured.
  health: () => request<{
    status: string; auth_required: boolean; login_url?: string;
    workspace_url?: string;   // cloud only: the control-plane workspace this cell belongs to
    detail?: string; pct_used?: number | null;
  }>("/health"),
  // Swap a one-time ?code= (handed to us in the redirect back from the control plane) for the real
  // cell key. Raw cross-origin fetch: no auth header yet, and the control plane's CORS allows POST
  // from *.<cell domain>. Deliberately NOT the `request` helper, which would attach the (absent)
  // token and treat a 401 as a session expiry.
  exchange: async (loginUrl: string, code: string): Promise<string> => {
    const res = await fetch(new URL("/exchange", loginUrl).toString(), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (!res.ok) throw new Error(`exchange failed: ${res.status}`);
    return (await res.json()).token as string;
  },
  connectors: () => request<Record<string, ConnectorSpec>>("/api/connectors"),
  capabilities: () =>
    request<{ version?: string | null; discover_docker: boolean; agent_key_configured?: boolean;
              url_configured: boolean; slack_configured?: boolean }>("/api/capabilities"),
  keys: () => request<{ keys: ApiKey[]; enforced: boolean; scopes: string[] }>("/api/keys"),
  createKey: (name: string, scopes: string[]) =>
    request<{ id: string; name: string; scopes: string[]; secret: string }>(
      "/api/keys", { method: "POST", body: JSON.stringify({ name, scopes }) }),
  revokeKey: (id: string) => request<{ ok: boolean }>(`/api/keys/${id}`, { method: "DELETE" }),
  whoami: () => request<{ id: string; name: string; scopes: string[] }>("/api/whoami"),

  sources: () => request<Source[]>("/api/sources"),
  source: (name: string) => request<Source>(`/api/sources/${name}`),
  createSource: (body: object) =>
    request<{ ok: boolean; name: string; ingest_key: string | null }>(
      "/api/sources", { method: "POST", body: JSON.stringify(body) }),
  updateSource: (name: string, body: object) =>
    request(`/api/sources/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteSource: (name: string, purge: boolean, cascade = false) =>
    request<{ ok: boolean; purged_events: number; deleted: string[] }>(
      `/api/sources/${encodeURIComponent(name)}?purge_events=${purge}&cascade=${cascade}`, { method: "DELETE" }),
  // what else goes if an object is deleted, in delete order
  dependents: (kind: "source" | "view" | "trigger", name: string) =>
    request<{ dependents: { kind: ProjectObjectKind; name: string }[] }>(
      `/api/catalog/dependents?kind=${kind}&name=${encodeURIComponent(name)}`),
  pauseSource: (name: string) => request(`/api/sources/${name}/pause`, { method: "POST" }),
  resumeSource: (name: string) => request(`/api/sources/${name}/resume`, { method: "POST" }),
  testSource: (body: object) =>
    request<TestResult>("/api/sources/test", { method: "POST", body: JSON.stringify(body) }),
  discoverSource: (connector: string, config: Record<string, unknown>) =>
    request<DiscoverProposal>("/api/sources/discover",
      { method: "POST", body: JSON.stringify({ connector, config }) }),
  discoverEnvironment: (provider = "docker") =>
    request<EnvScan>(`/api/discover/environment?provider=${provider}`),
  sourceEvents: (name: string, limit = 50) =>
    request<SourceEvent[]>(`/api/sources/${name}/events?limit=${limit}`),
  sourceFields: (name: string) =>
    request<SourceFieldsProfile>(`/api/sources/${name}/fields`),
  labelPreview: (source: string, label: Record<string, unknown>) =>
    request<{ sampled: number; distinct_before: number; distinct_after: number;
              results: { from: string; to: string; events: number }[] }>(
      "/api/labels/preview", { method: "POST", body: JSON.stringify({ source, label }) }),

  views: () => request<View[]>("/api/views"),
  createView: (body: View) =>
    request("/api/views", { method: "POST", body: JSON.stringify(body) }),
  updateView: (name: string, body: View) =>
    request(`/api/views/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteView: (name: string, cascade = false) =>
    request<{ ok: boolean; deleted: string[] }>(`/api/views/${encodeURIComponent(name)}?cascade=${cascade}`, { method: "DELETE" }),

  triggers: () => request<Trigger[]>("/api/triggers"),
  createTrigger: (body: Trigger) =>
    request("/api/triggers", { method: "POST", body: JSON.stringify(body) }),
  updateTrigger: (name: string, body: Trigger) =>
    request(`/api/triggers/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTrigger: (name: string) => request(`/api/triggers/${name}`, { method: "DELETE" }),
  pauseTrigger: (name: string) => request(`/api/triggers/${name}/pause`, { method: "POST" }),
  resumeTrigger: (name: string) => request(`/api/triggers/${name}/resume`, { method: "POST" }),
  // ── Tares agents: a first look when a trigger fires (managed under /builtin) ──
  builtinAgents: () =>
    request<{ agents: BuiltinAgent[]; key_configured: boolean; key_source: string;
              models: string[]; default_model: string; slack_workspace: boolean;
              default_max_rounds: number; default_max_rounds_with_mcp: number;
              max_rounds_limit: number;
              presets: AgentPreset[] }>("/api/agents/builtin"),
  createBuiltinAgent: (body: { name: string; trigger: string; prompt: string; slack_webhook?: string; slack_webhook_clear?: boolean; model?: string; slack_channel?: string; webhook_url?: string; webhook_token?: string; mcp_servers?: string[]; max_rounds?: number | null; budget_usd?: number | null }) =>
    request<{ ok: boolean; enabled: boolean }>("/api/agents/builtin",
      { method: "POST", body: JSON.stringify(body) }),
  updateBuiltinAgent: (name: string, body: { name: string; trigger: string; prompt: string; slack_webhook?: string; slack_webhook_clear?: boolean; model?: string; slack_channel?: string; webhook_url?: string; webhook_token?: string; mcp_servers?: string[]; max_rounds?: number | null; budget_usd?: number | null }) =>
    request(`/api/agents/builtin/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteBuiltinAgent: (name: string) => request(`/api/agents/builtin/${name}`, { method: "DELETE" }),
  enableBuiltinAgent: (name: string) => request(`/api/agents/builtin/${name}/enable`, { method: "POST" }),
  disableBuiltinAgent: (name: string) => request(`/api/agents/builtin/${name}/disable`, { method: "POST" }),
  builtinAgentRuns: (name: string, limit = 20, offset = 0, status = "") =>
    request<AgentRun[]>(`/api/agents/builtin/${encodeURIComponent(name)}/runs?limit=${limit}&offset=${offset}`
      + (status ? `&status=${encodeURIComponent(status)}` : "")),
  rerunAgentRun: (name: string, runId: string) =>
    request<{ ok: boolean; run_id: string }>(
      `/api/agents/builtin/${encodeURIComponent(name)}/runs/${encodeURIComponent(runId)}/rerun`,
      { method: "POST" }),
  // The cell's Anthropic spend meter: totals + per-day, split agent runs vs Ask.
  modelUsage: (days = 30) => request<ModelUsage>(`/api/usage/model?days=${days}`),

  // The Anthropic key Tares agents run on. Never returned — only whether one resolves and where.
  anthropicKeyStatus: () =>
    request<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>(
      "/api/settings/anthropic-key"),
  setAnthropicKey: (key: string) =>
    request<{ ok: boolean; source: string; note?: string }>("/api/settings/anthropic-key",
      { method: "PUT", body: JSON.stringify({ key }) }),
  clearAnthropicKey: () =>
    request<{ ok: boolean; configured: boolean }>("/api/settings/anthropic-key",
      { method: "DELETE" }),

  // Agent tracing: where runs are exported and whether. Secrets are write-only; the status says
  // what resolves and where from (console vs environment).
  tracingStatus: () => request<TracingStatus>("/api/settings/tracing"),
  setTracing: (body: { enabled?: boolean; provider?: string; endpoint?: string; api_key?: string; headers?: string }) =>
    request<TracingStatus & { ok: boolean; note?: string }>("/api/settings/tracing",
      { method: "PUT", body: JSON.stringify(body) }),

  // The Slack bot token behind slack:// subscriptions. Same contract as the Anthropic key: the
  // value never leaves the server, only whether one resolves and where from.
  slackTokenStatus: () =>
    request<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>(
      "/api/settings/slack-bot-token"),
  setSlackToken: (token: string) =>
    request<{ ok: boolean; source: string; note?: string }>("/api/settings/slack-bot-token",
      { method: "PUT", body: JSON.stringify({ token }) }),
  clearSlackToken: () =>
    request<{ ok: boolean; configured: boolean }>("/api/settings/slack-bot-token",
      { method: "DELETE" }),

  // The signing secret that authenticates inbound Slack (`/tares ask …`). Write-only like the
  // others; without it POST /api/slack/events answers 503 rather than trusting the request.
  slackSigningSecretStatus: () =>
    request<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>(
      "/api/settings/slack-signing-secret"),
  setSlackSigningSecret: (secret: string) =>
    request<{ ok: boolean; source: string; note?: string }>("/api/settings/slack-signing-secret",
      { method: "PUT", body: JSON.stringify({ secret }) }),
  clearSlackSigningSecret: () =>
    request<{ ok: boolean; configured: boolean }>("/api/settings/slack-signing-secret",
      { method: "DELETE" }),

  // The channels the bot can post to, for picking one instead of pasting an ID. Answers 200 even
  // when it can't list them — `reason` says why the list is empty — so a workspace we can't read
  // stays a fallback to typing the channel in, not an error the console has to render.
  slackChannels: () => request<SlackChannels>("/api/slack/channels"),

  agents: () => request<{ agents: AgentInfo[] }>("/api/agents"),
  unsubscribe: (subscription_id: string) =>
    request<{ ok: boolean }>("/unsubscribe",
      { method: "POST", body: JSON.stringify({ subscription_id }) }),
  subscribe: (trigger: string, url: string) =>
    request<{ subscription_id: string }>("/subscribe",
      { method: "POST", body: JSON.stringify({ trigger, url }) }),

  // ── MCP connections: external tool servers agents can opt into ──
  mcpServers: () => request<{ servers: McpServer[] }>("/api/mcp-servers"),
  createMcpServer: (body: { name: string; url: string; auth_header?: string; auth_value?: string;
                            headers?: Record<string, string> }) =>
    request<{ ok: boolean }>("/api/mcp-servers", { method: "POST", body: JSON.stringify(body) }),
  updateMcpServer: (name: string, body: { name: string; url: string; auth_header?: string; auth_value?: string;
                                          headers?: Record<string, string> }) =>
    request<{ ok: boolean }>(`/api/mcp-servers/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  deleteMcpServer: (name: string) =>
    request<{ ok: boolean }>(`/api/mcp-servers/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testMcpServer: (name: string) =>
    request<{ ok: boolean; error?: string; tools: { name: string; description: string }[] }>(
      `/api/mcp-servers/${encodeURIComponent(name)}/test`, { method: "POST" }),

  // ── GitHub credentials: a token stored once, referenced by sources and MCP servers ──
  githubCredentials: () =>
    request<{ credentials: GithubCredential[] }>("/api/integrations/github"),
  createGithubCredential: (body: { name: string; token: string; api_url?: string }) =>
    request<{ ok: boolean; account: string }>("/api/integrations/github",
      { method: "POST", body: JSON.stringify(body) }),
  updateGithubCredential: (name: string, body: { name: string; token?: string; api_url?: string }) =>
    request<{ ok: boolean; account: string }>(`/api/integrations/github/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  deleteGithubCredential: (name: string) =>
    request<{ ok: boolean }>(`/api/integrations/github/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testGithubCredential: (name: string) =>
    request<{ ok: boolean; error?: string; login?: string; name?: string; scopes?: string[] }>(
      `/api/integrations/github/${encodeURIComponent(name)}/test`, { method: "POST" }),
  githubCredentialTree: (name: string, repo: string, ref = "", path = "") =>
    request<{ ref: string; path: string; dirs: string[]; files: string[]; markdown: string[]; exists: boolean }>(
      `/api/integrations/github/${encodeURIComponent(name)}/tree?repo=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}&path=${encodeURIComponent(path)}`),
  githubCredentialRepos: (name: string, query = "") =>
    request<{ repos: { full_name: string; default_branch: string; private: boolean; pushed_at: string | null }[] }>(
      `/api/integrations/github/${encodeURIComponent(name)}/repos?query=${encodeURIComponent(query)}`),

  // ── Projects: templates instantiated with params; each instance owns ordinary objects ──
  templates: () => request<{ templates: Template[] }>("/api/projects/templates"),
  // by key, hidden templates included (the gallery list leaves those out)
  template: (key: string) => request<Template>(`/api/projects/templates/${encodeURIComponent(key)}`),
  projects: () => request<{ projects: Project[] }>("/api/projects"),
  project: (id: string) => request<Project>(`/api/projects/${encodeURIComponent(id)}`),
  // Accepted memory: only what a user accepted is ever written (a proposal is just text until then).
  remember: (body: { key: string; content: string; memory_type?: string }) =>
    request<{ ok: boolean; source: string }>("/remember", { method: "POST", body: JSON.stringify(body) }),
  projectSummary: (id: string) =>
    request<ProjectSummary>(`/api/projects/${encodeURIComponent(id)}/summary`),
  // template "custom" takes `objects` ({kind, name} each) instead of params
  createProject: (body: { template: string; name?: string; params?: Record<string, unknown>;
                          objects?: { kind: ProjectObjectKind; name: string }[] }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  updateProject: (id: string, body: { params?: Record<string, unknown>; name?: string;
                                      objects?: { kind: ProjectObjectKind; name: string }[] }) =>
    request<Project & { report?: ProjectUpdateReport }>(`/api/projects/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  // `deleteObjects`: the project's objects to delete along with it; the rest are released and
  // stay. Omit for the default (a template project takes everything, a custom one keeps everything).
  deleteProject: (id: string, purgeEvents = false, deleteObjects?: { kind: ProjectObjectKind; name: string }[]) =>
    request<{ ok: boolean; deleted?: string[]; released?: string[]; purged_events?: number }>(
      `/api/projects/${encodeURIComponent(id)}?purge_events=${purgeEvents}`
      + (deleteObjects === undefined ? "" : `&delete=${encodeURIComponent(deleteObjects.length ? deleteObjects.map((o) => `${o.kind}:${o.name}`).join(",") : "none")}`),
      { method: "DELETE" }),
  pauseProject: (id: string, sources = false) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/pause`, { method: "POST", body: JSON.stringify({ sources }) }),
  resumeProject: (id: string) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/resume`, { method: "POST" }),
  detectRecipe: (key: string) =>
    request<{ params: Record<string, unknown>; found: Record<string, string>; missing: Record<string, string>; notes: string[] }>(
      `/api/projects/templates/${encodeURIComponent(key)}/detect`, { method: "POST" }),
  projectAction: (id: string, name: string, args: Record<string, unknown> = {}) =>
    request<{ ok: boolean; action: string; message?: string }>(
      `/api/projects/${encodeURIComponent(id)}/actions/${encodeURIComponent(name)}`,
      { method: "POST", body: JSON.stringify(args) }),
  repairProject: (id: string, key: string) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/repair`,
      { method: "POST", body: JSON.stringify({ key }) }),

  // ── Ask sessions: server-side chat history (state is the console's own JSON blob) ──
  askSessions: () =>
    request<{ sessions: { id: string; title: string; created_at: string; updated_at: string }[] }>(
      "/api/ask/sessions"),
  askSession: (id: string) =>
    request<{ id: string; title: string; state: string }>(`/api/ask/sessions/${id}`),
  saveAskSession: (id: string, title: string, state: string) =>
    request<{ ok: boolean }>(`/api/ask/sessions/${id}`,
      { method: "PUT", body: JSON.stringify({ title, state }) }),
  deleteAskSession: (id: string) =>
    request<{ ok: boolean }>(`/api/ask/sessions/${id}`, { method: "DELETE" }),

  queries: (limit = 100) => request<QueryLogEntry[]>(`/api/activity/queries?limit=${limit}`),
  dispatches: (limit = 100) =>
    request<DispatchLogEntry[]>(`/api/activity/dispatches?limit=${limit}`),
  dispatch: (id: string) => request<DispatchDetail>(`/api/activity/dispatches/${id}`),
  subscriptions: () => request<Subscription[]>("/api/subscriptions"),
  mcpTools: () => request<{ name: string; description: string }[]>("/api/mcp/tools"),

  // What this instance is using on disk. `max_bytes`/`pct_used` are null unless the operator set
  // TARES_MAX_DB_SIZE — see the Usage type before rendering any of it.
  usage: () => request<Usage>("/api/usage"),

  catalog: () => request<CatalogList>("/catalog"),
  describe: (handle: string) => request<CatalogDescribe>(`/catalog/${handle}`),

  entities: (label?: string) =>
    request<{ labels?: LabelFacet[]; label?: string; sources?: string[]; values?: Entity[] }>(
      label ? `/api/entities?label=${encodeURIComponent(label)}` : "/api/entities"),

  // Raw label-native read across ALL sources — no view. `selector` is a {label: value}
  // conjunction (strict AND). Returns the rendered payload, contributing sources, and structured
  // rows (each with its per-event labels, for the console timeline).
  read: (selector: Record<string, string>, window: string) =>
    request<{ payload: string; count: number; sources: string[]; rows: TimelineEventRow[] }>("/read", {
      method: "POST",
      body: JSON.stringify({ selector, window, client: "ui" }),
    }),

  runQuery: (view: string, key: string, window: string) =>
    request<{ payload: string; rows: TimelineEventRow[] }>("/query", {
      method: "POST",
      body: JSON.stringify({ view, key, window, client: "ui" }),
    }),
  runQueryWhere: (view: string, where: Record<string, string>, window: string) =>
    request<{ payload: string; rows: TimelineEventRow[] }>("/query", {
      method: "POST",
      body: JSON.stringify({ view, where, window, client: "ui" }),
    }),

  // Defaults match the agent/MCP call: all sources, secrets omitted. The UI passes options.
  exportYaml: async (opts?: { sources?: string[]; includeSecrets?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.sources?.length) q.set("sources", opts.sources.join(","));
    if (opts?.includeSecrets) q.set("include_secrets", "true");
    const qs = q.toString();
    const res = await fetch("/api/catalog/export" + (qs ? "?" + qs : ""), { headers: authHeader() });
    if (res.status === 401) {
      unauthorized();
      throw new Error("authentication required");
    }
    return res.text();
  },
  importYaml: (yaml: string, mode: "merge" | "replace") =>
    request<{ sources: number; views: number; triggers: number; agents: number;
              mcp_servers: number;
              names: { sources: string[]; views: string[]; triggers: string[];
                       agents: string[]; mcp_servers: string[] } }>("/api/catalog/import", {
      method: "POST",
      body: JSON.stringify({ yaml, mode }),
    }),
};

export type TracingStatus = {
  enabled: boolean; enabled_source: string; active: boolean;
  provider: string; provider_source: string;
  endpoint: string; endpoint_source: string;
  key_configured: boolean; key_source: string; key_stored: boolean;
  headers_configured: boolean; headers_source: string;
  instance: string; providers: string[]; rius_console_url: string;
};
