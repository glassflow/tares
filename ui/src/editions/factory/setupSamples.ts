// Invented answers of GET /api/factory/setup for the "Set up your factory" screen. They show only on
// an address with `?sample=<name>` in a browser that has the sample flag, never on a live cell. The
// names, people and addresses are made up. Each one is shaped like the real answer
// (tares/screens/factory_setup.py), so the screen draws a sample the way it draws the real read.

import type {
  SetupAction, SetupApp, SetupAvailable, SetupConfirm, SetupConnected, SetupData, SetupField, SetupForm, SetupSlot, SetupStep,
} from "./FactorySetup.api";

const NAMES: Record<SetupSlot, [string, string]> = {
  tickets: ["Tickets", "Where the crew keeps its work: the tickets projects are planned in."],
  review: ["Review", "Who reviews each pull request before it can merge."],
  ci: ["CI", "Where the crew reads check results for a pull request."],
  memory: ["Memory", "Where the crew keeps what it learns about your code and your team."],
  monitoring: ["Monitoring", "Where the traces of crew sessions go."],
  guardrails: ["Guardrails", "Who may stop an action before it happens."],
  runtime: ["Runtime", "What crew members start as."],
};
const SLOTS = Object.keys(NAMES) as SetupSlot[];

const SAVED_NOTE = "A key is saved. Leave this blank to keep it.";

function field(name: string, label: string, kind: SetupField["kind"], extra: Partial<SetupField> = {}): SetupField {
  const required = extra.required ?? false;
  const base: SetupField = {
    name, label, kind, required, requirement: required ? "Required" : "Optional", placeholder: "", help: "",
    error: kind === "bool" ? "" : `Enter ${label.toLowerCase()}.`, options: [], rows: [], saved: false, saved_note: "",
  };
  if (kind === "secret") {
    if (extra.saved) return { ...base, placeholder: "Saved. Leave blank to keep it.", saved_note: SAVED_NOTE, ...extra };
    return { ...base, ...extra };
  }
  return { value: kind === "bool" ? false : "", ...base, ...extra };
}

function stages(values: Record<string, string> = {}) {
  const keys: [string, string][] = [["todo", "To do"], ["in_progress", "In progress"], ["in_review", "In review"], ["done", "Done"]];
  return keys.map(([key, label]) => ({ key, label, aria_label: `Jira status for ${label.toLowerCase()}`, value: values[key] ?? "" }));
}

function form(mode: "connect" | "edit", title: string, lead: string, fields: SetupField[], asDefault = true): SetupForm {
  return {
    mode, title: `${mode === "connect" ? "Connect" : "Edit"} ${title}`, lead,
    save_label: mode === "connect" ? "Save and connect" : "Save", save_as_default: asDefault,
    test_hint: `Test connection checks the settings above with ${title}. Nothing is saved by it.`, fields,
  };
}

const LINEAR_KEY = (saved: boolean) => field("api_key", "API key", "secret", {
  required: true, saved, placeholder: saved ? "Saved. Leave blank to keep it." : "lin_api_",
  help: "A Linear personal API key. In Linear, open your account, then Security and access, then Personal API keys.",
  error: "Enter the API key.",
});
const GITHUB_FIELDS = (repo = "", label = "") => [
  field("repo", "Repository", "text", { required: true, value: repo, placeholder: "owner/name",
    help: "The repository that holds the issues, as owner/name, for example northwind/orders-api.",
    error: "Enter the repository as owner/name, for example northwind/orders-api." }),
  field("issue_label", "Issue label", "text", { value: label, help: "The label every issue of the project carries. Left empty, it is tares: followed by the project's name." }),
];
const JIRA_FIELDS = (saved: boolean, status: Record<string, string> = {}) => [
  field("site", "Site address", "url", { required: true, value: saved ? "https://northwind.atlassian.net" : "", placeholder: "https://northwind.atlassian.net",
    help: "For Jira Cloud the address ends in atlassian.net. For Jira Data Center, use the address you open Jira at.", error: "Enter the site address." }),
  field("email", "Email", "text", { required: true, value: saved ? "dana@northwind.example" : "", help: "The email of the Jira account that owns the API token.", error: "Enter the email of the Jira account." }),
  field("token", "API token", "secret", { required: true, saved, help: "A Jira API token for that account. Jira Data Center calls it a personal access token.", error: "Enter the API token." }),
  field("status", "Status mapping", "mapping", { required: true, rows: stages(status), help: "Match each Tares stage to the Jira status that means it.", error: "Enter the status for each stage." }),
];

