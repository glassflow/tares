// Sample data for the Services list and the service page, shown at /services?sample=<name> and
// /services/<name>?sample=<name> when the browser has the sample flag (see ../../sample.ts). It is
// invented: edge-proxy, service-a, service-a-receiver, service-a-ui, docs-site, example-org/service-a, Acme.

import { ApiError } from "../../../api";
import type {
  SreIncident, SreRaised, SreService, SreServiceRow, SreServices, SreServiceState, SreWindow,
} from "../Services.api";

export const LIST_SAMPLES = ["normal", "empty", "error", "long", "many", "load", "hidden"] as const;
export const SERVICE_SAMPLES = [
  "normal", "incidents", "long", "silent", "quiet", "error", "load", "missing", "readonly", "savefail", "join",
] as const;

export const isListSample = (n: string | null): n is (typeof LIST_SAMPLES)[number] =>
  LIST_SAMPLES.some((s) => s === n);
export const isServiceSample = (n: string | null): n is (typeof SERVICE_SAMPLES)[number] =>
  SERVICE_SAMPLES.some((s) => s === n);

const FOREVER = new Promise<never>(() => {});
const REPO = "example-org/service-a";

// ── the list ─────────────────────────────────────────────────────────────────
const wave = (seed: number, scale: number, n = 24): number[] =>
  Array.from({ length: n }, (_, i) => Math.round(Math.max(0, Math.sin((i + seed) * 1.3) * scale + scale)));

interface Spec {
  name: string; state: SreServiceState; words: string; signals?: string; feeds?: string; events?: number;
  last?: string; spark?: number[]; about?: string | null; hidden?: boolean; watched?: boolean | null; watch?: string;
}

const row = (s: Spec): SreServiceRow => ({
  name: s.name, about: s.about ?? null, state: s.state, state_words: s.words,
  signals_words: s.signals ?? "Errors and traffic", feeds_words: s.feeds ?? "from prod-otlp",
  events_24h_words: s.events === undefined ? "No events in 24 hours" : `${s.events.toLocaleString("en")} events in 24 hours`,
  watch_words: s.watch ?? "Watched every 10 minutes", last_words: s.last ?? "checked 4 minutes ago",
  spark: s.spark ?? wave(s.name.length, 2), watched: s.watched === undefined ? true : s.watched, hidden: s.hidden ?? false,
});

const NORMAL: SreServiceRow[] = [
  row({ name: "service-a", state: "incident", words: "Incident open for 2 hours", events: 18420, spark: [0, 0, 0, 1, 0, 0, 2, 1, 0, 0, 3, 2, 1, 4, 5, 3, 6, 8, 9, 12, 14, 15, 18, 22] }),
  row({ name: "service-a-ui", state: "not_watched", words: "Not watched since 3 days ago", signals: "Traffic only", feeds: "from edge-proxy", events: 6210, watched: false, last: "not checked yet", watch: "Not watched, no check covers it" }),
  row({ name: "edge-proxy", state: "unusual", words: "More errors than usual", events: 2975, last: "checked 9 minutes ago", spark: [1, 0, 1, 2, 1, 0, 1, 1, 0, 1, 2, 1, 1, 0, 1, 1, 2, 1, 1, 0, 1, 7, 9, 11] }),
  row({ name: "service-a-receiver", state: "silent", words: "Sent nothing for 6 days", feeds: "from prod-otlp", events: undefined, spark: [3, 4, 2, 5, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] }),
  row({ name: "docs-site", state: "no_own_signal", words: "Seen only through edge-proxy", signals: "Deploys only", feeds: "from docs-site", events: undefined, watched: null, last: "not checked, it has no events of its own", spark: [] }),
  row({ name: "extra-svc-01", state: "normal", words: "Normal", events: 31540 }),
  row({ name: "extra-svc-02", state: "normal", words: "Normal", events: 9870, last: "checked 5 minutes ago" }),
  row({ name: "extra-svc-03", state: "normal", words: "Normal", signals: "Logs", feeds: "from prod-otlp", events: 22180, last: "checked 7 minutes ago" }),
];

const HIDDEN: SreServiceRow[] = [
  row({ name: "service-b", state: "normal", words: "Normal", events: 3, hidden: true, last: "not checked, it is hidden", about: "Batch jobs" }),
];

