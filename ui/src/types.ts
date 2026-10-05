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
  mode: "poll" | "push";
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

export interface SourceFieldValue { value: string; events: number; }
export interface SourceField {
  name: string;
  help: string;
  primary_default: boolean;
  coverage: number;
  distinct: number;
  is_label?: boolean;
  is_key?: boolean;
  values: SourceFieldValue[];
}
export interface SourceLabelProfile {
  name: string;
  is_key: boolean;
  coverage: number;
  distinct: number;
  values: SourceFieldValue[];
}
export interface SourceFieldsProfile {
  sampled: number;
  fields: SourceField[];
  labels?: SourceLabelProfile[];
}

// One event in a correlated read: its time offset, source, rendered text, and the labels it
// carries (endpoint, status, …) — so the timeline shows the dimensions you filtered/sliced by.
export interface TimelineEventRow {
  offset: string;
  source: string;
  text: string;
  labels: Record<string, string>;
}

export interface CatalogDescribe {
  handle: string;
  kind: "source" | "trigger";
  entry: Record<string, unknown>;
  schema?: { event_types?: string[]; fields?: Record<string, string>; sampled_events?: number } & Record<string, unknown>;
  labels?: Record<string, { value: string; events: number; last_ingest?: string }[]>;
  primary_label?: string;
  freshness?: { last_event_time?: string; lag_seconds?: number; events_total?: number; status?: string };
  lineage?: { from: string; to: string; transform: string }[];
  sample?: { key: string; event_type: string; text: string; ingest_time?: string }[];
  subscribers?: number;
}

export interface DiscoverMetric {
  name: string;
  type: string;
  help: string;
  series: number;
  sample: string | null;
  labels: string[];
  ingest: boolean;
  reason: string;
}

export interface ProposedSource {
  connector: string;
  name: string;
  config: Record<string, unknown>;
  summary: string;
  preselect: boolean;
  from: string;
}

export interface EnvScan {
  provider: string;
  summary: { containers: number; proposed: number };
  containers: Array<{ name: string; image: string; ports: string; project: string; service: string }>;
  proposed_sources: ProposedSource[];
  skipped: Array<{ service: string; image: string; reason: string }>;
}

export interface DiscoverProposal {
  connector: string;
  families?: string[];   // prometheus: the metric families (name prefixes) this proposal ingests
  summary: { total_metrics: number; relevant: number; hidden: number; capped?: boolean; families?: number };
  suggested_key: { name: string; cardinality: number; values_preview: string[]; alternatives: string[] };
  proposed_labels: string[];
  metrics: DiscoverMetric[];
  derived_suggestions: { id: string; label: string; promql: string; event_type: string; field: string; reason: string }[];
  proposed_config: { url: string; default_key: string; queries: unknown[]; labels: { name: string; field: string }[] };
}

// A connected agent: subscriptions grouped by endpoint, named deterministically server-side.
export interface AgentInfo {
  name: string;
  endpoint: string;   // masked — the last path segment may carry a secret
  // a Tares agent (in-process), an external webhook, or a Slack channel (slack://channel/<id>)
  kind: "tares" | "connected" | "slack";
  subscriptions: { subscription_id: string; trigger: string; created_at: string | null }[];
  triggers: string[];
  created_by: string[];
  first_seen: string | null;
  delivered_ok_24h: number;     // deliveries in the last 24h — what the roster columns show
  delivered_fail_24h: number;
  delivered_ok_total: number;   // all-time, shown as secondary text so the number isn't lost
  delivered_fail_total: number;
  pending?: number;             // in-flight Tares-agent runs
  last_woken: string | null;
  unhealthy?: boolean;          // the most recent delivery to this endpoint failed
  last_error?: string | null;   // why, when unhealthy
  recent: { at: string | null; ok: boolean | null; trigger: string | null; key: string | null; error?: string | null; dispatch_id?: string }[];
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
}

// An agent that joined a project with a webhook subscription (TR-336). The URL comes masked: it
// can carry the receiver's secret.
export interface ExternalAgent {
  subscription_id: string;
  name: string;
  url: string;
  key_id: string | null;
  key_name: string | null;
  created_at: string | null;
  last_delivery: { at: string | null; ok: boolean | null; error: string | null; dispatch_id: string } | null;
}

// Discover response for table-shaped connectors (postgres): the columns found plus a proposed
// config to review and apply. (Prometheus uses the richer DiscoverProposal above.)
export interface ColumnsProposal {
  connector: string;
  summary: string;
  columns: { name: string; type: string }[];
  proposed_config: Record<string, unknown>;
}