interface ConnectedOpts {
  title: string; description: string; use?: string; isDefault?: boolean; builtIn?: boolean; picked?: [string, string][];
  follows?: number; actions?: string[]; form?: SetupForm | null; tags?: SetupConnected["tags"]; via?: "app" | "key" | null; who?: string;
  blocked?: string; confirm?: Partial<SetupConfirm>;
}

function connected(id: string, o: ConnectedOpts): SetupConnected {
  const picked = (o.picked ?? []).map(([uid, name]) => ({ uid, name }));
  const labels: Record<string, string> = { edit: "Edit settings", make_default: "Make default", remove: o.via === "app" ? "Disconnect" : "Remove" };
  const actions = (o.actions ?? []).map((key) => ({ key: key as SetupAction["key"], label: labels[key] }));
  const tags: SetupConnected["tags"] = [];
  if (o.isDefault) tags.push({ key: "default", text: "Default" });
  if (o.builtIn) tags.push({ key: "built_in", text: "Built in" });
  tags.push(...(o.tags ?? []));
  const verb = o.via === "app" ? "Disconnect" : "Remove";
  const gone = `The saved settings of ${o.title} are deleted.`;
  const confirm = actions.some((a) => a.key === "remove")
    ? {
        title: `${verb} ${o.title}`,
        text: picked.length ? `${picked.length} ${picked.length === 1 ? "project picks" : "projects pick"} ${o.title}: ${picked.map((p) => p.name).join(", ")}. They move to the default, Tares. ${gone}` : gone,
        yes: `${verb} it`, no: "Keep it", moves_projects: picked.length > 0, ...o.confirm,
      }
    : null;
  return {
    id, title: o.title, description: o.description, tags, default: !!o.isDefault, built_in: !!o.builtIn, via: o.via ?? null, who: o.who ?? "",
    picked_by: picked, follows: o.follows ?? 0, use: o.use ?? "", actions,
    blocked: o.blocked ? { key: "remove", text: o.blocked } : null, confirm, form: o.form ?? null, last_test: null,
  };
}

function available(id: string, title: string, description: string, f: SetupForm | null, o: Partial<SetupAvailable> = {}): SetupAvailable {
  return { id, title, description, enterprise: false, allowed: true, button: "Connect", reason: null, app: null, form: f, ...o };
}

const FOOTER = (n: number) => `Step ${n} of 7. Projects pick one of the connected integrations; they never hold a key.`;
const NO_LICENCE = "This cell has no licence. Without it, {t} cannot be connected. Your crew keeps using the built-in choice for this step.";

function step(slot: SetupSlot, o: Partial<SetupStep> & { connected: SetupConnected[]; available: SetupAvailable[] }): SetupStep {
  const i = SLOTS.indexOf(slot);
  const dflt = o.connected.find((c) => c.default)?.title ?? o.connected[0]?.title ?? "";
  const isSet = o.set ?? (o.connected.length > 1 || o.connected.some((c) => !c.built_in));
  return {
    slot, number: i + 1, name: NAMES[slot][0], why: NAMES[slot][1], state: isSet ? "set" : "not_set", set: isSet,
    line: dflt + (o.connected.length > 1 ? ` and ${o.connected.length - 1} more` : "") + (isSet ? "" : " (not set yet)"),
    note: null, banner: null, connected_heading: `Connected (${o.connected.length})`, available_heading: "Connect another",
    footer: { hint: FOOTER(i + 1), next: i === 6 ? "Finish setup" : `Next: ${NAMES[SLOTS[i + 1]][0]}` }, ...o,
  };
}

const GUARDRAILS_NOTE = "Your own rules and the checks asked before a build, a merge or a release are on the factory Guardrails page, not here.";

