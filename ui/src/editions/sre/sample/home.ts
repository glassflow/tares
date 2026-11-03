// Sample data for the AI SRE home page, shown at /?sample=<name> when the browser has the sample
// flag (see ../../sample.ts). It is invented: edge-proxy, service-a, service-a-receiver,
// service-a-ui, docs-site, example-org/service-a, the system "Acme". One body per state of the page.

import type { StartAction, StartBlock, StartStep } from "../../StartChecklist.api";
import type {
  HomeBlindSpot, HomeChart, HomeIncident, HomeOnWatch, HomeRaised, HomeService, HomeTool, HomeWatch,
  SreHome,
} from "../Home.api";

export const HOME_SAMPLES = [
  "watcher-off", "watcher-off-monthly", "all-quiet", "one-incident", "many-incidents", "new-cell",
  "silent", "long-text", "many-items", "start-partial", "start-demo", "load", "error",
] as const;
export type HomeSample = (typeof HOME_SAMPLES)[number];

export const isHomeSample = (n: string | null): n is HomeSample => HOME_SAMPLES.some((s) => s === n);

const FOREVER = new Promise<never>(() => {});
const REPO = "example-org/service-a";
const MIN = 60_000;
const ago = (minutes: number) => new Date(Date.now() - minutes * MIN).toISOString();
/** "14:09" in UTC, the way the cell words a time. */
const hm = (iso: string) => new Date(iso).toISOString().slice(11, 16);

// ── the Start checklist: the rows and words come from tares/start_steps.py ───
const row = (key: string, title: string, gets: string, actions: StartAction[], done: boolean): StartStep =>
  ({ key, title, gets, done, actions: done ? [] : actions });

/** The checklist with the keys in `done` ticked. `demo` adds the sixth row. */
const startBlock = (done: string[], show = true, demo = false): StartBlock => {
  const has = (k: string) => done.includes(k);
  const steps = [
    row("model", "Add a model provider", "The agents that watch and investigate need an AI model.", [{ label: "Add a provider", to: "/settings/advanced" }], has("model")),
    row("signals", "Connect logs or metrics", "Tares sees how your services behave.",
        [{ label: "Connect", to: "/settings#tools" }, { label: "Try the demo service", do: "start_demo" }], has("signals")),
    row("services", "See your services appear", "Each of your services, with its state.", [{ label: "Open services", to: "/services" }], has("services")),
    row("watching", "Turn on watching", "An agent looks at every change and tells you when something breaks.", [{ label: "Turn on watching", to: "/settings#watching" }], has("watching")),
    row("slack", "Choose the Slack channel for notes", "Incident notes land where your team talks.", [{ label: "Choose a channel", to: "/settings#notes" }], has("slack")),
    ...(demo ? [row("test_incident", "Cause a test incident on the demo", "See a full incident note written in about a minute.", [{ label: "Open the demo", to: "/incidents" }], has("test_incident"))] : []),
  ];
  const n = steps.filter((s) => s.done).length;
  return { steps, done: n, total: steps.length, show: show && n < steps.length };
};
const ALL_FIVE = ["model", "signals", "services", "watching", "slack"];

// ── pieces ───────────────────────────────────────────────────────────────────
const wave = (seed: number, scale: number, n = 24): number[] =>
  Array.from({ length: n }, (_, i) => Math.round(Math.max(0, Math.sin((i + seed) * 1.3) * scale + scale)));

const service = (s: Partial<HomeService> & Pick<HomeService, "name" | "state" | "state_words">): HomeService => ({
  about: null, signals_words: "logs", last_words: "checked 3 minutes ago", spark: wave(s.name.length, 2), ...s,
});

const edgeProxy = (over: Partial<HomeService> = {}) => service({ name: "edge-proxy", about: "All traffic into Acme.", state: "normal", state_words: "Normal", ...over });
const serviceA = (over: Partial<HomeService> = {}) => service({ name: "service-a", about: "The Acme API.", state: "normal", state_words: "Normal", signals_words: "traces and logs", ...over });
const docsSite = (over: Partial<HomeService> = {}) => service({ name: "docs-site", about: "The docs site.", state: "normal", state_words: "Normal", ...over });
const serviceUi = (): HomeService => service({
  name: "service-a-ui", state: "no_own_signal", state_words: "Seen only through edge-proxy", signals_words: "status codes only",
  last_words: "not checked, it has no events of its own", spark: [],
});
const receiver = (over: Partial<HomeService> = {}) => service({
  name: "service-a-receiver", about: "No app logs and no deploys arrive.", state: "silent", state_words: "Sent nothing lately",
  signals_words: "no signals yet", last_words: "not checked yet", spark: [], ...over,
});

