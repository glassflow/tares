import type {
  ApiKey, Person, Whoami, GithubAppTest, GithubCredential, ModelUsage, Project,
  ProjectSummary, Area, ClaudeCodeUsage, CrewSettingField, CrewSettingsView, Source, Usage, Trigger,
  ModelProviders,
} from "./types";
import { ENTERPRISE_URL, licenceSentence, licenceWordsOn } from "./licenceWords";

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
    const b = body as { detail?: unknown; code?: unknown; message?: unknown } | null;
    const d = b?.detail;
    // a workspace without a licence answers an invite with 402 {code: "licence_required", message, url};
    // the cloud calls only happen on a hosted cell, which never says licence, so the words are ours
    const detail = res.status === 402 && b?.code === "licence_required" ? licenceSentence()
      : typeof d === "string" ? d
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
 *  it (setup planning says "add a model provider" on a 409). `retryAfter` is the seconds a 429
 *  asks to wait (GitHub rate limiting), when it says. */
export class ApiError extends Error {
  status: number;
  retryAfter?: number;
  constructor(message: string, status: number, retryAfter?: number) {
    super(message); this.status = status; this.retryAfter = retryAfter;
  }
}

/** The cell answered a team action with 402 `licence_required`: the action needs an enterprise
 *  licence. `message` is the plain sentence and `url` the page that explains it. Show it with
 *  `<LicenceNeeded>`, never as an error. */
export class LicenceRequired extends ApiError {
  url: string;
  constructor(message: string, status = 402, url: string = ENTERPRISE_URL) {
    super(message, status); this.url = url;
  }
}

export const isLicenceRequired = (e: unknown): e is LicenceRequired => e instanceof LicenceRequired;

function unauthorized() {
  auth.clear();
  window.dispatchEvent(new Event("tares-auth-required"));
}

/** GET /api/me/slack. `found` is null until the cell has looked the person up on Slack;
 *  `problem` is a cell-wide one (the app cannot look people up by email). */
export type MySlack = {
  on: boolean; connected: boolean; claimed: boolean; has_email: boolean;
  found: boolean | null; problem: string | null;
};

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
      // the licence answer keeps its keys at the top level of the body, not under `detail`
      if (body && typeof body === "object" && body.code === "licence_required") {
        // the cell's own words and link only where licence words are allowed; a Tares Cloud cell
        // (or one not yet known to be self-hosted) gets the plain sentence and no link
        const on = licenceWordsOn();
        throw new LicenceRequired(
          on && typeof body.message === "string" && body.message ? body.message : licenceSentence(),
          402,
          on ? (typeof body.url === "string" && body.url ? body.url : ENTERPRISE_URL) : "",
        );
      }
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch (e) {
      if (isLicenceRequired(e)) throw e;
      /* non-JSON error body */
    }
    const wait = Number(res.headers.get("Retry-After"));
    throw new ApiError(detail, res.status, wait > 0 ? wait : undefined);
  }
  return res.json() as Promise<T>;
}

/** A repo as `owner/name`: a pasted GitHub address (https://github.com/o/n, .git, git@github.com:o/n)
 *  is cut down to it, so it fits the area routes. The daemon checks the rest. */