function page(steps: SetupStep[], o: Partial<SetupData> = {}): SetupData {
  const done = steps.filter((s) => s.set).length;
  return {
    title: "Set up your factory", exit_label: "Exit setup", edition: { key: "self_hosted", name: "Self-hosted", cloud: false },
    licence: { state: "none", ended_on: null, text: "This cell has no licence." },
    progress: { set: done, total: steps.length, text: `${done} of ${steps.length} steps set` }, steps, ...o,
  };
}

// ---- a fresh cell: the built-in choice on every step, nothing that needs a licence connected ----

function fresh(): SetupData {
  const lic = (t: string) => NO_LICENCE.replace("{t}", t);
  return page([
    step("tickets", {
      connected: [connected("tares", { title: "Tares", description: "Tickets live in Tares.", isDefault: true, builtIn: true, use: "No project picks it. Every project that picks none (0) follows it.", follows: 0 })],
      available: [
        available("linear", "Linear", "Tickets live in a Linear project. Each project picks its Linear project on its own Integrations page.", form("connect", "Linear", "Tickets live in a Linear project. It becomes the default.", [LINEAR_KEY(false)])),
        available("github", "GitHub", "Tickets are issues in one repository.", form("connect", "GitHub", "Tickets are issues in one repository. It becomes the default.", GITHUB_FIELDS())),
        available("jira", "Jira", "Tickets live in a Jira project. Works with Jira Cloud and Jira Data Center.", null, { enterprise: true, allowed: false, button: "Connect", reason: lic("Jira") }),
      ],
    }),
    step("review", {
      connected: [connected("tares", { title: "Tares", description: "The built-in reviewer on your crew.", isDefault: true, builtIn: true, use: "Every project uses it." })],
      available: [
        available("coderabbit", "CodeRabbit", "CodeRabbit reviews each pull request.", null, { enterprise: true, allowed: false, reason: lic("CodeRabbit") }),
      ],
    }),
    step("ci", {
      connected: [connected("local", { title: "Local checks", description: "Uses the checks recorded on Tares.", isDefault: true, builtIn: true, use: "Every project uses it." })],
      available: [
        available("github", "GitHub Actions", "Reads check results from GitHub.", form("connect", "GitHub Actions", "Reads check results from GitHub. It replaces Local checks.", [
          field("no_ci", "This repository has no CI", "bool", { help: "Tick this when a repository has no checks at all, so a pull request is not held back waiting for results." }),
        ])),
      ],
    }),
    step("memory", {
      connected: [connected("tares", { title: "Tares", description: "Memory lives in Tares docs.", isDefault: true, builtIn: true, use: "Every project uses it." })],
      available: [available("cognee", "Cognee", "Memory is kept in Cognee.", null, { enterprise: true, allowed: false, reason: lic("Cognee") })],
    }),
    step("monitoring", {
      connected: [connected("off", { title: "Off", description: "Nothing is sent.", isDefault: true, builtIn: true, use: "Every project uses it." })],
      available: [
        available("rius", "Rius", "Traces go to Rius.", form("connect", "Rius", "Traces go to Rius. It replaces Off.", [
          field("api_key", "Rius API key", "secret", { help: "Sent to Rius as a bearer key. A change reaches sessions that start after you save.", placeholder: "rius_" }),
          field("endpoint", "Address", "url", { help: "Leave empty to use Rius's own address.", placeholder: "https://" }),
        ])),
        available("otlp", "OpenTelemetry", "Traces go to any OpenTelemetry backend.", form("connect", "OpenTelemetry", "Traces go to any OpenTelemetry backend. It replaces Off.", [
          field("endpoint", "Address", "url", { required: true, placeholder: "https://collector.northwind.example:4318", help: "An http or https address that accepts OTLP over HTTP.", error: "Enter the address of the OTLP endpoint." }),
          field("api_key", "API key", "secret", { help: "Sent as a bearer key." }),
        ])),
      ],
    }),
    step("guardrails", {
      connected: [connected("tares", { title: "Tares", description: "Built-in rules, always on.", isDefault: true, builtIn: true, use: "No check uses it." })],
      available: [available("webhook", "Policy webhook", "Ask your own policy service before an action.", null, { enterprise: true, allowed: false, reason: lic("Policy webhook") })],
      note: GUARDRAILS_NOTE,
    }),
    step("runtime", {
      connected: [connected("claude_code", { title: "Claude Code", description: "Crew members start as Claude Code background sessions.", isDefault: true, builtIn: true, use: "Every project uses it." })],
      available: [], set: true,
    }),
  ]);
}

