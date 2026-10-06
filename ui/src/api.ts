import type {
  AgentInfo,
  ApiKey, ExternalAgent,
  CatalogDescribe, ConnectorSpec, DiscoverProposal, DispatchDetail, DispatchLogEntry, Entity, EnvScan,
  AgentPreset, AgentRun, BuiltinAgent, Handoff,
  GithubAppTest, GithubCredential,
  LabelFacet, ModelUsage, QueryLogEntry,
  McpServer, Plan, ProjectSetup, Resources, SetupConnect, SetupProblem, SetupStep, Template, Project, ProjectObjectKind, ProjectSummary, ProjectUpdateReport,
  ProjectHealth, ProjectOutline, ProjectResultDetail, ProjectResults,
  Skill, SkillSummary,
  Source, SourceEvent, SourceFieldsProfile, Subscription, TestResult, Usage,
  TimelineEventRow, Trigger, ModelProvider, ModelProviders,
} from "./types";
import type { TimelineThread } from "./components/ProjectActivity";

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

// Every public channel, plus the private ones the bot is in (Slack shows a bot no other).
// `is_private` exists because Slack never shows a private channel as "#name": labelling it that
// way would name something the user can't find by that name. `is_member`: Tares is in it.
export type SlackChannel = { id: string; name: string; is_private: boolean; is_member: boolean };
export type SlackChannels = {
  channels: SlackChannel[];
  reason: null | "no_token" | "missing_scope" | "error";
  detail?: string;
  stale?: boolean;   // a re-read failed; this is the last good list (detail says why)
};

/** One workspace in the control plane's list (Tares Cloud). Only `active` ones can be opened. */
export type CloudWorkspace = {
  slug: string; host: string; url: string; role: string; owner_email: string;
  state: "provisioning" | "active" | "error" | "suspended" | string;
  is_default: boolean; open_url: string;
};
export type CloudWorkspacesResult =
  | { status: "ok"; workspaces: CloudWorkspace[];
      links: { all?: string; new?: string; account?: string } }
  | { status: "signed_out" }   // 401: the control-plane session expired, the cell key still works
  | { status: "unknown" };     // any other answer, or the network failed

/** This workspace as the control plane sees it (Tares Cloud, TR-375): GET workspace_api_url. */
export type CloudWorkspaceOverview = {
  slug: string; state: string; plan: string; image_tag: string;
  storage_gb: number; storage_max_gb: number;
  storage_used_bytes: number | null; storage_pct_used: number | null; usage_checked_at?: string | null;
  host: string; role: "owner" | "member" | string;
  trial?: { state: "active" | "exhausted" | "superseded" | string; spend_usd: number | null; credit_usd: number };
  you: { email: string; name: string | null };
  is_default: boolean; default_url: string;
};
export type CloudMember = {
  user_id: string; email: string; name: string | null;
  role: "owner" | "member" | string; status: "active" | "invited" | string;
};
/** A call to the control plane from this workspace: the answer, signed out of Tares Cloud (401),
 *  or any other failure with the control plane's own `detail` (contract §2). Never throws. */
export type CloudResult<T> =
  | { status: "ok"; data: T }
  | { status: "signed_out" }
  | { status: "error"; code: number; detail: string };

/** Calls the control plane with the person's Tares Cloud session cookie. Reads send no custom
 *  header, so they stay simple requests; writes say which workspace asks (X-Tares-Workspace) and,
 *  with a body, that it is JSON. Never the `request` helper: a 401 here is the control plane's
 *  session, not this console's key. */
async function cloudCall<T>(url: string, init?: { method?: string; slug?: string; body?: unknown }): Promise<CloudResult<T>> {
  const method = init?.method ?? "GET";
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Tares-Workspace"] = init?.slug ?? "";
  if (init?.body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(url, { method, credentials: "include", headers,
                             body: init?.body !== undefined ? JSON.stringify(init.body) : undefined });
  } catch {
    return { status: "error", code: 0, detail: "Tares Cloud could not be reached. Try again in a moment." };
  }
  if (res.status === 401) return { status: "signed_out" };
  let body: unknown = null;
  try { body = await res.json(); } catch { /* an empty or non-JSON body */ }
  if (!res.ok) {
    const d = (body as { detail?: unknown } | null)?.detail;
    const detail = typeof d === "string" ? d
      : res.status === 404 ? "Only the workspace owner can do that."
      : `Tares Cloud answered ${res.status}${res.statusText ? ` ${res.statusText}` : ""}.`;
    return { status: "error", code: res.status, detail };
  }
  return { status: "ok", data: body as T };
}