const LONG_NAME = "service-with-an-extremely-long-name-that-goes-on-and-on-and-1234567890";
const LONG_ABOUT =
  "Takes every request that reaches the front door and passes it to the right service, retries the ones that fail, " +
  "and keeps a record of each one for 30 days so that a person can find it again when a customer writes in.";

export function sampleServices(name: string, hidden: boolean): Promise<SreServices> {
  switch (name) {
    case "load": return FOREVER;
    case "error": return Promise.reject(new Error("the cell did not answer"));
    case "empty": return Promise.resolve({ services: [], hidden_count: 0, merged_words: null });
    case "long": return Promise.resolve({
      services: [row({ name: LONG_NAME, about: LONG_ABOUT.slice(0, 70), state: "normal", words: "Normal", events: 412900 }), ...NORMAL.slice(0, 3)],
      hidden_count: 0, merged_words: null });
    case "many": return Promise.resolve({
      services: Array.from({ length: 60 }, (_, i) => row({
        name: `service-${String(i + 1).padStart(2, "0")}`, state: i === 0 ? "incident" : i % 9 === 0 ? "unusual" : "normal",
        words: i === 0 ? "Incident open for 1 hour" : i % 9 === 0 ? "More errors than usual" : "Normal", events: 1000 + i * 37, spark: wave(i, 3) })),
      hidden_count: 0, merged_words: null });
    default: return Promise.resolve({
      services: hidden ? [...NORMAL, ...HIDDEN] : NORMAL,
      hidden_count: HIDDEN.length,
      merged_words: name === "hidden" || name === "normal" ? "2 names are counted under another service" : null });
  }
}

// ── one service ──────────────────────────────────────────────────────────────
const SPAN: Record<SreWindow, { n: number; seconds: number }> = {
  "1h": { n: 60, seconds: 60 }, "24h": { n: 60, seconds: 1440 }, "7d": { n: 84, seconds: 7200 },
};

function chart(window: SreWindow, opts: { quiet?: boolean; deploys?: boolean } = {}): SreService["chart"] {
  const { n, seconds } = SPAN[window];
  const until = new Date();
  until.setSeconds(0, 0);
  const since = new Date(until.getTime() - n * seconds * 1000);
  const traffic = Array.from({ length: n }, (_, i) => opts.quiet ? 0 : Math.round(900 + Math.sin(i / 3) * 220 + (i % 7) * 12));
  const errors = traffic.map((t, i) => (i > n - 8 || i % 11 === 0 ? Math.round(t * 0.04) : Math.round(t * 0.004)));
  const at = (fromEnd: number) => new Date(until.getTime() - fromEnd * seconds * 1000).toISOString();
  const marks = opts.deploys === false ? [] : [
    { at: at(Math.round(n * 0.2)), label: `${REPO} pushed to main`, repo: REPO },
    { at: at(Math.round(n * 0.55)), label: "Merged fix flaky retry", repo: REPO },
  ];
  return { window, since: since.toISOString(), until: until.toISOString(), bucket_seconds: seconds, errors, traffic, deploys: marks };
}

const incident = (i: number, state: SreIncident["state"]): SreIncident => ({
  id: `inc-${i}`, title: `service-a returns errors (${i})`, state,
  state_words: state === "resolved" ? "Resolved by itself" : state === "on_it" ? "Alex is on it" : "Open, needs someone",
  opened_words: `Opened ${i + 1} ${i === 0 ? "hour" : "hours"} ago`,
});

const raised = (n: number): SreRaised[] => Array.from({ length: n }, (_, i) => ({
  id: `rs-${i}`, when_words: `${i + 2} minutes ago`, what: `service-a returns errors on the jobs route (${i + 1})`,
  outcome_words: i % 3 === 0 ? "Handed to the investigator" : "Written down", worth: ["Worth waking someone", "Worth a look", "For the record"][i % 3],
}));