// ---- a cell with a licence and most steps connected ----

function filled(): SetupData {
  const d = fresh();
  const P = (n: [string, string][]) => n;
  const s = (slot: SetupSlot) => d.steps[SLOTS.indexOf(slot)];
  const edit = (title: string, desc: string, f: SetupField[]) => form("edit", title, `${desc} Projects that pick it use the new settings from the next time they read them.`, f, false);
  const steps: SetupStep[] = [
    step("tickets", {
      connected: [
        connected("tares", { title: "Tares", description: "Tickets live in Tares.", builtIn: true, actions: ["make_default"], use: "No project picks it." }),
        connected("linear", {
          title: "Linear", description: "Tickets live in a Linear project. Each project picks its Linear project on its own Integrations page.", isDefault: true, tags: [{ key: "key", text: "API key" }], via: "key",
          picked: P([["pr1", "orders-api"]]), follows: 2, use: "Picked by orders-api. Every project that picks none (2) follows it.", actions: ["edit"],
          form: edit("Linear", "Tickets live in a Linear project.", [LINEAR_KEY(true)]),
        }),
        connected("github", {
          title: "GitHub", description: "Tickets are issues in one repository.", picked: P([["pr2", "billing-service"], ["pr4", "warehouse-sync"]]),
          use: "Picked by billing-service, warehouse-sync.", actions: ["edit", "make_default", "remove"],
          form: edit("GitHub", "Tickets are issues in one repository.", GITHUB_FIELDS("northwind/orders-api", "tares:orders")),
        }),
        connected("jira", {
          title: "Jira", description: "Tickets live in a Jira project. Works with Jira Cloud and Jira Data Center.", use: "No project picks it.", actions: ["edit", "make_default", "remove"],
          form: edit("Jira", "Tickets live in a Jira project.", JIRA_FIELDS(true, { todo: "To Do", in_progress: "In Progress", in_review: "In Review", done: "Done" })),
        }),
      ],
      available: [],
    }),
    step("review", {
      connected: [
        connected("tares", { title: "Tares", description: "The built-in reviewer on your crew.", builtIn: true, actions: ["make_default"], use: "Every project that picks none follows the default." }),
        connected("coderabbit", {
          title: "CodeRabbit", description: "CodeRabbit reviews each pull request.", isDefault: true, use: "Every project uses it.", actions: ["edit"],
          form: edit("CodeRabbit", "CodeRabbit reviews each pull request.", [field("key", "API key", "secret", { required: true, saved: true, help: "A CodeRabbit API key for this organisation." })]),
        }),
      ],
      available: [],
    }),
    s("ci"),
    step("memory", {
      connected: [
        connected("cognee", {
          title: "Cognee", description: "Memory is kept in Cognee.", isDefault: true, use: "Every project uses it.", actions: ["edit"],
          form: edit("Cognee", "Memory is kept in Cognee.", [
            field("url", "Address", "url", { required: true, value: "https://cognee.northwind.example", help: "Where your Cognee server answers." }),
            field("key", "API key", "secret", { saved: true, help: "Needed only when your Cognee server asks for one." }),
            field("mode", "How it searches", "select", { options: ["Fast", "Balanced", "Thorough"], value: "Balanced", help: "Thorough takes longer and finds more." }),
          ]),
        }),
      ],
      available: [],
    }),
    step("monitoring", {
      connected: [
        connected("otlp", {
          title: "OpenTelemetry", description: "Traces go to any OpenTelemetry backend.", isDefault: true, use: "Every project uses it.", actions: ["edit"],
          form: edit("OpenTelemetry", "Traces go to any OpenTelemetry backend.", [
            field("endpoint", "Address", "url", { required: true, value: "https://collector.northwind.example:4318", help: "An http or https address that accepts OTLP over HTTP." }),
            field("api_key", "API key", "secret", { saved: true, help: "Sent as a bearer key." }),
          ]),
        }),
      ],
      available: [],
    }),
    step("guardrails", {
      connected: [
        connected("tares", { title: "Tares", description: "Built-in rules, always on.", builtIn: true, use: "No check uses it.", actions: ["make_default"] }),
        connected("webhook", {
          title: "Policy webhook", description: "Ask your own policy service before an action.", isDefault: true, use: "1 check uses it.", actions: ["edit"],
          form: edit("Policy webhook", "Ask your own policy service before an action.", [
            field("url", "Address", "url", { required: true, value: "https://policy.northwind.example/check", help: "Where your policy service answers." }),
            field("secret", "Signing secret", "secret", { saved: true, help: "Used to sign each request, so your service can tell it came from this cell." }),
          ]),
        }),
      ],
      available: [], note: GUARDRAILS_NOTE,
    }),
    s("runtime"),
  ];
  return page(steps, { licence: { state: "valid", ended_on: null, text: "" } });
}