/** A URL plus one path segment, kept clear of the query string. */
function cloudPath(base: string, ...segments: string[]): string {
  const u = new URL(base, window.location.href);
  u.pathname = [u.pathname.replace(/\/+$/, ""), ...segments.map(encodeURIComponent)].join("/");
  return u.toString();
}

export const cloudApi = {
  overview: (url: string) => cloudCall<CloudWorkspaceOverview>(url),
  growStorage: (url: string, slug: string, storage_gb: number) =>
    cloudCall<Partial<CloudWorkspaceOverview>>(url, { method: "PATCH", slug, body: { storage_gb } }),
  deleteWorkspace: (url: string, slug: string) => {
    const u = new URL(url, window.location.href);
    u.searchParams.set("confirm", slug);
    return cloudCall<{ slug: string; deleted: boolean; next?: string }>(u.toString(), { method: "DELETE", slug });
  },
  members: (url: string) => cloudCall<{ members: CloudMember[] }>(cloudPath(url, "members")),
  invite: (url: string, slug: string, email: string) =>
    cloudCall<CloudMember>(cloudPath(url, "members"), { method: "POST", slug, body: { email } }),
  removeMember: (url: string, slug: string, userId: string) =>
    cloudCall<unknown>(cloudPath(url, "members", userId), { method: "DELETE", slug }),
  /** Pin this workspace as the one to open after sign-in, or unpin it (null). */
  setDefault: (defaultUrl: string, slug: string, pin: string | null) =>
    cloudCall<unknown>(defaultUrl, { method: "PUT", slug, body: { slug: pin } }),
};

