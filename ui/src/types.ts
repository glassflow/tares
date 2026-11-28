export interface SourceHealth {
  status: string;
  last_poll_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  consecutive_errors: number;
  polls: number;
  events_since_start: number;
  events_total: number;
  last_ingest: string | null;
}

export interface Source {
  name: string;
  type: string;
  connector: string;
  poll: string;
  config: Record<string, unknown>;
  paused: boolean;
  health: SourceHealth | null;
  ingest_key?: string;   // stable path segment for push endpoints: /ingest/<ingest_key>
  owned_by?: string | null;   // the project that created it, if any
  customized?: boolean;       // edited by hand since; the project keeps that version
  projects?: string[];        // every project the source is a member of (ids)
}

export interface ConnectorField {
  name: string;
  type: "string" | "number" | "json" | "map" | "list" | "bool";
  required: boolean;
  help: string;
  secret?: boolean;          // render as a password input (tokens, DSNs)
  discover_input?: boolean;  // Discover needs this field — shown above the Discover panel
  default?: unknown;         // what the connector uses when the field is empty; prefilled on a fresh source
  choices?: string[];        // a fixed set of values: rendered as a dropdown, anything else is refused on save
  label?: string;            // the words shown for the field; the name stays the config key
  item?: ConnectorField[];   // for type "list": the sub-fields of each row
}

export interface ConnectorSpec {
  label: string;
  mode: "poll" | "push" | "reference";   // reference: documents added by hand, no events flow
  discover?: boolean;
  poll?: string;   // connector-specific default poll interval (e.g. github: "2m")
  internal?: boolean;   // provisioned by Tares itself (agent findings) — not offered in the UI
  // created with its credential (the GitHub App source): never offered as a new kind, but an
  // existing one is picked like any other source (under the GitHub kind)
  credential_managed?: boolean;
  description: string;
  fields: ConnectorField[];
  provides?: { name: string; primary?: boolean; help?: string }[];   // synthesized label fields
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  project?: string | null;      // a project key: reads that project only (TR-335)
  person?: { id: string; name: string } | null;   // who the key belongs to; null: nobody yet
}

// Someone on the cell. Keys belong to a person; the credential in a request says who is calling.
export interface Person {
  id: string;
  name: string;
  email: string | null;
  emails: string[];
  github: string | null;
  is_admin: boolean;
  created_via: string;
  /** An https address of their picture (TR-1016); null shows their initials. */
  picture?: string | null;
}

// /api/whoami: the credential making the request, and the person it acts as (null for the root
// token, and for a key nobody has bound once the cell has more than one person).
export interface Whoami {
  id: string;
  name: string;
  kind: "system" | "key" | "project" | "open";
  person: { id: string; name: string; email: string | null; picture?: string | null } | null;
  scopes: string[];
  project?: string;
  project_name?: string | null;
  mcp_object_tools?: boolean;
}

// One filter row on a trigger: which events it counts. Same ops the daemon accepts.
export interface TriggerFilter {
  field: string;
  op: "eq" | "neq" | "contains" | "gt" | "lt" | "gte" | "lte" | "in";
  value: string | number | string[];   // a list for `in`
}

export interface TriggerCondition {
  aggregate: string;
  predicate: string;
  window: string;
  field?: string | null;
  group_by?: string[];
  // a schedule instead of a condition (TR-320): fire every interval, summarized by these labels
  every?: string;
  summary_by?: string[];
}

// A trigger reads its own sources (members of its project), narrowed by filters, grouped by the
// entity label (key_field; empty = the first source's primary label).
export interface Trigger {
  name: string;
  project?: string;   // the project it belongs to; omitted on create = the default project
  sources: string[];
  filters?: TriggerFilter[];
  key_field?: string | null;
  description?: string | null;   // what wakes it in plain words, follows "When"; empty = said from the rule
  condition: TriggerCondition;
  emit: Record<string, unknown>;
  cooldown: string;
  paused?: boolean;   // paused triggers are not evaluated and never fire
  owned_by?: string | null;
  customized?: boolean;
}