// ---- the licence ended: Tickets still lists Linear and says the crew has fallen back ----

function expired(): SetupData {
  const d = filled();
  const t = d.steps[0];
  t.banner = {
    lead: "Your licence ended on 3 November.",
    text: "Your licence ended on 3 November. Linear is still connected, but the crew now keeps its tickets in Tares until you renew. Your Linear settings are kept.",
  };
  t.connected[1].tags.push({ key: "licence_ended", text: "Licence ended" });
  d.steps[1].connected[1].tags.push({ key: "licence_ended", text: "Licence ended" });
  d.licence = { state: "ended", ended_on: "3 November", text: "Your licence ended on 3 November." };
  return d;
}

// ---- a project is moved off a pick before the connection goes: the question shows its own text ----

function used(): SetupData {
  const d = filled();
  d.steps[0].connected[2].confirm = {
    title: "Remove GitHub",
    text: "2 projects pick GitHub: billing-service, warehouse-sync. They move to the default, Linear. The saved settings of GitHub are deleted.",
    yes: "Remove it", no: "Keep it", moves_projects: true,
  };
  return d;
}

// ---- long names, long help and long answers: nothing may run past its box ----

const LONG_REPO = "northwind-platform-engineering-organisation/customer-orders-and-fulfilment-service-monorepo-with-a-very-long-name";
function long(): SetupData {
  const d = filled();
  const t = d.steps[0];
  t.why = "Where the crew keeps its work: the tickets projects are planned in, whichever system your organisation has settled on for planning across every team.";
  t.connected[1].picked_by = [["pr1", "orders-api-with-a-very-long-project-name-for-the-checkout-flow"], ["pr6", "warehouse-synchronisation-and-reconciliation-service"]].map(([uid, name]) => ({ uid, name }));
  t.connected[1].use = `Picked by ${t.connected[1].picked_by.map((p) => p.name).join(", ")}. Every project that picks none (14) follows it.`;
  t.connected[1].description = "Tickets live in a Linear project. Each project picks its Linear project on its own Integrations page, so the same Linear workspace can hold a very large number of projects, each with its own team and its own milestones.";
  t.connected[2].title = "GitHub Enterprise Server (northwind-platform-engineering)";
  t.connected[2].form = form("edit", "GitHub Enterprise Server (northwind-platform-engineering)", "Tickets are issues in one repository. Projects that pick it use the new settings from the next time they read them.", [
    field("repo", "Repository", "text", { required: true, value: LONG_REPO, placeholder: "owner/name", help: "The repository that holds the issues, as owner/name, for example northwind/orders-api. A repository with a long name is shown in full here, and it wraps on a narrow screen instead of running past the edge." }),
    field("issue_label", "Issue label", "text", { value: "tares:" + "orders-".repeat(14), help: "The label every issue of the project carries." }),
  ], false);
  return d;
}

// ---- many connected integrations and many projects ----