function base(over: Partial<SreService> = {}, window: SreWindow = "24h"): SreService {
  return {
    name: "service-a", about: null, state: "incident", state_words: "Incident open for 2 hours", hidden: false, watched: true,
    watch_words: "Watched every 10 minutes", signals_words: "Errors and traffic", feeds_words: "from prod-otlp",
    last_words: "checked 4 minutes ago", events_24h_words: "18,420 events in 24 hours",
    chart: chart(window),
    incidents: [incident(0, "on_it"), incident(1, "resolved")], incidents_total: 2, incidents_open: 1,
    raised: raised(3), raised_total: 3,
    blind_spots_words: "1 blind spot found",
    blind_spots: [{ kind: "no_deploys", title: "Deploys are not linked", detail: "service-a has no repo linked, so its chart shows no deploys.",
      action: { label: "Link a repo", to: "/settings#tools" } }],
    repos: [{ name: REPO, linked_by: "Matched by name", linked_words: "Last event 12 minutes ago" }],
    settings: { display_name: "", hidden: false, merged_into: null, merged_names: ["service-a-worker"], merge_options: ["edge-proxy", "service-a-ui"],
      repo_options: [REPO, "example-org/docs-site", "example-org/edge-proxy"], updated_by: null },
    ...over,
  };
}

export function sampleService(name: string, window: SreWindow): Promise<SreService> {
  switch (name) {
    case "load": return FOREVER;
    case "error": return Promise.reject(new Error("the cell did not answer"));
    case "missing": return Promise.reject(new ApiError("No service called service-z", 404));
    case "incidents": return Promise.resolve(base({
      incidents: Array.from({ length: 10 }, (_, i) => incident(i, i < 4 ? "open" : "resolved")),
      incidents_total: 15, incidents_open: 4, raised: raised(8), raised_total: 14,
    }, window));
    case "long": return Promise.resolve(base({
      name: LONG_NAME, about: LONG_ABOUT.slice(0, 70), state: "normal", state_words: "Normal",
      incidents: [], incidents_total: 0, incidents_open: 0, raised: [], raised_total: 0, blind_spots: [], blind_spots_words: "No blind spots found",
      repos: [{ name: "example-org/a-repository-with-a-very-long-name-that-goes-on-and-on-and-on", linked_by: "Linked in settings", linked_words: "Last event 2 days ago" }],
      settings: { ...base().settings, display_name: LONG_ABOUT.slice(0, 70) },
    }, window));
    case "silent": return Promise.resolve(base({
      name: "service-a-receiver", state: "silent", state_words: "Sent nothing for 6 days", events_24h_words: "No events in 24 hours",
      incidents: [], incidents_total: 0, incidents_open: 0, raised: [], raised_total: 0,
      chart: chart(window, { quiet: window !== "7d", deploys: false }), repos: [], blind_spots_words: "1 blind spot found",
      blind_spots: [{ kind: "silent", title: "It has gone quiet", detail: "service-a-receiver sent 50 events 3 days ago and none since.",
        action: { label: "Check where it sends", to: "/settings#tools" } }],
    }, window));
    case "quiet": return Promise.resolve(base({
      name: "service-a-ui", state: "normal", state_words: "Normal", events_24h_words: "No events in 24 hours",
      incidents: [], incidents_total: 0, incidents_open: 0, raised: [], raised_total: 0, repos: [],
      chart: chart(window, { quiet: true, deploys: false }), blind_spots: [], blind_spots_words: "Not checked yet",
    }, window));
    default: return Promise.resolve(base({}, window));
  }
}

/** What a save does on a sample: `savefail` and `readonly` answer as the cell would, the rest apply the change to a copy. */
export function sampleSave(name: string, current: SreService, body: Record<string, unknown>): Promise<SreService> {
  if (name === "savefail") return Promise.reject(new ApiError("The cell could not save the settings.", 500));
  if (name === "readonly") return Promise.reject(new ApiError("This key can read but not change settings.", 403));
  if (body.merged_into) {
    const into = String(body.merged_into);
    return Promise.resolve(base({ name: into, about: null, state: "normal", state_words: "Normal", incidents: [], incidents_total: 0, incidents_open: 0, raised: [], raised_total: 0 }));
  }
  const s = current.settings;
  const next = {
    ...s,
    ...("display_name" in body ? { display_name: String(body.display_name ?? "") } : {}),
    ...("hidden" in body ? { hidden: Boolean(body.hidden) } : {}),
  };
  const repos = Array.isArray(body.repos) ? (body.repos as string[]) : null;
  return Promise.resolve({
    ...current, hidden: next.hidden, about: next.display_name || null, settings: next,
    ...(repos ? { repos: repos.map((r) => ({ name: r, linked_by: "Linked in settings", linked_words: "Nothing received yet" })) } : {}),
  });
}