// When a run concludes a finding with `verdict`, `agent` (in the same project) is started on the
// concluded entity, handed the finding; at most once per entity per `cooldown`.
export interface Handoff {
  verdict: string;
  agent: string;
  cooldown: string;            // a duration, e.g. 30m
}

// Docs and tickets (TR-403): what a spec session leaves in a project for the session that builds it
// The person behind a record (TR-619): who stood behind the call, and `by_label` says it in words
// ("Kiran (crew-builder-1)", or just "Kiran" when no station was named).
export interface PersonRef { id: string; name: string }
export type DocKind = "start" | "spec" | "plan" | "agents" | "memory" | "grants" | "note" | "working";
export type TicketStatus = "todo" | "in_progress" | "done" | "canceled";
export interface Ticket {
  id: string;
  owner: "tares" | "linear" | "github";
  identifier: string | null;
  url: string | null;
  title: string;
  status: TicketStatus;
  position: number;
  working_doc: string | null;
  working_doc_title: string | null;
  working_doc_body?: string | null;
  updated_at: string;
  // the factory's plan (TR-411, TR-412)
  number: number | null;
  label: string;                  // the Linear identifier, GH-<n>, else T<n>
  milestone: string | null;       // milestone id
  milestone_name: string | null;
  repo?: string | null;           // the GitHub repo (owner/name) it changes, when the project spans several
  depends_on: string[];           // labels of the tickets it waits for
  ready: boolean;                 // not finished, and everything it depends on is
  blocked_by: string[];           // labels of the unfinished tickets it depends on
  // the ledger (TR-427, TR-410): who holds it, where it stands, its PR as GitHub has it
  holder: string | null;
  stage: TicketStage | null;
  stage_reason: string | null;
  pr: TicketPR | null;
  pr_note: string | null;         // the stage and GitHub disagree
  history?: { at: string; field: "holder" | "stage"; value: string | null; reason: string | null; by: string | null; role: string | null; person?: PersonRef | null; by_label?: string | null }[];
  // the build, recorded (TR-433, TR-435)
  assumptions?: { label: string; question: string; choice: string; why: string; state: AssumptionState; words: string | null; made_by: string | null; person?: PersonRef | null; by_label?: string | null; tickets: string[] }[];
  checks?: TicketCheck[];
  // review and merge (TR-440..444)
  verdict?: VerdictNow | null;
  reviews?: Review[];
  challenger?: { rounds: number; open: ChallengeFinding[]; fixed: ChallengeFinding[]; waived: ChallengeFinding[]; verdict: string | null; sha: string | null; clean: boolean };
}
export interface VerdictNow { verdict: "pass" | "changes" | "block"; head: string; round: number; at: string; current: boolean; note: string | null }
export interface ChallengeFinding { priority?: string; title?: string; waived?: boolean; reason?: string }
export interface ReviewFinding { id: string; label: string; severity: "P1" | "P2" | "P3"; file: string | null; line: number | null; blocking: boolean; text: string; kind: string; state: "open" | "fixed"; fixed_in: string | null }
export interface Review { id: string; round: number; pr: string | null; head: string; verdict: "pass" | "changes" | "block"; verified: string; not_verified: string | null; by: string | null; person?: PersonRef | null; by_label?: string | null; at: string; findings: ReviewFinding[]; fixed: string[] }
export interface Decision {
  id: string; label: string | null; question: string; words: string; choice: string | null;
  standing: boolean; scope: "project" | "all"; matched: boolean; granted: boolean; tickets: string[]; at: string;
  via: "console" | "feed" | "message" | null;   // a page of the console or a message (feed on rows from before the feed was removed): a permission allowed there, or an answer given there, with no typed words to match
  person?: PersonRef | null;   // who decided
}
export type AssumptionState = "open" | "kept" | "overturned";
export interface TicketCheck {
  id: string; command: string; result: string; commit: string | null; broke_test: string | null;
  by: string | null; person?: PersonRef | null; by_label?: string | null; at: string;
  match?: "matched" | "not_found" | "differs" | null; recorded?: string | null;
}
export type TicketStage = "todo" | "doing" | "review" | "changes" | "merged" | "shipped" | "blocked";
export interface TicketPR {
  url?: string; repo?: string; number?: number; title?: string; branch?: string; head?: string;
  state?: "open" | "closed"; merged?: boolean; merge_commit?: string | null;
  ci?: "success" | "failure" | "pending" | "none"; verdict?: "pass" | "changes" | "pending" | null;
  verdict_text?: string | null; checked_at?: string | null; error?: string | null;
}
export interface MilestoneCheck { check: string; expect: string }
export interface Milestone {
  id: string;
  owner: "tares" | "linear" | "github";
  name: string;
  goal: string;
  checks: MilestoneCheck[];
  position: number;
  updated_at: string;
  tickets: number;
  done: number;
  ready: string[];
  finished: boolean;
}
// The tares-factory crew (M3): stations are roles with stable names, played by a chain of sessions
export type CrewState = "working" | "idle" | "waiting" | "quiet" | "ended";
export interface CrewStation {
  name: string;
  role: string | null;
  parent: string | null;
  project: string | null;
  project_name: string | null;
  projects: string[];
  state: CrewState;
  state_reason: string | null;
  state_at: string | null;
  quiet_minutes: number | null;
  /** null when the current session worked in a private project the viewer cannot see, in another
   *  person's crew (TR-1704); started_at, lines, claude_version, state_reason and the context's
   *  values are null with it */
  session: string | null;
  started_at: string | null;
  last_at: string | null;
  lines: number | null;
  earlier: { session: string; started_at: string | null; last_at: string | null; lines: number; state: "replaced" }[];
  children: string[];
  /** the Claude Code version the station runs, from its status line; null before it reported */
  claude_version: string | null;
  /** Set while the factory retries moving the station onto a newer Claude Code: the version, failed moves, unix time of the next try. */
  claude_retry: { target: string; fails: number; next_at: number } | null;
  /** the context window from the station's status line, null before it reported */
  context: StationContext | null;
  /** what all the station's sessions spent, null before any reply */
  usage: UsageTotals | null;
  /** the crew it belongs to (TR-628): a station name repeats across crews, the crew id does not */
  crew_id?: string | null;
  crew?: string | null;
  machine?: string;
  person?: PersonRef | null;
}
// What Claude Code sessions spend (tares/claude_usage.py)
export interface StationContext {
  used: number | null; size: number; pct: number | null;
  /** where the station compacts when started with --autocompact below its window, and how near it is */
  compact_at: number | null; compact_pct: number | null;
  model: string | null; cost_usd: number | null; at: string | null;
}
export interface UsageTotals {
  replies: number; input_tokens: number; output_tokens: number; cache_write_tokens: number;
  cache_read_tokens: number; cost_usd: number; unpriced_replies: number;
}
/** A /context reading of a session: the window by category */
export interface ContextReading {
  model: string | null; total_tokens: number | null; max_tokens: number | null; percentage: number | null;
  categories: { name: string; tokens: number; kind: "used" | "deferred" | "buffer" | "free" | string | null }[];
  mcp_servers: { name: string; tools: number; tokens: number }[];
  memory_files: { path: string | null; type: string | null; tokens: number }[];
  skills: { count: number; tokens: number; largest: { name: string | null; tokens: number }[] };
  agents: { count: number; tokens: number };
}
export interface PlanLimit {
  kind: string; label: string; percent: number; resets_at: string | null; severity: string | null;
  active: boolean; at: string | null; from: "usage" | "status line"; reset: boolean;
}
export interface ClaudePlan {
  limits: PlanLimit[];
  extra_usage: { is_enabled: boolean | null; monthly_limit: number | null; used_credits: number | null; utilization: number | null; currency: string | null } | null;
  at: string | null;
}
export interface ProjectUsage extends UsageTotals {
  project: string | null; kind: "project" | "crew" | "other"; name: string; sessions: number; share: number | null;
}
export interface ClaudeCodeUsage {
  plan: ClaudePlan | null;
  window: { since: string; until: string | null; projects: ProjectUsage[] };
  days: number; projects: ProjectUsage[]; now: string;
}
export interface CrewSettings {
  autonomy: "L3-review" | "L4-ship" | null;
  releaser: boolean | null;
  release_profile: string | null;
  prod_pattern: string | null;
  challenger: boolean | null;
  builders_max: number | null;
  kit: { frozen: { at: string; by: string; version: string } | null };
  idle_wake?: boolean | null;
  pace?: boolean | null;
  claude_update: boolean | null;
  docker_cleanup?: boolean | null;
  docker_keep?: string[] | null;
}
// The merged settings the caller sees (GET /api/crew/settings): theirs over the cell's default.
export interface CrewSettingsView extends CrewSettings { autonomy_levels?: Record<string, string> }
export type CrewSettingField = "autonomy" | "challenger" | "builders_max" | "releaser" | "release_profile" | "prod_pattern" | "pace" | "idle_wake";
// A question from a spec to the owner of a repo, and the answer once they give it (TR-636)
export interface SpecQuestion {
  id: string; to_person_id: string; to_person?: PersonRef | null; repo: string; text: string;
  asked_by: string | null; asked_at: string; answer: string | null; answered_by: string | null;
  answered_by_person?: PersonRef | null; answered_at: string | null; state: "open" | "answered";
  via?: "console" | "feed" | "message" | null;   // answered on a page of the console
}
export interface StationDetail extends CrewStation {
  tickets: { project: string; project_name: string; ticket: string; title: string; stage: string | null; repo: string | null; pr: string | null }[];
  recent: { at: string | null; type: string | null; text: string }[];
  attach: string;
  context_detail: ContextReading | null;
  context_at: string | null;
  context_asked_at: string | null;
}
export interface ProjectSession {
  session: string; repo: string | null; linked_at: string;
  started_at: string | null; last_at: string | null; lines: number;
  // what it is doing now, from the plugin; null before the plugin reported a state
  state: "working" | "idle" | "waiting" | "quiet" | "ended" | "replaced" | null;   // idle: a turn ended, the person is not needed; replaced: a fresh session picked up; quiet: silent past its window (TR-473)
  state_reason: string | null;
  state_at: string | null;
  last_said?: string;   // a waiting session: the last thing it wrote
  station?: string | null;   // the tares-factory station it played, for a crew session
}