function many(): SetupData {
  const d = filled();
  const t = d.steps[0];
  const names = ["orders-api", "billing-service", "storefront", "warehouse-sync", "mobile-app", "search-index", "payments-gateway", "notifications", "identity", "analytics-pipeline", "reporting", "admin-console"];
  const extra = ["Gitea", "GitLab", "Azure Boards", "Shortcut", "ClickUp", "Asana", "Trello", "Monday"].map((n, i) => connected(`more${i}`, {
    title: n, description: `Tickets live in ${n}.`, use: "No project picks it.", actions: ["edit", "make_default", "remove"],
    form: form("edit", n, `Tickets live in ${n}.`, [field("site", "Site address", "url", { required: true, value: `https://${n.toLowerCase().replace(/ /g, "")}.northwind.example` })], false),
  }));
  t.connected[1].picked_by = names.map((name, i) => ({ uid: `p${i}`, name }));
  t.connected[1].use = `Picked by ${names.join(", ")}. Every project that picks none (31) follows it.`;
  t.connected.push(...extra);
  t.connected_heading = `Connected (${t.connected.length})`;
  t.line = `Linear and ${t.connected.length - 1} more`;
  return d;
}

// ---- Tares Cloud: Linear and GitHub connect with an app, or with a key ----

const LINEAR_APP: SetupApp = {
  url: "https://connect.example.test/linear", service: "Linear", app_name: "GlassFlow Linear app", button: "Connect Linear", repo_step: false,
  steps: ["Linear opens in this window.", "Sign in and choose your workspace.", "Approve the GlassFlow Linear app. You come back to this step to save it."],
  note: "Tares keeps you signed in and renews the access for you, so there is no key to create. Disconnect on the Tickets step takes the access away in Linear again.",
  use_key_label: "Use an API key instead", use_app_label: "Connect with the Linear app instead", stage: "start", done: null,
};
const GITHUB_APP: SetupApp = {
  url: "https://connect.example.test/github", service: "GitHub", app_name: "GlassFlow GitHub App", button: "Connect GitHub", repo_step: true,
  steps: ["GitHub opens in this window.", "Choose your account or organisation and the repositories the crew may use.", "Approve the GlassFlow GitHub App. You come back here to choose the repository."],
  note: "Tares reads and writes issues through the app, so there is no credential to create. Disconnect on the Tickets step takes the access away in GitHub again.",
  use_key_label: "Use a GitHub credential instead", use_app_label: "Connect with the GitHub App instead", stage: "start", done: null,
};

function cloud(stage: "start" | "done" = "start"): SetupData {
  const d = fresh();
  const t = d.steps[0];
  const gh: SetupApp = stage === "done" ? { ...GITHUB_APP, stage: "done", done: "GitHub is connected. The GlassFlow GitHub App is installed on northwind." } : GITHUB_APP;
  t.available = [
    available("linear", "Linear", "Tickets live in a Linear project. Each project picks its Linear project on its own Integrations page.",
      form("connect", "Linear", "Tickets live in a Linear project. It becomes the default.", [LINEAR_KEY(false)]), { button: "Connect Linear", app: LINEAR_APP }),
    available("github", "GitHub", "Tickets are issues in one repository.",
      form("connect", "GitHub", "Tickets are issues in one repository. It becomes the default.", [...GITHUB_FIELDS(), field("credential", "GitHub credential", "text", { help: "The name of a GitHub credential stored on this cell." })]),
      { button: "Connect GitHub", app: gh }),
  ];
  d.edition = { key: "cloud", name: "Tares Cloud", cloud: true };
  d.licence = { state: "valid", ended_on: null, text: "" };
  return d;
}

/** An app connection is a row that cannot be edited and is disconnected, not removed. */
function appRow(): SetupData {
  const d = cloud("done");
  const t = d.steps[0];
  t.connected.push(connected("linear", {
    title: "Linear", description: "Linear is connected through the GlassFlow Linear app of Dana Whitfield.", via: "app", who: "of Dana Whitfield",
    tags: [{ key: "app", text: "Connected with the app" }], use: "No project picks it.", actions: ["make_default", "remove"],
  }));
  t.available = t.available.filter((a) => a.id !== "linear");
  return d;
}

const SAMPLES: Record<string, () => SetupData> = {
  empty: fresh, filled, expired, used, long, many, cloud: () => cloud(), ghdone: () => cloud("done"), approw: appRow,
};

export const isSample = (name: string | null): name is string => !!name && (name in SAMPLES || name === "load" || name === "err");

/** `load` never answers (the page shows its loading state); `err` fails as a cell that is down. */
export function sampleLoad(name: string): Promise<SetupData> {
  if (name === "load") return new Promise(() => {});
  if (name === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(SAMPLES[name]());
}