export function ownerName(repo: string): string {
  const m = repo.trim().match(/^(?:(?:https?:\/\/)?(?:www\.)?github\.com\/|git@github\.com:)([^/]+)\/([^/?#]+)/i);
  const r = m ? `${m[1]}/${m[2].replace(/\.git$/i, "")}` : repo.trim();
  if (r.split("/").length !== 2) throw new Error(`${repo.trim()} is not a repo: write it as owner/name, like glassflow/tares`);
  return r;
}

/** What GET /api/licence answers (TR-912). Never the key text. `ee_allowed` is the cell's own answer. */
export type LicenceState = {
  valid: boolean; source: "env" | "settings" | "none"; kid: string | null; org: string | null;
  seats: number | null; used: number; features: string[]; expires: string | null; days_left: number | null;
  expired: boolean; over_seats: boolean; ee_allowed: boolean; reason: string | null; stored: boolean; env_set: boolean;
};

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
    linear_connect_url?: string;   // cloud only: "Connect Linear" (the GlassFlow Linear app)
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
  capabilities: () =>
    request<{ version?: string | null; discover_docker: boolean; agent_key_configured?: boolean;
              url_configured: boolean; slack_configured?: boolean;
              default_provider?: { id: string; name: string; kind: string } | null }>("/api/capabilities"),
  keys: () => request<{ keys: ApiKey[]; enforced: boolean; scopes: string[] }>("/api/keys"),
  createKey: (name: string, scopes: string[], personId?: string) =>
    request<{ id: string; name: string; scopes: string[]; secret: string; person: { id: string; name: string } | null }>(
      "/api/keys", { method: "POST", body: JSON.stringify({ name, scopes, ...(personId ? { person_id: personId } : {}) }) }),
  revokeKey: (id: string) => request<{ ok: boolean }>(`/api/keys/${id}`, { method: "DELETE" }),
  whoami: () => request<Whoami>("/api/whoami"),
  licence: () => request<LicenceState>("/api/licence"),
  setLicence: (key: string) =>
    request<LicenceState & { env_in_use?: boolean }>("/api/licence", { method: "PUT", body: JSON.stringify({ key }) }),
  deleteLicence: () => request<{ ok?: boolean }>("/api/licence", { method: "DELETE" }),
  people: () => request<{ people: Person[] }>("/api/people"),
  removePerson: (id: string) => request<{ removed: boolean }>(`/api/people/${encodeURIComponent(id)}`, { method: "DELETE" }),
  updatePerson: (id: string, body: { name?: string; email?: string | null; emails?: string[]; github?: string | null; is_admin?: boolean; picture?: string | null }) =>
    request<Person>(`/api/people/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),

  source: (name: string) => request<Source>(`/api/sources/${name}`),
  updateSource: (name: string, body: object) =>
    request(`/api/sources/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  labelPreview: (source: string, label: Record<string, unknown>) =>
    request<{ sampled: number; distinct_before: number; distinct_after: number;
              results: { from: string; to: string; events: number }[] }>(
      "/api/labels/preview", { method: "POST", body: JSON.stringify({ source, label }) }),

  triggers: () => request<Trigger[]>("/api/triggers"),
  createTrigger: (body: TriggerBody) =>
    request("/api/triggers", { method: "POST", body: JSON.stringify(body) }),
  updateTrigger: (name: string, body: TriggerBody) =>
    request(`/api/triggers/${name}`, { method: "PUT", body: JSON.stringify(body) }),
  // The cell's Anthropic spend meter: totals + per-day, split agent runs vs Ask.
  modelUsage: (days = 30) => request<ModelUsage>(`/api/usage/model?days=${days}`),

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
  setAnthropicKey: (key: string) =>
    request<{ ok: boolean; source: string; note?: string }>("/api/settings/anthropic-key",
      { method: "PUT", body: JSON.stringify({ key }) }),
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

  // The caller's own Slack message when something new waits for them (TR-1458). 400 for a key
  // that is no person.
  mySlack: () => request<MySlack>("/api/me/slack"),
  setMySlack: (on: boolean) =>
    request<MySlack>("/api/me/slack", { method: "PUT", body: JSON.stringify({ on }) }),

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
  githubAppInstallLink: (name: string) =>
    request<{ url: string }>(`/api/integrations/github/${encodeURIComponent(name)}/install`),
  projectsWithHidden: () => request<{ projects: Project[] }>("/api/projects"),
  projectSummary: (id: string) =>
    request<ProjectSummary>(`/api/projects/${encodeURIComponent(id)}/summary`),
  // close a factory project whose tickets are all finished; `words` is what the person said
  finishProject: (id: string, words: string) =>
    request<{ project: string; name: string; finished: unknown }>(
      `/api/projects/${encodeURIComponent(id)}/finish`, { method: "POST", body: JSON.stringify({ words }) }),
  // Triggers, agents and MCP servers always go with the project. `deleteSources`: which of its
  // sources to delete too; one another project still uses is kept (and reported) either way.
  deleteProject: (id: string, purgeEvents = false, deleteSources: string[] = []) =>
    request<{ ok: boolean; deleted?: string[]; released?: string[]; kept?: string[]; purged_events?: number }>(
      `/api/projects/${encodeURIComponent(id)}?purge_events=${purgeEvents}`
      + `&delete_sources=${encodeURIComponent(deleteSources.length ? deleteSources.join(",") : "none")}`,
      { method: "DELETE" }),
  areas: () => request<Area[]>("/api/areas"),
  // null = unchanged; `clear` unsets a personal value so the team's applies again
  setCrewSettings: (body: Partial<Record<CrewSettingField, string | boolean | number | null>> & { clear?: CrewSettingField[] }) =>
    request<CrewSettingsView>("/api/crew/settings", { method: "PUT", body: JSON.stringify(body) }),
  askStationContext: (name: string, crew?: string | null) =>
    request<{ session: string | null; asked_at: string }>(`/api/crew/${encodeURIComponent(name)}/context${crew ? `?crew=${encodeURIComponent(crew)}` : ""}`, { method: "POST" }),
  claudeCodeUsage: (days = 7) => request<ClaudeCodeUsage>(`/api/claude-code/usage?days=${days}`),
  pauseProject: (id: string, sources = false) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/pause`, { method: "POST", body: JSON.stringify({ sources }) }),
  resumeProject: (id: string) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/resume`, { method: "POST" }),
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

  // What this instance is using on disk. `max_bytes`/`pct_used` are null unless the operator set
  // TARES_MAX_DB_SIZE — see the Usage type before rendering any of it.
  usage: () => request<Usage>("/api/usage"),

};

// What a trigger is saved from. `project` omitted = the default project.
export type TriggerBody = Pick<Trigger, "name" | "sources" | "filters" | "condition" | "emit" | "cooldown">
  & { project?: string; key_field?: string | null; description?: string };