// The cell's Anthropic spend meter (/api/usage/model): all-time totals plus a per-day tail,
// split by surface (agent runs vs Ask).
export interface ModelUsageBucket {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  cost_usd: number | null;
  uncosted_calls: number;
}

export interface ModelUsage {
  total: ModelUsageBucket;
  by_surface: Record<string, ModelUsageBucket>;
  by_provider?: Record<string, ModelUsageBucket>;   // provider id -> spend; rows before providers count as anthropic
  days: ({ day: string } & ModelUsageBucket)[];
  window_days: number;
}

// GET /api/usage — what this instance is using on disk.
// Three things the renderer must respect:
//  · `pct_used` is 0-100, NOT a 0-1 fraction — "warn at 80%" compares against 80.
//  · `max_bytes` is TARES_MAX_DB_SIZE when set, else the volume the database sits on
//    (`max_bytes_source` says which); null only when neither is known. At the pause mark ingest
//    is refused and polls stop (`ingest_paused`). Older comment, still true for the null case: (the Helm
//    chart does it for hosted cells), so a self-hosted install has no denominator at all: show
//    absolute bytes and fall back to `disk_free` for headroom. Null is unknown, never 0.
//  · `sources[].bytes` is always null — DuckDB keeps every source in one events table and cannot
//    attribute storage per source. Only the per-source event counts are real.
export interface Usage {
  db_bytes: number;
  wal_bytes: number;
  disk_total: number | null;
  disk_free: number | null;
  max_bytes: number | null;
  max_bytes_source?: "env" | "volume" | "";
  pct_used: number | null;
  ingest_paused?: boolean;
  events: number;
  sources: { name: string; events: number; bytes: number | null }[];
  agent_runs: number;
  dispatch_deliveries: number;
}