const FEEDS = "from the docs-feed and prod-otlp feeds";

const TOOLS: HomeTool[] = [
  { name: "Traces and logs", ok: true }, { name: "Code changes", ok: true }, { name: "Slack", ok: true },
];

const SPOT_RECEIVER: HomeBlindSpot = {
  service: "service-a-receiver", kind: "silent", title: "service-a-receiver is silent",
  detail: "No app logs and no deploys arrive. If it breaks, the investigator has nothing to read.",
  action: { label: "Connect its logs", to: "/settings#tools" },
};
const SPOT_UI: HomeBlindSpot = {
  service: "service-a-ui", kind: "seen_via_other", title: "service-a-ui has no signal of its own",
  detail: "Problems show up only as edge-proxy errors, so causes stay guesses.",
  action: { label: "Connect its logs", to: "/settings#tools" },
};
const SPOT_DEPLOYS: HomeBlindSpot = {
  service: "", kind: "no_deploys", title: "Deploys are not linked to services",
  detail: "Code pushes arrive, but no note has been able to tie an error to a deploy.",
  action: { label: "Map repos to services", to: "/settings#tools" },
};

const worker = (name: string, role: HomeOnWatch["role"], state: string, doing: string, cost: string): HomeOnWatch => ({
  role, name, state, doing, model_words: cost.split(",")[0], cost_words: cost,
});
const ON_WATCH: HomeOnWatch[] = [
  worker("Watcher", "watcher", "running", "Checks docs-feed and prod-otlp every 10 minutes", "Haiku, Spent $3.74 of $25 this month"),
  worker("Investigator", "investigator", "waiting", "Looks into what the watcher raises", "Sonnet, Spent $0.36 of $10 this month"),
];
const NOTES = { label: "#ops-alerts in Slack" };

const WATCH_ON: HomeWatch = {
  on: true, reason: null, reason_words: null, since: null, off_for_words: null, missed_checks: null,
  every_words: "every 10 minutes", last_check: { at: ago(3), said: "Traffic steady, status codes normal. Nothing to raise." },
  cost_per_check_usd: 0.005, spent_words: "Spent $3.74 of $25 this month", resets_on: null, told: null, ways_out: [],
};

const raised = (id: string, when: string, svc: string, what: string, outcome: string, can = true, worth: boolean | null = null): HomeRaised => ({
  id, at: ago(60), when_words: when, service: svc, what, outcome_words: outcome, worth, can_answer: can,
});
const OLD_RAISED: HomeRaised[] = [
  raised("chk-a1", "29 Sep", "docs-site", "burst of 404s on /.env from a scanner", "Investigated: no real users hit. Resolved itself."),
  raised("chk-a2", "28 Sep", "edge-proxy", "errors looked at by the old agent", "Incident note written. Nobody marked it handled.", false),
  raised("chk-a3", "24 Aug", "edge-proxy", "4xx and 5xx spike around 12:37 UTC", "Incident note written. Nobody marked it handled.", false),
];

const BASE: SreHome = {
  edition: "sre", system: "Acme",
  headline: { text: "All quiet", tone: "quiet", sub: "3 services watched, last check 4 minutes ago. service-a-receiver is silent." },
  watch: WATCH_ON, incidents: [], incidents_more: 0,
  services: [edgeProxy(), serviceA(), docsSite(), serviceUi(), receiver()],
  services_from_words: FEEDS, raised: OLD_RAISED, raised_note: "Every check since then came back quiet.",
  today: { checks: 95, quiet: 95, investigated: 0, spent_usd: 0.48 }, on_watch: ON_WATCH, notes_go_to: NOTES,
  blind_spots: [SPOT_RECEIVER, SPOT_UI, SPOT_DEPLOYS], tools: TOOLS,
  start: startBlock(ALL_FIVE),
};

