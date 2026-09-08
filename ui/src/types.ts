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
}

export interface ConnectorField {
  name: string;
  type: "string" | "number" | "json" | "list" | "bool";
  required: boolean;
  help: string;
  secret?: boolean;          // render as a password input (tokens, DSNs)
  discover_input?: boolean;  // Discover needs this field — shown above the Discover panel
  default?: unknown;         // what the connector uses when the field is empty; prefilled on a fresh source
  item?: ConnectorField[];   // for type "list": the sub-fields of each row
}

export interface ConnectorSpec {
  label: string;
  mode: "poll" | "push";
  discover?: boolean;
  poll?: string;   // connector-specific default poll interval (e.g. github: "2m")
  internal?: boolean;   // provisioned by Tares itself (agent findings) — not offered in the UI
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

export interface CatalogList {
  sources: { name: string; type: string }[];
  views: { name: string; key_field: string; sources: string[]; created_by: string }[];
  triggers: { name: string; view: string }[];
}
export interface CatalogDescribe {
  handle: string;
  kind: "source" | "view" | "trigger";
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
}

// Discover response for table-shaped connectors (postgres): the columns found plus a proposed
// config to review and apply. (Prometheus uses the richer DiscoverProposal above.)
export interface ColumnsProposal {
  connector: string;
  summary: string;
  columns: { name: string; type: string }[];
  proposed_config: Record<string, unknown>;
}

export interface ViewFilter {
  field: string;
  op: "eq" | "neq" | "contains" | "gt" | "lt" | "gte" | "lte";
  value: string | number;
}

export interface ViewUsage {
  queries: number;
  last_used_at: string | null;
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

export interface View {
  name: string;
  key_field: string;
  sources: string[];
  filters?: ViewFilter[];
  created_by?: string;
  usage?: ViewUsage | null;
  owned_by?: string | null;
  customized?: boolean;
}

export interface TriggerCondition {
  aggregate: string;
  predicate: string;
  window: string;
  field?: string | null;
  group_by?: string[];
}

export interface Trigger {
  name: string;
  view: string;
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
  trigger: string;
  prompt: string;
  enabled: boolean;
  slack_configured: boolean;   // the webhook URL itself is never sent to the client
  model: string;               // "" = the instance default
  slack_channel: string;       // workspace-bot channel id, "" = none
  webhook_url: string;         // write-back target, "" = none
  webhook_token_configured: boolean;   // the token itself is never sent to the client
  mcp_servers: string[];       // registry names this agent may use
  max_rounds: number | null;   // model rounds per run; null = the default for its shape
  budget_usd?: number | null;  // lifetime spend cap in USD; null = no budget
  effective_max_rounds: number;   // the cap its next run will be held to
  updated_at?: string;
  last_run?: AgentRun | null;
  owned_by?: string | null;
  customized?: boolean;
  stats?: AgentStats;
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
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  cache_read_input_tokens?: number | null;
  cost_usd?: number | null;
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
  days: ({ day: string } & ModelUsageBucket)[];
  window_days: number;
}

export interface AgentPreset {
  id: string;
  label: string;
  prompt: string;
}

export interface QueryLogEntry {
  id: string;
  view: string;
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
//  · `pct_used` and `max_bytes` are null unless the operator set TARES_MAX_DB_SIZE (the Helm
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
  pct_used: number | null;
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
  // what a person would type to get this template, first person; the landing screen's starters
  sentence?: string;
}
export interface McpServer {
  name: string; url: string; auth_header: string; auth_value_configured: boolean;
  auth_credential: string; headers: Record<string, string>; updated_at: string;
  owned_by?: string | null; customized?: boolean;
}
export type ProjectObjectKind = "source" | "view" | "trigger" | "agent" | "mcp_server";
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
  status: "active" | "paused" | "error";
  created_at: string;
  updated_at: string;
  last_error: string | null;
  objects: ProjectObject[];
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