export interface TestResult {
  ok: boolean;
  events?: number;
  sample?: string[];
  error?: string;
  note?: string;
}

// A GitHub token stored once (Settings > GitHub) and referenced by name from sources and MCP
// servers. The token itself never comes back over the API.
export interface GithubCredential {
  name: string;
  kind: string;
  api_url: string;
  account: string;
  token_configured: boolean;
  created_at: string;
  updated_at: string;
  sources: string[];
  mcp_servers: string[];
  // a GitHub App (kind app, or app_broker on Tares Cloud)
  app_id?: string;
  slug?: string;
  html_url?: string;
  installations?: { id: number; account: string; account_type?: string; suspended?: boolean }[];
  broker?: boolean;
  repositories?: string[];     // app_broker: the repositories this workspace follows, [] = none yet
  source?: string;             // the webhook source this App feeds
  deliveries?: { received: number; stored: number; last_at: number | null; last_event: string | null;
                 unknown_installation: number; rejected_signature: number };
}

export interface GithubAppTest {
  ok: boolean;
  error?: string;
  login?: string;
  scopes?: string[];
  installations?: { id: number; account: string; repos: number }[];
}

// ── Projects: a template (code) instantiated with params; the instance owns ordinary objects ──
export interface RecipeParam {
  type?: string;              // string | number | bool | list | json | choice
  required?: boolean;
  secret?: boolean;           // a token: never shown back; blank on edit means keep
  default?: unknown;
  help?: string;
  label?: string;
  choices?: string[];
  item?: Record<string, RecipeParam>;   // for lists of objects
}
export interface RecipeSetupStep { title: string; text?: string; command?: string; check?: "anthropic_key" | "detect" }
export interface RecipeActionOption { value: string; label?: string; help?: string }
export interface RecipeAction {
  name: string; label: string; help?: string; intro?: string;
  docs?: { label: string; url: string };
  params?: Record<string, { label?: string; options?: (string | RecipeActionOption)[] }>;
}
export interface Template {
  key: string;
  title: string;
  description: string;
  params: Record<string, RecipeParam>;
  tags?: string[];
  guide?: { label: string; url: string } | null;
  setup?: RecipeSetupStep[];
  actions?: RecipeAction[];
  // The card's you/Tares bullets, in the mode the daemon is running in (a hosted demo stack
  // says different things than a local docker one). Absent on older daemons.
  facts?: { you: string[]; tares: string[] };
  // what a person would type to get this template, first person
  sentence?: string;
}
export type ProjectObjectKind = "source" | "trigger" | "agent" | "mcp_server";
export interface ProjectObject {
  kind: ProjectObjectKind;
  key: string;
  name: string;
  customized: boolean;
  missing: boolean;
  created_at: string;
}
export interface Project {
  id: string;
  name: string;
  status: "active" | "paused" | "error" | "draft" | "done";   // draft: being planned; done: the person said it is finished
  created_at: string;
  updated_at: string;
  objects: ProjectObject[];
  default?: boolean;   // the Default project: holds whatever was made outside a project; never deleted
  goal?: string | null; // what the project should achieve, one line (<= 200 chars); null = not set yet
  kind?: "software_factory" | null;   // its own page and tag; null = an ordinary project
  progress?: TicketProgress;           // a software factory project's tickets (GET /api/projects only)
  lead?: PersonRef | null;             // who leads it; null: nobody yet. Absent on an older daemon
  scope?: ProjectScope;                // team: everyone on the cell sees it; private: lead and members only
  members?: ProjectMember[];           // the lead first (reason "lead"), then the others
  hidden?: boolean;                    // a private project the caller is not on: name and lead, nothing else
  activity_at?: string;                // when it last moved: its own update or its newest ticket's (GET /api/projects only)
  ready_to_close?: boolean;            // a software factory project, still open, whose tickets are all merged or otherwise finished (GET /api/projects and the summary)
}
export type ProjectScope = "team" | "private";
export interface ProjectMember { id: string; name: string; reason: "lead" | "manual" | "ticket" | "self" }
// a software factory project's tickets as its progress ring shows them (tares/factory.py
// `progress`): merged only for a merge, closed for done without one, queued (waits on a ticket)
// apart from stuck (a station stopped); total is every group but canceled
export interface TicketProgress {
  merged: number; closed: number; doing: number; stuck: number; queued: number; todo: number;
  canceled: number; total: number;
}