// ── the watcher is off ───────────────────────────────────────────────────────
const OFF_SERVICES = (since: string): HomeService[] => [
  edgeProxy({ state: "not_watched", state_words: `Not watched since ${since}`, last_words: "checked 6 days ago" }),
  serviceA({ state: "not_watched", state_words: `Not watched since ${since}`, last_words: "checked 6 days ago" }),
  docsSite({ state: "not_watched", state_words: `Not watched since ${since}`, last_words: "checked 6 days ago" }),
  serviceUi(), receiver(),
];

const OFF_WATCH: HomeWatch = {
  on: false, reason: "budget_used", reason_words: "the watcher used its $25.00 budget", since: ago(6 * 1440),
  off_for_words: "6 days", missed_checks: 950, every_words: "every 10 minutes",
  last_check: { at: ago(6 * 1440), said: "Traffic down 3.2%, status codes normal. Nothing to raise." },
  cost_per_check_usd: 0.005, spent_words: "Spent $25.00 of $25", resets_on: null, told: null,
  ways_out: [{ action: "raise_budget", label: "Add $25 and restart", amount_usd: 25, primary: true }],
};

const OFF_ON_WATCH: HomeOnWatch[] = [
  worker("Watcher", "watcher", "stopped", "The watcher used its $25.00 budget", "Haiku, Spent $25.00 of $25"),
  worker("Investigator", "investigator", "waiting", "Looks into what the watcher raises", "Sonnet, Spent $0.00 of $10"),
];

const WATCHER_OFF: SreHome = {
  ...BASE,
  headline: { text: "Nobody has watched Acme since 27 Oct, 00:05", tone: "unwatched", sub: "Your 5 services keep sending data. The watcher used its $25.00 budget." },
  watch: OFF_WATCH, services: OFF_SERVICES("27 Oct"),
  raised_note: "The checks before the watcher stopped all came back quiet.",
  today: { checks: 0, quiet: 0, investigated: 0, spent_usd: 0 }, on_watch: OFF_ON_WATCH,
  blind_spots: [SPOT_RECEIVER, SPOT_UI, SPOT_DEPLOYS],
};

const WATCHER_OFF_MONTHLY: SreHome = {
  ...WATCHER_OFF,
  headline: { text: "Nobody has watched Acme since 29 Oct, 03:00", tone: "unwatched", sub: "Your 5 services keep sending data. The watcher used its $25.00 budget for October; it starts again on 1 Nov." },
  watch: {
    ...OFF_WATCH, reason_words: "the watcher used its $25.00 budget for October; it starts again on 1 Nov", off_for_words: "2 days",
    missed_checks: 290, since: ago(2 * 1440),
    last_check: { at: ago(2 * 1440), said: "Traffic up 1.1%, status codes normal. Nothing to raise." },
    spent_words: "Spent $25.00 of $25 this month", resets_on: "1 Nov",
    told: { where: "#ops-alerts", at: "2026-10-29T03:00:00Z" },
  },
  services: OFF_SERVICES("29 Oct"),
  on_watch: [
    worker("Watcher", "watcher", "stopped", "The watcher used its $25.00 budget for October; it starts again on 1 Nov", "Haiku, Spent $25.00 of $25 this month"),
    worker("Investigator", "investigator", "waiting", "Looks into what the watcher raises", "Sonnet, Spent $0.00 of $10 this month"),
  ],
};

// ── an incident ──────────────────────────────────────────────────────────────
function chartAround(openedAt: string, deploys: { minutesBefore: number; label: string }[]): HomeChart {
  const opened = new Date(openedAt).getTime();
  const points: [string, number][] = [];
  for (let i = -30; i <= 30; i++) {
    const t = opened + i * 2 * MIN;
    const after = Math.max(0, i);
    const n = t > Date.now() ? 0 : i < 0 ? 1 + ((i * i) % 3) : 3 + after * 2 + (i % 4);
    points.push([new Date(t).toISOString(), n]);
  }
  return {
    label: "Errors, 2 hours around when it opened", points,
    deploys: deploys.map((d) => ({ at: new Date(opened - d.minutesBefore * MIN).toISOString(), label: d.label })),
  };
}

