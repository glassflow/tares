// Sample data for the AI SRE Settings page, shown at /settings?sample=<name> when the browser has the
// sample flag (see ../../sample.ts). Every body is shaped exactly like GET /api/sre/settings. It is
// invented: edge-proxy, service-a, docs-site, Alex, Kim, Lee.
//
// busy     a cell in use, an admin
// new      nothing set up yet: no watcher, no investigator, Slack not connected, one key
// ro       the same cell for a key that may only read: no revoke data, nothing editable
// long     a long name and a long rule
// many     20 tools and 30 keys
// saved, partial and switch show the busy cell with the page in that state; load and err never answer.

import type {
  Field, FieldGroup, KeyRow, Option, SreSettings, ToolRow,
} from "../Settings.api";

export const SAMPLE_NAMES = ["busy", "new", "ro", "saved", "partial", "switch", "load", "err", "long", "many"] as const;
export type SampleName = (typeof SAMPLE_NAMES)[number];

export const isSettingsSample = (n: string | null): n is SampleName => !!n && (SAMPLE_NAMES as readonly string[]).includes(n);

const WARNING =
  "Switching changes the screens, not your data. What only the other edition shows stays reachable under Settings, Advanced.";

const f = (key: string, label: string, value: Field["value"], value_words: string | null, options: Option[] | null,
          help: string, extra: Partial<Field> = {}): Field => ({ key, label, value, value_words, options, help, ...extra });

const EVERY: Option[] = [
  { value: "1m", label: "Every minute" }, { value: "5m", label: "Every 5 minutes" },
  { value: "10m", label: "Every 10 minutes" }, { value: "15m", label: "Every 15 minutes" },
  { value: "30m", label: "Every 30 minutes" }, { value: "1h", label: "Every hour" },
  { value: "6h", label: "Every 6 hours" }, { value: "1d", label: "Every day" },
];
const COOLDOWN: Option[] = [
  { value: "5m", label: "5 minutes" }, { value: "15m", label: "15 minutes" }, { value: "30m", label: "30 minutes" },
  { value: "1h", label: "1 hour" }, { value: "2h", label: "2 hours" }, { value: "6h", label: "6 hours" },
  { value: "1d", label: "1 day" },
];
const PERIOD: Option[] = [
  { value: "month", label: "Resets on the 1st of each month" },
  { value: "lifetime", label: "Never resets" },
];
const MODELS: Option[] = [
  { value: "", label: "The usual model (Sonnet)" },
  { value: "claude-opus-5-5", label: "Claude Opus 5.5" },
  { value: "claude-sonnet-5-5", label: "Claude Sonnet 5.5" },
];
const CHANNELS: Option[] = [
  { value: "", label: "No channel" }, { value: "C01INCIDENTS", label: "#incidents" },
  { value: "C02OPSALERTS", label: "#ops-alerts" }, { value: "C03ONCALL", label: "#oncall" },
];

const MODEL_HELP = "The AI model it thinks with.";
const BUDGET_HELP = "It stops when it has spent this much. Leave empty for no limit.";
const PERIOD_HELP = "A monthly budget starts again on the 1st at 00:00 UTC.";

const model = (value: string, words: string): Field =>
  f("model", "Model", value, words, MODELS, MODEL_HELP, { default: "claude-sonnet-4-6" });
const budget = (usd: number | null, words: string, spent: string): Field =>
  f("budget_usd", "Budget", usd, words, null, BUDGET_HELP, { spent_words: spent });
const period = (month: boolean, resets: string | null): Field =>
  f("budget_period", "When the budget resets", month ? "month" : "lifetime",
    month ? PERIOD[0].label : PERIOD[1].label, PERIOD, PERIOD_HELP,
    { resets_on: resets ? "2026-12-01" : null, resets_words: resets });

const RULE_TEXT =
  "Compare each service with its own last 7 days. Raise it when errors or slowness are clearly worse than usual and have lasted more than one check. Ignore the nightly 02:00 log rotation.";

const watching = (o: { name?: string; rule?: string[]; text?: string; spent?: string; hit?: string } = {}): FieldGroup => {
  const rule = o.rule ?? ["service", "status", "version"];
  return {
    title: "Watching", editable: true, empty_words: null, agent: "watcher",
    fields: [
      f("system_name", "What we call what is watched", o.name ?? "edge-proxy and service-a",
        (o.name ?? "edge-proxy and service-a"), null,
        "Used in messages and on the home page. Leave empty to use the name of this instance."),
      f("every", "How often it checks", "10m", "Every 10 minutes", EVERY,
        "It looks at your services this often. The shortest is every minute."),
      model("claude-sonnet-5-5", "Claude Sonnet 5.5"),
      budget(20, "$20 a month", o.spent ?? "Spent $6.40 of $20 this month"),
      period(true, "Starts again on 1 Dec"),
      f("rule", "What it compares", rule, "Compares by " + rule.join(", "), null,
        "The labels it groups events by before it decides what is unusual. Up to 5."),
      f("rule_text", "The rule, as written", null, null, null,
        "To change the wording, open the watcher's setup under Advanced.",
        { read_only: true, text: o.text ?? RULE_TEXT,
          link: { label: "Open the watcher's setup", value: "/projects/prj-project-x-watch" } }),
      f("hit_rate", "How often a raised case was worth it", null, o.hit ?? "7 of 9 raised were worth it, last 30 days",
        null, "From the Worth raising? answers people give in Slack.", { read_only: true }),
    ],
  };
};