// ── the goal-first project page (contract: goal-first-contract.md, "Backend") ──

export interface ProjectLogEntry { at: string; action: string; detail: string }
// summary = instance + log + whatever the template reports. The template part is free-form; the
// shapes below are what the shared code context template returns and the page renders when present.
export interface ProjectSummary extends Project {
  log: ProjectLogEntry[];
  summary_error?: string;
  // the generic core every template reports (computed in the Template base class)
  runs_total?: number; runs_ok?: number;
  trigger_last_fired?: string | null;
  triggers?: { name: string; last_fired?: string | null }[];
  runs?: { id?: string; started_at?: string; key?: string; repo?: string | null; status?: string; rounds?: number;
           max_rounds?: number | null; agent?: string; finding?: string | null;
           session?: string; proposals?: string[];
           decisions?: Record<string, "accepted" | "rejected"> }[];
  // template extras, declared as data: label/value tables and counters the shell renders as-is
  panels?: { title: string; rows: { label: string; value: unknown; url?: string; mono?: boolean }[] }[];
  cards?: { label: string; value: unknown }[];
  sessions?: ChallengerSession[];
  names?: Record<string, string>;
  [k: string]: unknown;
}
export interface ChallengeEvent {
  at: string; event_type: string; text: string; labels: Record<string, string>;
  findings?: { priority?: string; title?: string; waived?: boolean }[];
}
export interface ChallengerSession {
  session: string; repo?: string | null; branch?: string | null;
  started_at?: string | null; last_at?: string | null; ended: boolean; events: number;
  plan_verdict?: string | null; plan_findings?: string | number | null; plan_blocking?: string | number | null;
  waived: number; run_id?: string | null;
  commits: { sha?: string; verdict?: string; round?: string | number; findings?: string | number; blocking?: string | number }[];
  thread: ChallengeEvent[];
}
// A model provider the cell holds (Settings, Model providers). Credentials are never returned:
// `configured` says one resolves, `source` where from (console, env:VAR, or empty).
export interface ModelProvider {
  id: string;                 // "anthropic", "openai", or the slug of an OpenAI-compatible entry
  kind: "anthropic" | "openai" | "openai_compatible";
  name: string;
  base_url: string;
  configured: boolean;
  source: string;
  stored: boolean;            // a credential is stored on the cell (vs the environment only)
  default: boolean;
  models: string[];           // what the picker offers for this provider
  models_error?: string;      // why the last discovery failed (the last list is kept)
  models_problem?: string;    // the one-line reading of models_error, for the row
  models_at?: string;         // when the list was last read from the endpoint
  discovers?: boolean;        // the endpoint lists its own models (everything but Anthropic)
  base_source?: string;       // anthropic only: where the gateway URL came from
  gateway_stored?: boolean;
  gateway_token_stored?: boolean;
}
export interface ModelProviders {
  providers: ModelProvider[];
  default: string | null;
  kinds: { id: ModelProvider["kind"]; label: string }[];
}