function incident(id: string, svc: string, what: string, minutes: number, over: Partial<HomeIncident> = {}): HomeIncident {
  const opened = ago(minutes);
  const age = minutes < 60 ? `${minutes} minutes` : `${Math.floor(minutes / 60)} hour${minutes >= 120 ? "s" : ""}`;
  return {
    id, service: svc, state: "open", opened_at: opened, title: `${svc}: ${what} since ${hm(opened)}`,
    age_words: `open ${age}`, who: null, who_words: "nobody has picked it up",
    failing: `1 in 12 requests through ${svc} returns 502. Before ${hm(opened)} it was 1 in 50.`,
    likely_cause: `service-a restarted at ${hm(ago(minutes + 4))} after a push to main. Logs show "upstream connect error" from ${hm(ago(minutes + 1))}.`,
    next_step: `Roll back the ${hm(ago(minutes + 4))} deploy of service-a, or check why it restarts.`,
    chart: chartAround(opened, [{ minutesBefore: 4, label: `${REPO} pushed to main at ${hm(ago(minutes + 4))}` }]),
    posted: { where: "#ops-alerts", at: ago(minutes - 7) }, based_on: "logs and the latest deploy", ...over,
  };
}

const ONE_INCIDENT: SreHome = {
  ...BASE,
  headline: { text: "1 incident open on edge-proxy", tone: "broken", sub: "2 of the other 4 services look normal. Checked every 10 minutes, last 3 minutes ago." },
  incidents: [incident("inc-1", "edge-proxy", "errors up 4x", 38)],
  services: [edgeProxy({ state: "incident", state_words: "Incident open" }), serviceA({ last_words: "checked 3 minutes ago" }), docsSite(), serviceUi(), receiver()],
  raised: [raised("chk-b1", hm(ago(31)), "edge-proxy", `errors up 4x since ${hm(ago(38))}`, "Investigated, note posted to Slack. Still open."), ...OLD_RAISED],
  raised_note: null,
  today: { checks: 86, quiet: 83, investigated: 1, spent_usd: 0.57 },
  on_watch: [ON_WATCH[0], worker("Investigator", "investigator", "running", "Looks into what the watcher raises", "Sonnet, Spent $0.36 of $10 this month")],
};

const MANY_INCIDENTS: SreHome = (() => {
  const a = incident("inc-1", "edge-proxy", "errors up 4x", 38);
  const b = incident("inc-2", "service-a", "slow answers", 66, {
    state: "on_it", who: { name: "Alex" }, who_words: "Alex is on it",
    failing: `Half of the calls to service-a take longer than 5 seconds. Before ${hm(ago(66))} it was 1 in 40.`,
    likely_cause: "Two deploys within 10 minutes. The second one may have undone a cache setting.",
    next_step: "Check the cache setting from the second deploy.",
    chart: chartAround(ago(66), [
      { minutesBefore: 15, label: `${REPO} pushed to main at ${hm(ago(81))}` },
      { minutesBefore: 8, label: `${REPO} merged a fix at ${hm(ago(74))}` },
    ]),
    based_on: "traces, logs and the latest deploy",
  });
  const c = incident("inc-3", "docs-site", "errors up 3x", 16, {
    failing: `1 in 8 page requests to the docs site returns 404. Before ${hm(ago(16))} it was 1 in 60.`,
    likely_cause: "A docs deploy removed pages that other pages still link to.",
    next_step: "Restore the removed pages, or fix the links.",
    chart: chartAround(ago(16), [{ minutesBefore: 5, label: "example-org/docs-site pushed to main" }]),
  });
  return {
    ...BASE,
    headline: { text: "5 incidents open", tone: "broken", sub: "The other 2 services look normal. Checked every 10 minutes, last 3 minutes ago." },
    incidents: [a, b, c], incidents_more: 2,
    services: [
      edgeProxy({ state: "incident", state_words: "Incident open" }),
      serviceA({ state: "incident", state_words: "Incident, Alex is on it" }),
      docsSite({ state: "incident", state_words: "Incident open" }),
      service({ name: "billing-api", state: "incident", state_words: "Incident open" }),
      service({ name: "auth", state: "incident", state_words: "Incident open" }),
      service({ name: "cron-runner", state: "normal", state_words: "Normal" }),
      service({ name: "backup-job", state: "normal", state_words: "Normal" }),
    ],
    raised: [
      raised("chk-c1", hm(ago(16)), "docs-site", `errors up 3x since ${hm(ago(16))}`, "Investigated, note posted to Slack. Still open."),
      raised("chk-c2", hm(ago(38)), "edge-proxy", `errors up 4x since ${hm(ago(38))}`, "Investigated, note posted to Slack. Still open."),
      raised("chk-c3", hm(ago(66)), "service-a", `slow answers since ${hm(ago(66))}`, "Investigated, note posted to Slack. Alex is on it."),
      ...OLD_RAISED,
    ].slice(0, 5),
    raised_note: null, today: { checks: 120, quiet: 114, investigated: 3, spent_usd: 1.12 },
    on_watch: [ON_WATCH[0], worker("Investigator", "investigator", "running", "Looks into what the watcher raises", "Sonnet, Spent $1.46 of $10 this month")],
    blind_spots: [SPOT_DEPLOYS],
  };
})();