const investigating = (): FieldGroup => ({
  title: "Investigating", editable: true, empty_words: null, agent: "investigator",
  fields: [
    model("claude-opus-5-5", "Claude Opus 5.5"),
    budget(50, "$50 a month", "Spent $12.10 of $50 this month"),
    period(true, "Starts again on 1 Dec"),
    f("cooldown", "Wait before looking at the same service again", "30m", "30 minutes", COOLDOWN,
      "After an investigation it leaves that service alone for this long."),
  ],
});

const notes = (connected: boolean): SreSettings["notes"] => ({
  title: "Where notes go", editable: true, empty_words: null,
  slack: {
    connected, problem: null, words: connected ? "Slack is connected." : "Slack is not connected.",
    link: { label: "Slack settings", value: "/settings/advanced?tab=slack" },
  },
  fields: connected
    ? [
      f("slack_channel", "Slack channel for notes", "C01INCIDENTS", "#incidents", CHANNELS,
        "The channel the watcher and the investigator write to."),
      f("watch_channel", "Channel for services nobody watches", "ops-alerts", "#ops-alerts", null,
        "Where a message goes when a service has no watcher. A channel name, such as #oncall, or its Slack ID."),
    ]
    : [
      f("slack_channel", "Slack channel for notes", "",
        "Slack is not connected, so notes are not sent. Connect Slack under Tools.", null,
        "Connect Slack first. Until then notes stay in the console."),
      f("watch_channel", "Channel for services nobody watches", "", "No channel chosen", null,
        "Where a message goes when a service has no watcher. A channel name, such as #oncall, or its Slack ID."),
    ],
});

const row = (name: string, kind: string, ok: boolean, state: string, words: string, problem: string | null = null): ToolRow => ({
  name, kind, ok, state, state_words: words, problem_words: problem, link: { label: "Open", value: `/sources/${name}` },
});

const slackRow = (connected: boolean): ToolRow => ({
  name: "Slack", kind: "Slack", ok: connected, state: connected ? "connected" : "off",
  state_words: connected ? "Connected" : "Not connected",
  problem_words: connected ? null : "Not connected, so notes are not sent.",
  link: { label: "Slack settings", value: "/settings/advanced?tab=slack" },
});

const ADD = [
  { label: "Metrics", value: "/sources/new?connector=prometheus" },
  { label: "Logs", value: "/sources/new?connector=loki" },
  { label: "Alerts", value: "/sources/new?connector=alertmanager" },
  { label: "Traces and logs", value: "/sources/new?connector=otlp" },
  { label: "GitHub", value: "/settings/advanced?tab=github" },
  { label: "Slack", value: "/settings/advanced?tab=slack" },
];

const ADVANCED: SreSettings["advanced"] = {
  title: "Advanced", editable: false,
  links: [
    { label: "All settings", value: "/settings/advanced", help: "Model providers, limits, GitHub, Linear and the rest of today's Settings page." },
    { label: "Connections", value: "/sources", help: "The tools and services this workspace receives data from." },
    { label: "Explore your data", value: "/explore", help: "Look through the events and details Tares holds." },
    { label: "All resources", value: "/resources", help: "Every connection, rule and agent in this workspace, in one list." },
    { label: "Connect", value: "/connect", help: "Give an outside agent access to your data over MCP." },
    { label: "Reads", value: "/reads", help: "See what outside agents asked for, and when." },
  ],
  note: "Agents and rules live in each project, under Advanced setup.",
};

const SCOPES: Option[] = [
  { value: "read", label: "read" },
  { value: "ingest", label: "send data" }, // words-ok
  { value: "admin", label: "change settings" },
];

const key = (id: string, name: string, scopes: string, used: string, admin: boolean): KeyRow =>
  admin ? { name, scopes_words: scopes, last_used_words: used, id, can_revoke: true }
    : { name, scopes_words: scopes, last_used_words: used };

const people = (keys: KeyRow[], admin: boolean): SreSettings["people"] => ({
  title: "People and keys", editable: admin, keys, scope_options: SCOPES,
  empty_words: keys.length ? null : "No keys yet.",
  workspace: { words: "Manage people in your workspace", link: { label: "Workspace settings", value: "/settings/advanced?tab=workspace" } },
});