/** A failed call: the daemon's plain message, plus the HTTP status for the few places that act on
 *  it (setup planning says "add a model provider" on a 409). */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

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
    throw new ApiError(detail, res.status);
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
    github_connect_url?: string;   // cloud only: "Connect GitHub" (the GlassFlow-owned App)
    slack_connect_url?: string;    // cloud only: "Connect Slack" (the control plane's Slack install)
    workspaces_url?: string;       // cloud only: the signed-in person's workspaces, for the switcher
    workspace_api_url?: string;    // cloud only: this workspace in the control plane (Settings > Workspace)
    logout_url?: string;           // cloud only: Sign out ends the Tares Cloud session there
    detail?: string; pct_used?: number | null;
  }>("/health"),
  // The control plane's list of the person's workspaces (Tares Cloud). A plain cross-origin GET
  // with the control-plane session cookie and NO custom headers, so it stays a simple request with
  // no preflight. Never the `request` helper: a 401 here means "signed out of the control plane",
  // not "this console's key expired". Never throws.
  cloudWorkspaces: async (url: string): Promise<CloudWorkspacesResult> => {
    try {
      const res = await fetch(url, { credentials: "include" });
      if (res.status === 401) return { status: "signed_out" };
      if (!res.ok) return { status: "unknown" };
      const body = await res.json();
      if (!body || !Array.isArray(body.workspaces)) return { status: "unknown" };
      return { status: "ok", workspaces: body.workspaces, links: body.links ?? {} };
    } catch {
      return { status: "unknown" };
    }
  },
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
              url_configured: boolean; slack_configured?: boolean;
              default_provider?: { id: string; name: string; kind: string } | null }>("/api/capabilities"),
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
  dependents: (kind: "source" | "trigger", name: string) =>
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

  triggers: () => request<Trigger[]>("/api/triggers"),
  createTrigger: (body: TriggerBody) =>
    request("/api/triggers", { method: "POST", body: JSON.stringify(body) }),
  updateTrigger: (name: string, body: TriggerBody) =>
    request(`/api/triggers/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTrigger: (name: string) => request(`/api/triggers/${name}`, { method: "DELETE" }),
  pauseTrigger: (name: string) => request(`/api/triggers/${name}/pause`, { method: "POST" }),
  resumeTrigger: (name: string) => request(`/api/triggers/${name}/resume`, { method: "POST" }),
  // ── Tares agents: a first look when a trigger fires (managed under /builtin) ──
  // with a project: each agent's trigger, handoffs and on/off are that project's wiring
  builtinAgents: (project?: string) =>
    request<{ agents: BuiltinAgent[]; key_configured: boolean; key_source: string;
              models: string[]; default_model: string; slack_workspace: boolean;
              providers: ModelProvider[]; default_provider: string | null;
              default_models: Record<string, string>;   // per provider id, what "" resolves to
              default_max_rounds: number; default_max_rounds_with_mcp: number;
              max_rounds_limit: number;
              presets: AgentPreset[] }>(`/api/agents/builtin${project ? `?project=${encodeURIComponent(project)}` : ""}`),
  createBuiltinAgent: (body: AgentBody) =>
    request<{ ok: boolean; enabled: boolean }>("/api/agents/builtin",
      { method: "POST", body: JSON.stringify(body) }),
  updateBuiltinAgent: (name: string, body: AgentBody) =>
    request(`/api/agents/builtin/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteBuiltinAgent: (name: string) => request(`/api/agents/builtin/${name}`, { method: "DELETE" }),
  // on or off in one project (its wiring); enable without one: the project that made it
  enableBuiltinAgent: (name: string, project?: string) =>
    request(`/api/agents/builtin/${name}/enable${project ? `?project=${encodeURIComponent(project)}` : ""}`, { method: "POST" }),
  disableBuiltinAgent: (name: string, project?: string) =>
    request(`/api/agents/builtin/${name}/disable${project ? `?project=${encodeURIComponent(project)}` : ""}`, { method: "POST" }),
  // with a project: the runs that belong to it (its wiring started them)
  builtinAgentRuns: (name: string, limit = 20, offset = 0, status = "", project?: string) =>
    request<AgentRun[]>(`/api/agents/builtin/${encodeURIComponent(name)}/runs?limit=${limit}&offset=${offset}`
      + (status ? `&status=${encodeURIComponent(status)}` : "")
      + (project ? `&project=${encodeURIComponent(project)}` : "")),
  rerunAgentRun: (name: string, runId: string) =>
    request<{ ok: boolean; run_id: string }>(
      `/api/agents/builtin/${encodeURIComponent(name)}/runs/${encodeURIComponent(runId)}/rerun`,
      { method: "POST" }),
  // The cell's Anthropic spend meter: totals + per-day, split agent runs vs Ask.
  modelUsage: (days = 30) => request<ModelUsage>(`/api/usage/model?days=${days}`),

  // The Anthropic key Tares agents run on. Never returned — only whether one resolves and where.
  gatewayStatus: () =>
    request<{ configured: boolean; url: string; source: string; stored: boolean; token_stored: boolean;
              default_url: string }>("/api/settings/gateway"),
  setGateway: (url: string, token: string) =>
    request<{ ok: boolean; configured: boolean; url: string; source: string; stored: boolean;
              token_stored: boolean }>("/api/settings/gateway",
      { method: "PUT", body: JSON.stringify({ url, token }) }),
  clearGateway: () =>
    request<{ ok: boolean; configured: boolean; source: string }>("/api/settings/gateway",
      { method: "DELETE" }),
  // The model providers a cell holds (TR-301). Credentials are write-only; blank key keeps the
  // stored one. `new` as the id creates an entry.
  providers: () => request<ModelProviders>("/api/settings/providers"),
  saveProvider: (id: string, body: { kind: string; name?: string; key?: string; base_url?: string }) =>
    request<ModelProviders & { ok: boolean; id: string }>(`/api/settings/providers/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  deleteProvider: (id: string) =>
    request<ModelProviders & { ok: boolean }>(`/api/settings/providers/${encodeURIComponent(id)}`,
      { method: "DELETE" }),
  refreshProviderModels: (id: string) =>
    request<ModelProviders & { ok: boolean; models: string[]; error: string }>(
      `/api/settings/providers/${encodeURIComponent(id)}/models`, { method: "POST" }),
  setDefaultProvider: (id: string) =>
    request<ModelProviders & { ok: boolean }>("/api/settings/providers/default",
      { method: "PUT", body: JSON.stringify({ id }) }),
  anthropicKeyStatus: () =>
    request<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>(
      "/api/settings/anthropic-key"),
  setAnthropicKey: (key: string) =>
    request<{ ok: boolean; source: string; note?: string }>("/api/settings/anthropic-key",
      { method: "PUT", body: JSON.stringify({ key }) }),
  clearAnthropicKey: () =>
    request<{ ok: boolean; configured: boolean }>("/api/settings/anthropic-key",
      { method: "DELETE" }),

  // Runs per agent per day (TR-325): the console value, else the environment, else the default.
  agentLimits: () => request<AgentLimits>("/api/settings/agents"),
  setAgentLimits: (body: { daily_cap: number | string | null }) =>
    request<AgentLimits & { ok: boolean }>("/api/settings/agents",
      { method: "PUT", body: JSON.stringify(body) }),

  // Agent tracing: where runs are exported and whether. Secrets are write-only; the status says
  // what resolves and where from (console vs environment).
  tracingStatus: () => request<TracingStatus>("/api/settings/tracing"),
  setTracing: (body: { enabled?: boolean; provider?: string; endpoint?: string; api_key?: string; headers?: string }) =>
    request<TracingStatus & { ok: boolean; note?: string }>("/api/settings/tracing",
      { method: "PUT", body: JSON.stringify(body) }),

  // The Slack bot token behind slack:// subscriptions. Same contract as the Anthropic key: the
  // value never leaves the server, only whether one resolves and where from.
  slackTokenStatus: () =>
    request<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean;
              team?: { id: string; name: string } | null }>(   // the Slack team, set by Tares Cloud
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
  resources: () => request<Resources>("/api/resources"),

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
                            headers?: Record<string, string>; project?: string }) =>
    request<{ ok: boolean }>("/api/mcp-servers", { method: "POST", body: JSON.stringify(body) }),
  updateMcpServer: (name: string, body: { name: string; url: string; auth_header?: string; auth_value?: string;
                                          headers?: Record<string, string>; project?: string }) =>
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
    request<GithubAppTest>(`/api/integrations/github/${encodeURIComponent(name)}/test`,
      { method: "POST" }),
  // Create GitHub App: the manifest and where to post it (GitHub's manifest flow)
  createGithubApp: (body: { name: string; org?: string; app_name?: string; public_url?: string }) =>
    request<{ action: string; manifest: string; hook_url: string; warning: string | null }>(
      "/api/integrations/github/apps", { method: "POST", body: JSON.stringify(body) }),
  // the credential's GitHub MCP server (made once, reused): what an agent reads/writes repos with
  githubCredentialMcp: (name: string, write: boolean) =>
    request<{ ok: boolean; server: string }>(`/api/integrations/github/${encodeURIComponent(name)}/mcp`,
      { method: "POST", body: JSON.stringify({ write }) }),
  githubAppInstallLink: (name: string) =>
    request<{ url: string }>(`/api/integrations/github/${encodeURIComponent(name)}/install`),
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
  // the project's threads, newest first; page with `before` = the previous page's next_before
  projectTimeline: (id: string, q: { trigger?: string; agent?: string; outcome?: string;
                                     entity?: string; before?: string | null; limit?: number } = {}) =>
    request<{ threads: TimelineThread[]; next_before: string | null }>(
      `/api/projects/${encodeURIComponent(id)}/timeline?` + new URLSearchParams(
        Object.entries(q).filter(([, v]) => v != null && v !== "").map(([k, v]) => [k, String(v)])).toString()),
  // template "custom" takes `objects` ({kind, name} each) instead of params
  // `goal`: one line, <= 200 chars; a template's own goal applies when it is left out
  createProject: (body: { template: string; name?: string; params?: Record<string, unknown>;
                          objects?: { kind: ProjectObjectKind; name: string }[]; goal?: string }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  // a body with only `goal` works on every project, the default one included; "" or null clears it
  updateProject: (id: string, body: { params?: Record<string, unknown>; name?: string;
                                      objects?: { kind: ProjectObjectKind; name: string }[];
                                      goal?: string | null }) =>
    request<Project & { report?: ProjectUpdateReport }>(`/api/projects/${encodeURIComponent(id)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  // The goal-first page: the setup in plain sentences, what the agents concluded, and whether the
  // project is working. All computed by the daemon, no model call.
  projectOutline: (id: string) =>
    request<ProjectOutline>(`/api/projects/${encodeURIComponent(id)}/outline`),
  // newest first; page with `before` = the previous page's next_before
  projectResults: (id: string, q: { limit?: number; before?: string | null } = {}) =>
    request<ProjectResults>(
      `/api/projects/${encodeURIComponent(id)}/results?` + new URLSearchParams(
        Object.entries(q).filter(([, v]) => v != null && v !== "").map(([k, v]) => [k, String(v)])).toString()),
  projectResult: (id: string, runId: string) =>
    request<ProjectResultDetail>(`/api/projects/${encodeURIComponent(id)}/results/${encodeURIComponent(runId)}`),
  // the response body is not relied on: the page refetches the result after it
  setResultHandled: (id: string, runId: string, handled: boolean) =>
    request<unknown>(`/api/projects/${encodeURIComponent(id)}/results/${encodeURIComponent(runId)}/handled`,
      { method: "POST", body: JSON.stringify({ handled }) }),
  projectHealth: (id: string) =>
    request<ProjectHealth>(`/api/projects/${encodeURIComponent(id)}/health`),
  // Triggers, agents and MCP servers always go with the project. `deleteSources`: which of its
  // sources to delete too; one another project still uses is kept (and reported) either way.
  deleteProject: (id: string, purgeEvents = false, deleteSources: string[] = []) =>
    request<{ ok: boolean; deleted?: string[]; released?: string[]; kept?: string[]; purged_events?: number }>(
      `/api/projects/${encodeURIComponent(id)}?purge_events=${purgeEvents}`
      + `&delete_sources=${encodeURIComponent(deleteSources.length ? deleteSources.join(",") : "none")}`,
      { method: "DELETE" }),
  // Sources are shared: a project lists the ones it uses. Removing one is refused while a trigger
  // of the project reads it.
  addProjectSource: (id: string, name: string) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/sources`,
      { method: "POST", body: JSON.stringify({ name }) }),
  removeProjectSource: (id: string, name: string) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/sources/${encodeURIComponent(name)}`,
      { method: "DELETE" }),
  // Project keys and the external agents that joined a project (TR-335, TR-336). A project key
  // reads that project only and records findings in it; its secret is in the create response only.
  projectKeys: (id: string) =>
    request<{ keys: ApiKey[]; enforced: boolean }>(`/api/projects/${encodeURIComponent(id)}/keys`),
  createProjectKey: (id: string, name: string) =>
    request<{ id: string; name: string; scopes: string[]; secret: string; project: string }>(
      `/api/projects/${encodeURIComponent(id)}/keys`, { method: "POST", body: JSON.stringify({ name }) }),
  externalAgents: (id: string) =>
    request<{ agents: ExternalAgent[] }>(`/api/projects/${encodeURIComponent(id)}/external-agents`),
  unsubscribeProject: (id: string, sid: string) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/subscribe/${encodeURIComponent(sid)}`,
      { method: "DELETE" }),
  // A project's skills (TR-332). Upload takes a SKILL.md as it is: front matter, then the body.
  skills: (id: string) => request<SkillSummary[]>(`/api/projects/${encodeURIComponent(id)}/skills`),
  skill: (id: string, name: string) =>
    request<Skill>(`/api/projects/${encodeURIComponent(id)}/skills/${encodeURIComponent(name)}`),
  // skills are shared (P-TR-216): every skill on Tares, and a project using one of them
  cellSkills: () =>
    request<{ name: string; description: string; used_by: { id: string; name: string }[] }[]>("/api/skills"),
  useSkill: (id: string, name: string) =>
    request<Skill>(`/api/projects/${encodeURIComponent(id)}/skills/${encodeURIComponent(name)}/use`,
      { method: "POST" }),
  createSkill: (id: string, body: { name: string; description: string; body: string }) =>
    request<Skill>(`/api/projects/${encodeURIComponent(id)}/skills`,
      { method: "POST", body: JSON.stringify(body) }),
  updateSkill: (id: string, name: string, body: { description?: string; body?: string }) =>
    request<Skill>(`/api/projects/${encodeURIComponent(id)}/skills/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(body) }),
  deleteSkill: (id: string, name: string) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/skills/${encodeURIComponent(name)}`,
      { method: "DELETE" }),
  uploadSkill: (id: string, text: string) =>
    request<Skill & { created: boolean }>(`/api/projects/${encodeURIComponent(id)}/skills/upload`,
      { method: "POST", body: text, headers: { "content-type": "text/markdown" } }),
  // ── Goal-first setup: plan from a goal, adjust in plain words, apply, then connect and try ──
  planSetup: (body: { goal: string; who?: "tares" | "own"; existing_sources?: boolean }) =>
    request<{ plan: Plan }>("/api/setup/plan", { method: "POST", body: JSON.stringify(body) }),
  // the guided setup's opening line from the person's GitHub repositories (TR-262)
  setupGithubSuggestion: () =>
    request<{ available: boolean; suggestion: string | null; repos: string[] }>("/api/setup/github-suggestion"),
  adjustSetup: (plan: Plan, instruction: string) =>
    request<{ plan: Plan }>("/api/setup/adjust", { method: "POST", body: JSON.stringify({ plan, instruction }) }),
  checkSetup: (plan: Plan, project?: string) =>
    request<{ plan: Plan; problems: SetupProblem[] }>("/api/setup/check",
      { method: "POST", body: JSON.stringify({ plan, project }) }),
  applySetup: (plan: Plan, project?: string) =>
    request<{ project: Project; plan: Plan; connect: SetupConnect }>("/api/setup/apply",
      { method: "POST", body: JSON.stringify({ plan, project }) }),
  // a draft project: made on "Plan it", planned in the background, kept while it is edited
  createDraft: (body: { goal: string; who?: "tares" | "own"; name?: string; description?: string }) =>
    request<{ project: Project }>("/api/setup/drafts", { method: "POST", body: JSON.stringify(body) }),
  replanDraft: (id: string, body: { goal?: string; who?: "tares" | "own"; name?: string; description?: string;
                                    instruction?: string; plan?: Plan }) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/setup/plan`,
      { method: "POST", body: JSON.stringify(body) }),
  saveDraftPlan: (id: string, plan: Plan) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}/setup`,
      { method: "PUT", body: JSON.stringify({ plan }) }),
  projectSetup: (id: string) =>
    request<ProjectSetup>(`/api/projects/${encodeURIComponent(id)}/setup`),
  setSetupStep: (id: string, step: SetupStep) =>
    request<unknown>(`/api/projects/${encodeURIComponent(id)}/setup`,
      { method: "PUT", body: JSON.stringify({ step }) }),
  // ingests the plan's example event into a push source, marked practice=true
  sendSetupTestEvent: (id: string, source: string) =>
    request<unknown>(`/api/projects/${encodeURIComponent(id)}/setup/test-event`,
      { method: "POST", body: JSON.stringify({ source }) }),
  // Tares agents answer run_id; an own agent answers dispatch_id
  runSetupPractice: (id: string) =>
    request<{ run_id?: string; dispatch_id?: string }>(`/api/projects/${encodeURIComponent(id)}/setup/practice`,
      { method: "POST" }),
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

  describe: (handle: string) => request<CatalogDescribe>(`/catalog/${handle}`),

  entities: (label?: string) =>
    request<{ labels?: LabelFacet[]; label?: string; sources?: string[]; values?: Entity[] }>(
      label ? `/api/entities?label=${encodeURIComponent(label)}` : "/api/entities"),

  // Label-native read. `selector` is a {label: value} conjunction (strict AND). Without `project`
  // or `sources` it reads every source; either narrows which sources are read. Returns the
  // rendered payload, contributing sources, and structured rows (each with its per-event labels,
  // for the console timeline).
  read: (selector: Record<string, string>, window: string, scope?: { project?: string; sources?: string[] }) =>
    request<{ payload: string; count: number; sources: string[]; rows: TimelineEventRow[] }>("/read", {
      method: "POST",
      body: JSON.stringify({ selector, window, client: "ui", ...(scope ?? {}) }),
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
    request<{ sources: number; triggers: number; agents: number;
              mcp_servers: number;
              names: { sources: string[]; triggers: string[];
                       agents: string[]; mcp_servers: string[] } }>("/api/catalog/import", {
      method: "POST",
      body: JSON.stringify({ yaml, mode }),
    }),
};

// What a trigger is saved from. `project` omitted = the default project.
export type TriggerBody = Pick<Trigger, "name" | "sources" | "filters" | "condition" | "emit" | "cooldown">
  & { project?: string; key_field?: string | null; description?: string };

export type AgentBody = {
  name: string; trigger: string; prompt: string; project?: string;
  slack_webhook?: string; slack_webhook_clear?: boolean; model?: string; provider?: string;
  slack_channel?: string; webhook_url?: string; webhook_token?: string; mcp_servers?: string[];
  max_rounds?: number | null; budget_usd?: number | null; webhook_key_label?: string;
  handoffs?: Handoff[];
};

export type AgentLimits = {
  daily_cap: number;
  daily_cap_source: "console" | "env" | "default";
  default: number;
  env: string;
};

export type TracingStatus = {
  enabled: boolean; enabled_source: string; active: boolean;
  provider: string; provider_source: string;
  endpoint: string; endpoint_source: string;
  key_configured: boolean; key_source: string; key_stored: boolean;
  headers_configured: boolean; headers_source: string;
  instance: string; providers: string[]; rius_console_url: string;
};