// ── a new cell, a silent service, long text, many items ─────────────────────
const NEW_CELL: SreHome = {
  ...BASE,
  headline: { text: "No services yet", tone: "new", sub: "Connect where your logs, metrics or alerts come from." },
  watch: { ...WATCH_ON, on: false, reason: "no_agent", reason_words: "no watcher is set up", every_words: null, last_check: null, spent_words: null, cost_per_check_usd: null },
  services: [], services_from_words: "", raised: [], raised_note: null,
  today: { checks: 0, quiet: 0, investigated: 0, spent_usd: 0 }, on_watch: [], notes_go_to: null, blind_spots: [],
  tools: TOOLS.map((t) => ({ ...t, ok: false })),
  start: startBlock([]),
};

const SILENT: SreHome = {
  ...BASE,
  headline: { text: "All quiet", tone: "quiet", sub: "2 services watched, last check 3 minutes ago. docs-site has been silent for 1 day. 1 more service is silent too." },
  services: [
    edgeProxy(), serviceA(),
    docsSite({ state: "silent", state_words: "Sent nothing for 1 day", last_words: "checked 3 minutes ago" }),
    serviceUi(), receiver({ state_words: "Sent nothing for 6 days" }),
  ],
  today: { checks: 62, quiet: 62, investigated: 0, spent_usd: 0.31 },
  blind_spots: [
    { service: "docs-site", kind: "silent", title: "docs-site has been silent for 1 day",
      detail: "The docs-feed stopped sending yesterday. It may be fine, or nobody would know.", action: { label: "Check its connection", to: "/settings#tools" } },
    SPOT_RECEIVER, SPOT_UI, SPOT_DEPLOYS,
  ],
};

const LONG_NAME = "service-a-receiver-eu-central-1-canary-deployment-blue-green-1";
const LONG_CAUSE =
  "The 13:58 deploy of the receiver changed the batch size from 200 to 5000 and removed the retry limit, so every slow downstream " +
  "call now holds a worker for up to 90 seconds. Queues grew from 14:01, workers ran out by 14:09 and new requests started to fail " +
  "with timeouts. The same pattern showed on 12 Aug after a similar change.";

const LONG_TEXT: SreHome = (() => {
  const i = incident("inc-1", LONG_NAME, "errors up 4x", 80, {
    failing: "1 in 12 requests to the receiver returns 500 and the failing calls come from three different upstream clients, all from the same region and all since 14:02.",
    likely_cause: LONG_CAUSE,
    next_step: "Roll back the 13:58 deploy, or set the batch size back to 200 and put the retry limit back at 3. Then watch the queue for 30 minutes.",
    chart: chartAround(ago(80), [{ minutesBefore: 4, label: `example-org/${LONG_NAME} pushed to main` }]),
    based_on: "logs, metrics and the latest deploy",
  });
  return {
    ...BASE,
    headline: { text: `1 incident open on ${LONG_NAME}`, tone: "broken", sub: "The other 4 services look normal. Checked every 10 minutes, last 3 minutes ago." },
    incidents: [i],
    services: [service({ name: LONG_NAME, state: "incident", state_words: "Incident open", about: "Takes every request that reaches the receiver in the Frankfurt region and passes it on." }), serviceA(), docsSite(), serviceUi(), receiver()],
    raised: [raised("chk-d1", hm(ago(80)), LONG_NAME, "errors up 4x since 14:02 on the Frankfurt canary", "Investigated, note posted to Slack. Still open."), OLD_RAISED[0]],
    raised_note: null,
    blind_spots: [
      { service: LONG_NAME, kind: "no_deploys", title: `${LONG_NAME} has no deploy history`,
        detail: "The investigator cannot tell which change broke it unless repos are mapped to services in Settings.", action: { label: "Map repos to services", to: "/settings#tools" } },
      SPOT_RECEIVER,
    ],
  };
})();