const edition = (admin: boolean): SreSettings["edition"] => ({
  title: "Edition", editable: admin, value: "sre", label: "AI SRE", can_switch: admin,
  options: [{ value: "factory", label: "Software Factory" }, { value: "sre", label: "AI SRE" }], warning: WARNING,
});

const KEYS = (admin: boolean): KeyRow[] => [
  key("k1", "console admin", "Can change settings", "Used today", admin),
  key("k2", "Alex laptop", "Can read", "Used 3 days ago", admin),
  key("k3", "ci reader", "Can read", "Never used", admin),
  key("k4", "Kim data feed", "Can send data", "Used 1 minute ago", admin),
];

const busy = (admin = true): SreSettings => ({
  watching: { ...watching(), editable: admin },
  investigating: { ...investigating(), editable: admin },
  notes: { ...notes(true), editable: admin },
  tools: {
    title: "Tools", editable: false, add: ADD, empty_words: null,
    rows: [
      row("docs-site", "Vercel request logs", true, "receiving", "Receiving, last data 2 minutes ago"),
      row("github", "GitHub", false, "error", "The token has expired",
        "The token has expired. Deploys and pull requests are not coming in."),
      row("prod-otlp", "Traces and logs", true, "receiving", "Receiving, last data 20 seconds ago"),
      slackRow(true),
    ],
  },
  people: people(KEYS(admin), admin),
  edition: edition(admin),
  advanced: ADVANCED,
  can_edit: admin,
});

// A cell with nothing set up: no watcher and no investigator (so the page says plainly that nothing
// is checked and nothing is spent), Slack not connected, only the console key.
const fresh = (): SreSettings => ({
  watching: {
    title: "Watching", editable: true, agent: undefined,
    empty_words: "No watcher is set up yet, so nothing is being checked and nothing has been spent.",
    fields: [f("system_name", "What we call what is watched", "", "your services, the default", null,
      "Used in messages and on the home page. Leave empty to use the name of this instance.")],
  },
  investigating: {
    title: "Investigating", editable: true, agent: undefined, fields: [],
    empty_words: "No investigator is set up yet, so nothing has been looked into and nothing has been spent.",
  },
  notes: notes(false),
  tools: {
    title: "Tools", editable: false, add: ADD, rows: [slackRow(false)],
    empty_words: "Nothing is connected yet. Add a tool below to start.",
  },
  people: people([key("k1", "console admin", "Can change settings", "Used today", true)], true),
  edition: edition(true),
  advanced: ADVANCED,
  can_edit: true,
});

const LONG_NAME =
  "The checkout, payments and legacy gateway services, and every worker that handles their background jobs in the eu-west region";
const LONG_RULE =
  "Compare each service with its own last 7 days, and compare checkout, payments and the legacy gateway with each other when they share a customer. Raise it only when errors or slowness are clearly worse than usual and have lasted more than one check. Ignore the nightly 02:00 log rotation, the weekly 04:00 backup and any burst within ten minutes of a deploy that was rolled back.";

const long = (): SreSettings => {
  const b = busy();
  return { ...b, watching: watching({ name: LONG_NAME, text: LONG_RULE, rule: ["service", "status", "version", "namespace", "region"] }) };
};

const many = (): SreSettings => {
  const b = busy();
  const rows: ToolRow[] = Array.from({ length: 19 }, (_, i) =>
    row(`service-${String(i + 1).padStart(2, "0")}`, "Traces and logs", i !== 6, i !== 6 ? "receiving" : "error",
      i !== 6 ? `Receiving, last data ${i + 1} minutes ago` : "No data for 2 hours",
      i !== 6 ? null : "No data for 2 hours. Check that the service still sends traces."));
  rows.push(slackRow(true));
  const names = ["Alex", "Kim", "Lee"];
  const keys: KeyRow[] = [key("m0", "console admin", "Can change settings", "Used today", true)];
  for (let i = 1; i < 30; i++) {
    keys.push(key(`m${i}`, `${names[i % 3]} laptop ${i}`, i % 5 === 0 ? "Can send data" : "Can read",
      i % 4 === 0 ? "Never used" : `Used ${i} days ago`, true));
  }
  return { ...b, tools: { ...b.tools, rows }, people: people(keys, true) };
};

/** The body a sample answers with. `load` and `err` never answer a body, see sampleLoad. */
export function sampleSettings(name: SampleName): SreSettings {
  switch (name) {
    case "new": return fresh();
    case "ro": return busy(false);
    case "long": return long();
    case "many": return many();
    default: return busy();
  }
}

/** What a sample's load does: `load` never answers (the loading state stays) and `err` fails. */
export function sampleLoad(name: SampleName): Promise<SreSettings> {
  if (name === "load") return new Promise(() => { /* never answers */ });
  if (name === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(sampleSettings(name));
}

/** The partial-save sentence the cell gives when the budget is not a number. */
export const SAMPLE_PARTIAL =
  "Saved what we call what is watched and how often it checks. Did not save the budget: it must be a number of dollars, such as 25.";