// ── goal-first project setup (contract: setup-flow-contract.md, "The Plan object" and "Backend") ──

export interface PlanWatch {
  key: string;
  existing: boolean;            // an existing source reused (config omitted) or a new one
  name: string;
  connector: string;
  config?: Record<string, unknown>;
  poll?: string;                // a polled source's interval
  sentence: string;
  needs: "send" | "credential" | "none";
  sample: Record<string, unknown> | null;
}
export interface PlanKnob { id: string; label: string; value: number; min: number; max: number }
export interface PlanCondition {
  aggregate?: "count" | "avg" | "sum" | "max" | "min" | "any";
  field?: string | null;
  predicate?: string;           // "> 5"
  window?: string;              // "5m"
  every?: string;               // a schedule instead: "60m"
}
export interface PlanWake {
  key: string; name: string; sources: string[]; filters: TriggerFilter[];
  key_field: string | null; condition: PlanCondition;
  description?: string | null;  // what wakes the project in plain words, follows "When"
  cooldown: string | null; window: string | null;
  sentence: string;
  cooldown_sentence?: string | null;
  knobs: PlanKnob[];
}
export interface PlanAgent {
  key: string; name: string;
  existing?: boolean;           // an agent already on Tares: used as it is, this project only wires it
  trigger: string | null; on_trigger: boolean;
  prompt: string; model: string | null;
  provider?: string | null;     // a provider id from Settings; empty = the cell default
  handoffs: { verdict: string; agent: string; cooldown?: string | null }[];
  mcp_servers: string[];
  slack?: boolean;              // Tares posts its findings to Slack, to slack_channel (a channel id)
  slack_channel?: string;
  sentence: string;
  optional: boolean;
  enabled: boolean;
}
export interface PlanOwnAgent { name: string; wake: "webhook" | "poll"; sentence: string }
export interface PlanTool {
  key: string; name: string; url: string; why: string; can_act: boolean; enabled: boolean;
  existing?: boolean;           // an MCP server already on the cell, used as it is
}
export interface PlanSkill {
  key: string; name: string; description: string; body: string; enabled: boolean;
  existing?: boolean;           // a skill already on Tares: used as it is (shared), not copied
}
export interface Plan {
  goal: string;
  name: string;
  summary: string;
  watches: PlanWatch[];
  wakes: PlanWake[];
  who: "tares" | "own";
  agents: PlanAgent[];
  own_agent: PlanOwnAgent | null;
  tools: PlanTool[];
  skills: PlanSkill[];
  notes: string[];
}
export type SetupStep = "connect" | "try" | "done";