const MANY_ITEMS: SreHome = (() => {
  const silentNames = ["cron-runner", "backup-job", "worker-01", "worker-02", "worker-03"];
  const normal = Array.from({ length: 30 }, (_, k) => `svc-${String(k + 1).padStart(2, "0")}`);
  const services = [
    edgeProxy(), serviceA(), docsSite(), serviceUi(), receiver({ state_words: "Sent nothing for 2 days" }),
    ...silentNames.map((n) => service({ name: n, state: "silent", state_words: "Sent nothing for 3 days", signals_words: "no signals yet", last_words: "checked 3 minutes ago", spark: [] })),
    ...normal.map((n, k) => service({ name: n, state: "normal", state_words: "Normal", spark: wave(k, 3) })),
  ];
  const silentSpots: HomeBlindSpot[] = ["service-a-receiver", ...silentNames].map((n) => ({
    service: n, kind: "silent", title: `${n} is silent`,
    detail: "Nothing has arrived from it lately. If it breaks, the investigator has nothing to read.",
    action: { label: "Connect its logs", to: "/settings#tools" },
  }));
  const noDeploys: HomeBlindSpot[] = normal.slice(0, 4).map((n) => ({
    service: n, kind: "no_deploys", title: `${n} has no deploy history`,
    detail: "The investigator cannot tell which change broke it unless a repo is mapped to it.",
    action: { label: "Map repos to services", to: "/settings#tools" },
  }));
  return {
    ...BASE,
    headline: { text: "All quiet", tone: "quiet", sub: "39 services watched, last check 2 minutes ago. service-a-receiver is silent. 5 more services are silent too." },
    services, services_from_words: "from the docs-feed, kafka-bridge and prod-otlp feeds",
    raised: [
      ...OLD_RAISED,
      raised("chk-e1", "21 Aug", "billing-api", "errors up 3x for 20 minutes", "Looked normal on the next check. Not investigated."),
      raised("chk-e2", "19 Aug", "auth", "login latency doubled for 10 minutes", "Looked normal on the next check. Not investigated."),
    ],
    today: { checks: 212, quiet: 211, investigated: 1, spent_usd: 1.04 },
    blind_spots: [...silentSpots, SPOT_UI, SPOT_DEPLOYS, ...noDeploys],
  };
})();

// Connected, watching is still off: three of five
const START_PARTIAL: SreHome = { ...BASE, start: startBlock(["model", "signals", "services"]) };
// The demo service is running and the watcher is not: signals and services done, the test incident open
const START_DEMO: SreHome = { ...BASE, start: startBlock(["model", "signals", "services"], true, true) };

const BODIES: Record<Exclude<HomeSample, "load" | "error">, SreHome> = {
  "watcher-off": WATCHER_OFF, "watcher-off-monthly": WATCHER_OFF_MONTHLY, "all-quiet": BASE,
  "one-incident": ONE_INCIDENT, "many-incidents": MANY_INCIDENTS, "new-cell": NEW_CELL, silent: SILENT,
  "long-text": LONG_TEXT, "many-items": MANY_ITEMS, "start-partial": START_PARTIAL, "start-demo": START_DEMO,
};

export function sampleHome(name: HomeSample): Promise<SreHome> {
  if (name === "load") return FOREVER;
  if (name === "error") return Promise.reject(new Error("the cell did not answer"));
  return Promise.resolve(structuredClone(BODIES[name]));
}

// ── what the page's actions do on a sample ───────────────────────────────────
/** The raise_budget action: the watcher is back on, so the page is the quiet one. */
export function sampleRaise(): SreHome {
  return structuredClone(BASE);
}