// One filter row on a trigger: which events it counts. Same ops the daemon accepts.
export interface TriggerFilter {
  field: string;
  op: "eq" | "neq" | "contains" | "gt" | "lt" | "gte" | "lte" | "in";
  value: string | number | string[];   // a list for `in`
}

export interface Entity {
  value: string;
  events: number;
  last_ingest: string | null;
}

export interface LabelFacet {
  label: string;
  primary?: boolean;
  sources: string[];
  high_cardinality?: boolean;   // exceeded the entity cardinality cap — served by a live scan
  values: Entity[];
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

// A Tares agent is a prompt attached to a trigger: when the trigger fires, the agent takes a
// first look and writes a finding onto the entity's timeline. It's a real agent, configured inside
// Tares rather than connected over a webhook. The prompt is the only field a user edits; enabled
// means it's subscribed to its trigger, exactly like an external agent.
export interface BuiltinAgent {
  name: string;
  project?: string;            // the project it belongs to (its trigger is in the same one)
  trigger: string;
  prompt: string;
  enabled: boolean;
  slack_configured: boolean;   // the webhook URL itself is never sent to the client
  model: string;               // "" = the provider's default model
  provider: string;            // provider id from Settings; "" = the cell default
  effective_provider?: string | null;   // what the next run resolves to (the default when the named one is gone)
  slack_channel: string;       // workspace-bot channel id, "" = none
  webhook_url: string;         // write-back target, "" = none
  webhook_key_label?: string;  // the write-back reports this label's value as `key`; "" = the entity
  webhook_token_configured: boolean;   // the token itself is never sent to the client
  mcp_servers: string[];       // registry names this agent may use
  max_rounds: number | null;   // model rounds per run; null = the default for its shape
  budget_usd?: number | null;  // lifetime spend cap in USD; null = no budget
  handoffs?: Handoff[];        // who takes over when a run concludes with a verdict (TR-334)
  concludes?: boolean;         // every run ends with the conclude tool, whatever the prompt says
  verdicts?: Verdict[];        // the only verdicts it may give; empty: any word, or none
  github?: string;             // the GitHub credential it acts with (check runs); "" = none
  offers_conclude?: boolean;   // it gets the conclude tool (set to, or its prompt names it)
  effective_max_rounds: number;   // the cap its next run will be held to
  updated_at?: string;
  last_run?: AgentRun | null;
  owned_by?: string | null;
  customized?: boolean;
  stats?: AgentStats;
  projects?: string[];         // every project that uses it (P-TR-216: parts are shared)
}

// When a run concludes a finding with `verdict`, `agent` (in the same project) is started on the
// concluded entity, handed the finding; at most once per entity per `cooldown`.
export interface Handoff {
  verdict: string;
  agent: string;
  cooldown: string;            // a duration, e.g. 30m
}

// Lifetime aggregates over an agent's runs. cost_usd is a floor: runs from before usage tracking
// and runs on unpriced models carry no cost (uncosted_runs counts those).
export interface AgentStats {
  runs: number;
  ok: number;
  finished: number;            // runs that concluded or tried to (excludes running and capped)
  avg_duration_ms: number | null;
  cost_usd: number | null;
  input_tokens: number;
  output_tokens: number;
  uncosted_runs: number;
}

export interface AgentRun {
  id: string;
  agent: string;
  trigger: string;
  dispatch_id: string;
  key: string;
  status: "running" | "ok" | "empty" | "failed" | "capped" | "exhausted";
  rounds: number;
  max_rounds?: number | null;  // the cap this run was held to
  tool_calls: number;
  external_tools?: string[];   // prefixed server__tool names this run called
  started_at: string;
  duration_ms: number | null;
  finding: string | null;
  error: string | null;
  // Model usage; null on runs from before cost tracking (unknown, not zero).
  model?: string | null;
  provider?: string;           // the provider the run resolved to
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  cache_read_input_tokens?: number | null;
  cost_usd?: number | null;
  // the write-back's outcome: "ok", "http 4xx", "failed"; null when the agent has no webhook
  delivery?: string | null;
  delivery_error?: string | null;
  // how the run ended on purpose (TR-318): "no_op" left no finding; null on older runs
  outcome?: "finding" | "no_op" | null;
  verdict?: string | null;
  headline?: string | null;    // the one-line conclusion, when the agent gave one
  // what the run produced (TR-220), read off its tool calls and deliveries; [] when nothing
  results?: RunResult[];
  // the project skills the run loaded (TR-332), in order; [] when none
  skills?: string[];
}

// A project's skill (TR-332): instructions its agents load by name when a task matches.
export interface SkillSummary {
  name: string;
  description: string;
  updated_at: string;
  size: number;          // bytes of the body
  loaded_by: string[];   // agents of the project that loaded it in the last 7 days
}
export interface Skill {
  name: string;
  description: string;
  body: string;
  updated_at: string;
}

export interface RunResult {
  kind: "pr" | "commit" | "check" | "slack" | "email" | "webhook" | "custom";
  label: string;
  url?: string;
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

export interface AgentPreset {
  id: string;
  label: string;
  prompt: string;
  concludes?: boolean;          // the preset ends every run with conclude
  verdicts?: Verdict[];         // and gives these verdicts
}

/** A verdict an agent may give when it concludes with a finding, and when to give it. */
export interface Verdict { verdict: string; when?: string }

export interface QueryLogEntry {
  id: string;   // r_ = a read, s_ = a stats call
  key: string;
  window: string;
  rows_returned: number;
  client: string;
  queried_at: string;
}

export interface DispatchLogEntry {
  dispatch_id: string;
  trigger: string;
  key: string;
  kind: string;
  fired_at: string;
  subscribers: number;
  delivered: number;
  payload: string;
  error?: string | null;   // most recent failed delivery's reason, when delivered < subscribers
}

// One delivery attempt to a specific subscriber, for the dispatch detail page.
export interface DispatchDelivery {
  agent: string;
  kind: "tares" | "slack" | "webhook";
  endpoint: string;   // masked
  ok: boolean;
  error?: string | null;
  delivered_at: string | null;
}

// A single firing, deep — fetched by id so a linked dispatch page never dead-ends.
export interface DispatchDetail extends DispatchLogEntry {
  deliveries: DispatchDelivery[];
}

export interface Subscription {
  subscription_id: string;
  trigger: string;
  url: string;
  created_at: string;
}

export interface SourceEvent {
  source: string;
  key: string;
  event_type: string;
  text: string;
  event_time: string;
  ingest_time: string;
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
export interface McpServer {
  name: string; url: string; auth_header: string; auth_value_configured: boolean;
  auth_credential: string; headers: Record<string, string>; updated_at: string;
  owned_by?: string | null; customized?: boolean;
  project?: string;
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
  template: string;
  template_title: string;
  name: string;
  params: Record<string, unknown>;
  status: "active" | "paused" | "error" | "draft";   // draft: being planned, nothing exists yet
  created_at: string;
  updated_at: string;
  last_error: string | null;
  objects: ProjectObject[];
  default?: boolean;   // the Default project: holds whatever was made outside a project; never deleted
  goal?: string | null; // what the project should achieve, one line (<= 200 chars); null = not set yet
}

// ── the goal-first project page (contract: goal-first-contract.md, "Backend") ──

// GET /api/projects/{uid}/outline: the project's setup in plain sentences, built from its config
export type OutlineSourceState = "receiving" | "silent" | "error" | "paused" | "waiting";
export interface OutlineWatch {
  source: string; title?: string; connector: string; description: string; state: OutlineSourceState;
  last_event_at: string | null; detail: string | null;
}
export interface OutlineWake { trigger: string; sentence: string; cooldown_sentence: string | null; paused: boolean }
export interface OutlineHandoff { verdict: string; agent: string; cooldown: string | number | null }
export interface OutlineAgent {
  name: string; sentence: string; enabled: boolean; runs_on: "trigger" | "handoff"; handoffs: OutlineHandoff[];
}
export interface OutlineSkill { name: string; description: string; loaded_by: string[] }
/** An agent outside Tares working for the project: a project key, or a webhook subscribed without one. */
export interface OutlineOutside { name: string; sentence: string; joined: boolean }
export interface ProjectOutline {
  sentence: string;
  watches: OutlineWatch[];
  wakes: OutlineWake[];
  agents: OutlineAgent[];
  outside?: OutlineOutside[];
  skills: OutlineSkill[];
}

// GET /api/projects/{uid}/results: one entry per concluded chain, newest first
export interface ProjectResult {
  id: string;                 // run id of the concluding run
  thread: string;             // the timeline thread it belongs to
  at: string;
  entity: string | null;
  kind: "action" | "no_action";
  headline: string | null;
  summary: string | null;     // first paragraph of the note, plain text
  next_step: string | null;
  verdict: string | null;
  chain: string[];            // the agents in order, e.g. triage then root cause
  cost_usd: number | null;    // the whole chain
  duration_ms: number | null; // the whole chain
  handled: { at: string; by: string } | null;
  external: boolean;
  practice?: boolean;         // from a practice run during setup; left out of today's totals
}
export interface ProjectResults {
  results: ProjectResult[];
  next_before: string | null;
  today: { looked_at: number; found: number; spent_usd: number };
}
// GET /api/projects/{uid}/results/{run_id}
export interface ProjectResultDetail extends ProjectResult {
  note: string | null;                  // the full markdown note
  steps: { at: string; text: string }[]; // how Tares got there, oldest first
}

// GET /api/projects/{uid}/health
export interface ProjectHealthIssue {
  severity: "error" | "warning";
  message: string;
  fix: string | null;
  view: string | null;        // a project view to open, same form as ?view= (e.g. "source:checkout-errors")
}
export interface ProjectHealth {
  state: "working" | "attention" | "paused" | "setting_up";
  message: string;
  issues: ProjectHealthIssue[];
}
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
export interface ProjectUpdateReport { created: string[]; updated: string[]; kept: string[]; deleted: string[]; added?: string[]; released?: string[] }

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
// POST /api/setup/check -> problems: what stops the plan from applying, per item. where:
// "watches.<key>", "wakes.<key>", "agents.<key>", "tools.<key>", "skills.<key>", "own_agent",
// a section ("watches", "wakes", "agents") or "plan"
/** `who`: "tares" when the plan itself is wrong (the planner fixes it), "you" when only the
 *  person knows the answer (which Slack channel). */
export interface SetupProblem { where: string; message: string; who?: "tares" | "you" }
export type SetupStep = "connect" | "try" | "done";

// POST /api/setup/apply -> connect: what only the user can do next
export interface SetupConnect {
  sources: { name: string; needs: PlanWatch["needs"]; ingest_url: string | null;
             sample: Record<string, unknown> | null; credential_hint: string | null }[];
  tools: { name: string; url: string; needs_token: boolean }[];
  // key: the secret on the answer to apply only; null when the setup is read again (shown once)
  own_agent: { key: string | null; mcp_url: string; claude_command: string; subscribe_hint: string } | null;
}

// GET /api/projects/{uid}/setup -> checks: live state for the Connect step
export interface SetupChecks {
  sources: { name: string; state: "waiting" | "receiving" | "error"; detail: string | null;
             last_event_at: string | null; fields_seen: string[] }[];
  tools: { name: string; state: "untested" | "ok" | "error"; detail: string | null }[];
  own_agent: { state: "waiting" | "joined"; detail: string | null } | null;
}
/** A plan being written for a draft, step by step as it happens. */
export interface SetupPlanning {
  state: "running" | "failed";
  steps: { text: string; state: "running" | "done" | "stopped"; at: string }[];
  error?: string;
  no_provider?: boolean;        // failed for want of a model provider
}
export interface ProjectSetup {
  step: SetupStep | "plan";     // plan: a draft, still being planned or edited
  draft?: boolean;
  goal?: string | null;
  name?: string;                // a draft: the name the person gave it ("" when Tares names it)
  description?: string;         // a draft: what the person said about it, for the planner
  who?: "tares" | "own" | null;
  plan: Plan | null;            // null on a draft until its first plan is written
  planning?: SetupPlanning | null;
  practice_run: string | null;
  checks: SetupChecks | null;
  connect: SetupConnect | null; // what Connect shows; the own agent's key is never in it
}

// GET /api/resources: every part on the cell, each with the projects that use it (TR-351)
export interface ResourceRow {
  name: string;
  title?: string;               // what a person calls it, when plainer than the name (a GitHub source: its repository)
  what: string;                 // what it is, in plain words
  state: string;                // receiving / silent / error / paused / waiting; active / paused; on / off; set / no credentials; used / never used
  detail?: string | null;
  kind?: string; kind_label?: string;     // sources: the connector
  last_event_at?: string | null;          // sources
  last_fired_at?: string | null;          // wake-ups
  last_run_at?: string | null; last_run_status?: string | null;   // agents
  url?: string | null;                    // tools
  prefix?: string | null; last_used_at?: string | null;           // keys
  used_by: { id: string; name: string }[];
}
export type ResourceKind = "sources" | "triggers" | "agents" | "tools" | "skills" | "keys";
export type Resources = Record<ResourceKind, ResourceRow[]>;