/** The crew's standup (/api/crew/standup): what moved in each handed-over project over a range,
 *  read from the ledger; `markdown` is the same report ready to copy or post. */
export interface StandupTicket { ref: string; title: string; holder: string | null; pr: string | null; url: string | null }
export interface StandupProject {
  id: string; name: string; moved: boolean; finished: boolean;
  progress: { done: number; total: number; milestone: { name: string; done: number; total: number } | null };
  done: StandupTicket[]; started: StandupTicket[]; doing: StandupTicket[];
  review: (StandupTicket & { stage: string })[];
  blocked: (StandupTicket & { reason: string; new: boolean })[];
  needs_you: { label: string; question: string; blocking: boolean; permission: boolean; asked_at: string }[];
}
export interface Standup {
  since: string; until: string; markdown: string;
  projects: StandupProject[];
  quiet: { id: string; name: string; progress: StandupProject["progress"]; blocked: number; needs_you: number }[];
  summary?: { overall: string | null; projects: Record<string, string> } | null;
  summary_error?: string | null;
}

export type Edition = "factory" | "sre" | "platform";

// Area owners (TR-627): the person who owns each GitHub repo.
export interface Area { repo: string; person: PersonRef; note: string | null }

// Grants by kind (TR-630): what a person allows, and who still has to say yes.
export type GrantKind = "team" | "project" | "personal";
export interface Grant {
  id: string;
  kind: GrantKind;
  project: string | null;
  words: string;
  state: "active" | "waiting" | "revoked";
  given_at: string | null;
  given_by: PersonRef | null;
  holder: PersonRef | null;
  areas: string[];
  consents: (PersonRef & { at: string; via?: "console"; declined?: boolean; reason?: string })[];
  waiting_for: { id: string; name: string | null }[];   // id "admin": an admin has to
  in_effect: boolean;
}
