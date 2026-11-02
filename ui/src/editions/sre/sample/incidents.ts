import { ApiError } from "../../../api";
import { ageWords, clock, DAY, spanWords, sinceWords } from "../incidentTime";
import type {
  IncidentAction, IncidentData, IncidentGroup, IncidentItem, IncidentsData, IncidentsQuery, IncidentState, WorthAction,
} from "../Incidents.api";

// Sample data for the Incidents screens, shaped exactly like the API answers and only for
// `?sample=<name>` (see useSample). Every name is invented. The clock is fixed, so a state looks the
// same on every day: SAMPLE_NOW is a Monday, 14:49 UTC. The words the cell writes (state_words,
// when_words, the resolved words) are worked out here the same way tares/goal.py works them out, so
// a sample never says "opened today" for an incident that was resolved two days ago.

export const SAMPLE_NOW = Date.parse("2026-11-02T14:49:00Z");
/** The person looking at a sample: the incident that is "yours" is Lee's. */
export const SAMPLE_ME = "Lee";

const MIN = 60_000;
const HOUR = 60 * MIN;
const iso = (ms: number) => new Date(ms).toISOString();
const since = (at: number) => sinceWords(at, SAMPLE_NOW);
const age = (ms: number) => ageWords(ms);

type How = "person" | "itself";
interface Spec {
  id: string;
  service: string;
  what: string;
  opened: number;
  state: IncidentState;
  /** on_it: the person and when they took it. */
  owner?: { name: string; at: number };
  /** resolved: when, how and (for a person) who. */
  resolved?: { at: number; how: How; by?: string };
}

const titleOf = (s: Spec) => `${s.service}: ${s.what}`;

function stateWords(s: Spec) {
  if (s.state === "on_it" && s.owner) return `${s.owner.name} is on it since ${since(s.owner.at)}`;
  if (s.state === "resolved" && s.resolved) {
    if (s.resolved.how === "itself") return `resolved itself after ${spanWords(s.resolved.at - s.opened)}`;
    const ago = age(SAMPLE_NOW - s.resolved.at);
    return s.resolved.by ? `resolved by ${s.resolved.by}, ${ago}` : `resolved, ${ago}`;
  }
  return `open ${spanWords(SAMPLE_NOW - s.opened)}, nobody on it`;
}

function itemOf(s: Spec): IncidentItem {
  return {
    id: s.id, incident: true, title: titleOf(s), service: s.service, state: s.state,
    state_words: stateWords(s), opened_at: iso(s.opened), when_words: ageWords(SAMPLE_NOW - s.opened),
    who: s.state === "on_it" ? s.owner?.name ?? null : s.state === "resolved" && s.resolved?.how === "person" ? s.resolved.by ?? null : null,
  };
}

// ── the list ─────────────────────────────────────────────────────────────────
const at = (day: number, h: number, m: number) => Date.UTC(2026, 9, day, h, m); // October (day 29..31)
const today = (h: number, m: number) => Date.UTC(2026, 10, 2, h, m);

const OPEN: Spec[] = [
  { id: "inc_s1", service: "edge-proxy", what: "errors rose after the 14:02 deploy", opened: today(14, 11), state: "open" },
  { id: "inc_s2", service: "billing-worker", what: "queue is not draining since 14:35", opened: today(14, 37), state: "open" },
];
const ON_IT: Spec[] = [
  { id: "inc_s3", service: "service-a", what: "timeouts since 09:40", opened: today(9, 52), state: "on_it", owner: { name: "Alex", at: today(14, 20) } },
];
const RESOLVED: Spec[] = [
  { id: "inc_s4", service: "docs-site", what: "slow answers since the config change", opened: at(31, 12, 40), state: "resolved", resolved: { at: at(31, 13, 44), how: "person", by: "Kim" } },
  { id: "inc_s5", service: "api-server", what: "restarts every few minutes since 11:15", opened: at(30, 11, 15), state: "resolved", resolved: { at: at(30, 11, 27), how: "itself" } },
  { id: "inc_s6", service: "ingress-nginx", what: "disk filling up, 91% full", opened: at(29, 8, 30), state: "resolved", resolved: { at: at(29, 9, 40), how: "person", by: "Lee" } },
];

const SERVICES = ["api-server", "billing-worker", "docs-site", "edge-proxy", "ingress-nginx", "service-a"];
const PEOPLE = ["Alex", "Kim", "Lee"];
const WHATS = [
  "errors rose after the 14:02 deploy", "timeouts since 09:40", "restarts every few minutes since 11:15",
  "disk filling up, 91% full", "slow answers since the config change", "queue is not draining since 16:05",
];
/** 80 resolved incidents, newest first, each opened a little earlier than the one before. */
const MANY: Spec[] = Array.from({ length: 80 }, (_, i) => {
  const opened = SAMPLE_NOW - 3 * DAY - i * 7 * HOUR - (i % 5) * 11 * MIN;
  const took = (10 + (i % 7) * 9) * MIN;
  const byPerson = i % 4 !== 0;
  return {
    id: `inc_m${String(i).padStart(2, "0")}`, service: SERVICES[i % 6], what: WHATS[(i * 5) % 6], opened, state: "resolved" as const,
    resolved: byPerson ? { at: opened + took, how: "person" as const, by: PEOPLE[i % 3] } : { at: opened + took, how: "itself" as const },
  };
});

const LONG_WHAT = "the nightly invoice export fails with \"upstream connect error or disconnect/reset before headers. reset reason: connection termination\" for every tenant since the 02:00 batch started and keeps retrying";
const LONG_NAME = "Kim Alexandra Montgomery-Featherstonehaugh-Richardson";
const LONG_OPEN: Spec = { id: "inc_s7", service: "billing-worker", what: LONG_WHAT, opened: today(2, 7), state: "open" };
const LONG_ON_IT: Spec = { id: "inc_s8", service: "service-a", what: "timeouts since 09:40", opened: at(31, 23, 12), state: "on_it", owner: { name: LONG_NAME, at: at(31, 23, 40) } };

const RAISED: IncidentItem[] = [
  { id: "run_r1", incident: false, title: "docs-site: one slow page", service: "docs-site", state: "raised", state_words: "raised, not an incident", opened_at: iso(at(31, 10, 2)), when_words: ageWords(SAMPLE_NOW - at(31, 10, 2)), who: null },
  { id: "run_r2", incident: false, title: "service-a: a single 500 error", service: "service-a", state: "raised", state_words: "raised, not an incident", opened_at: iso(at(31, 8, 40)), when_words: ageWords(SAMPLE_NOW - at(31, 8, 40)), who: null },
];

interface ListSpec { open: Spec[]; on_it: Spec[]; resolved: Spec[] }
const LISTS: Record<string, ListSpec> = {
  "list-normal": { open: OPEN, on_it: ON_IT, resolved: RESOLVED },
  "list-empty": { open: [], on_it: [], resolved: [] },
  "list-long": { open: [LONG_OPEN, ...OPEN], on_it: [LONG_ON_IT], resolved: RESOLVED },
  "list-many": { open: OPEN, on_it: ON_IT, resolved: MANY },
};

const PAGE = 20;
const QUIET = "1,204 quiet checks today are not shown";

function listFor(name: string, q: IncidentsQuery): IncidentsData {
  const l = LISTS[name] ?? LISTS["list-normal"];
  const keep = (s: Spec) => !q.service || s.service === q.service;
  const open = l.open.filter(keep);
  const onIt = l.on_it.filter(keep);
  let resolved = l.resolved.filter(keep);
  const total = resolved.length;
  if (q.before) {
    const cut = Date.parse(q.before);
    resolved = resolved.filter((s) => s.opened < cut);
  }
  resolved = resolved.slice(0, PAGE);
  const groups: IncidentGroup[] = [];
  if (!q.before) {
    groups.push({ state: "open", label: "Open", items: open.map(itemOf) });
    groups.push({ state: "on_it", label: "Someone on it", items: onIt.map(itemOf) });
  }
  groups.push({ state: "resolved", label: "Resolved", items: resolved.map(itemOf) });
  if (q.show === "all" && !q.before) groups.push({ state: "raised", label: "Raised, not an incident", items: q.service ? RAISED.filter((r) => r.service === q.service) : RAISED });
  const every = [...l.open, ...l.on_it, ...l.resolved];
  return {
    groups,
    counts: { open: open.length, on_it: onIt.length, resolved: total },
    services: [...new Set(every.map((s) => s.service))].sort(),
    next_before: resolved.length === PAGE ? iso(resolved[resolved.length - 1].opened) : null,
    quiet_checks_words: every.length === 0 ? null : QUIET,
  };
}

// ── one incident ─────────────────────────────────────────────────────────────
interface Flavour {
  spec: Spec;
  slack?: boolean;
  evidence?: number;
  joined?: boolean;
  long?: boolean;
  worth?: { answer: boolean | null; by: string | null };
}

const SLACK = { where: "#alerts", url: "https://example.org/slack/alerts/thread" };

function evidenceFor(deploy: number, n: number): { at: string; text: string }[] {
  const e: [number, string][] = [
    [0, "A deploy of edge-proxy finished"],
    [3, "Errors rose from 2 a minute to 310 a minute"],
    [4, "502 answers on /checkout, 48 in one minute"],
    [5, "Two backends in the pool did not answer"],
    [7, "Traffic stayed the same, so this is not a surge"],
    [10, "Memory and CPU are normal"],
    [13, "The last deploy before this one was 6 days ago"],
    [16, "The same error showed once on 12 Aug after a deploy"],
    [18, "A second check from outside also failed"],
    [22, "The pool file still lists the two old machines"],
  ];
  return e.slice(0, n).map(([m, text]) => ({ at: iso(deploy + m * MIN), text }));
}

function chartFor(s: Spec) {
  const end = s.state === "resolved" && s.resolved ? Math.min(s.resolved.at + 15 * MIN, SAMPLE_NOW) : SAMPLE_NOW;
  const start = s.opened - 66 * MIN;
  const step = Math.max(5 * MIN, Math.ceil((end - start) / 36 / MIN) * MIN);
  const rise = s.opened - 6 * MIN;
  const stop = s.state === "resolved" && s.resolved ? s.resolved.at : Infinity;
  const down = [40, 10, 4, 3];
  const high = [90, 260, 310, 295, 280, 300, 270];
  const low = [2, 2, 3, 2, 4];
  const points: [string, number][] = [];
  const traffic: [string, number][] = [];
  let ups = 0;
  let downs = 0;
  for (let t = start, i = 0; t <= end; t += step, i++) {
    let v: number;
    if (t < rise) v = low[i % low.length];
    else if (t < stop) v = high[Math.min(ups++, 40) % high.length];
    else v = down[Math.min(downs++, down.length - 1)];
    points.push([iso(t), v]);
    traffic.push([iso(t), 80 + ((i * 3) % 9)]);
  }
  return { points, traffic };
}

const BIG_PARA = "The errors come in two waves. The first wave lines up with the deploy and affects only requests that land on the two machines that were shut down last week but still sit in the pool file. The second wave is retries from clients that gave up on the first answer and asked again, which doubles the load on the machines that still work. ";

function incidentFor(f: Flavour): IncidentData {
  const s = f.spec;
  const deploy = s.opened - 9 * MIN;
  const failing = `Requests to edge-proxy return 502 for about 1 in 6 visitors. It started at ${clock(s.opened - 6 * MIN)}, three minutes after the ${clock(deploy)} deploy.`;
  const likely = "The new release sends traffic to a backend pool that still lists two machines that were shut down. Calls to those two fail, the rest work.";
  const next = `Take the two old machines out of the pool, or roll back the ${clock(deploy)} deploy. Rolling back is the quicker one.`;
  const parts = `## What is failing\n\n${failing}\n\n## Likely cause\n\n${likely}\n\n## Next step\n\n${next}`;
  const extra = Array.from({ length: 9 }, (_, i) => `## What the watcher also looked at (${i + 1})\n\n${BIG_PARA.repeat(3)}`).join("\n\n");
  const itself = s.resolved?.how === "itself";
  const resolved = s.state === "resolved" && s.resolved
    ? {
        by: itself ? null : s.resolved.by ?? null,
        at: iso(s.resolved.at),
        words: itself ? `Resolved itself after ${spanWords(s.resolved.at - s.opened)}` : s.resolved.by ? `Resolved by ${s.resolved.by}` : "Resolved",
      }
    : null;
  return {
    id: s.id, title: titleOf(s), service: s.service, state: s.state, state_words: stateWords(s), opened_at: iso(s.opened),
    who: s.state === "on_it" && s.owner ? { name: s.owner.name, since: iso(s.owner.at) } : null,
    resolved,
    failing, likely_cause: likely, next_step: next,
    note: f.long ? `${parts}\n\n${extra}` : parts,
    evidence: evidenceFor(deploy, f.evidence ?? 10),
    chart: {
      label: `Errors from ${s.service}`, ...chartFor(s),
      deploys: [{ at: iso(deploy), label: `A deploy of ${s.service} finished` }],
    },
    slack: f.slack === false ? null : SLACK,
    joined: f.joined
      ? [{ at: iso(s.opened + 19 * MIN), title: "edge-proxy: errors still high" }, { at: iso(s.opened + 33 * MIN), title: "edge-proxy: 502 on /checkout" }]
      : [],
    worth: { answer: f.worth?.answer ?? null, by: f.worth?.by ?? null, can_answer: true },
    actions: s.state === "open" ? ["on_it", "resolve"] : s.state === "on_it" ? ["resolve", "reopen"] : ["reopen"],
  };
}

/** The incident of the page samples: edge-proxy, its errors rising after a deploy, named by the clock of the day it opened. */
const base = (opened: number) => ({ id: "inc_sample", service: "edge-proxy", what: `errors rose after the ${clock(opened - 9 * MIN)} deploy`, opened });
const OLD_OPENED = at(31, 12, 40);
const FLAVOURS: Record<string, Flavour> = {
  "inc-open": { spec: { ...base(today(14, 11)), state: "open" } },
  "inc-onit": { spec: { ...base(today(14, 11)), state: "on_it", owner: { name: "Alex", at: today(14, 20) } } },
  "inc-you": { spec: { ...base(today(14, 11)), state: "on_it", owner: { name: SAMPLE_ME, at: today(14, 22) } }, joined: true },
  "inc-resolved": {
    spec: { ...base(OLD_OPENED), state: "resolved", resolved: { at: OLD_OPENED + 64 * MIN, how: "person", by: "Alex" } },
    worth: { answer: false, by: SAMPLE_ME },
  },
  "inc-self": { spec: { ...base(OLD_OPENED), state: "resolved", resolved: { at: OLD_OPENED + 12 * MIN, how: "itself" } } },
  "inc-noslack": {
    spec: { ...base(OLD_OPENED), state: "resolved", resolved: { at: OLD_OPENED + 64 * MIN, how: "person", by: "Alex" } },
    slack: false, evidence: 3,
  },
  "inc-long": { spec: { ...base(today(14, 11)), state: "open" }, long: true },
  "inc-action-err": { spec: { ...base(today(14, 11)), state: "open" } },
};

// ── what a screen asks for ───────────────────────────────────────────────────
export const LIST_NAMES = ["list-loading", "list-error", ...Object.keys(LISTS)];
export const INCIDENT_NAMES = ["inc-loading", "inc-error", "inc-missing", ...Object.keys(FLAVOURS)];
export const isListSample = (n: string | null): n is string => !!n && LIST_NAMES.includes(n);
export const isIncidentSample = (n: string | null): n is string => !!n && INCIDENT_NAMES.includes(n);

const never = <T,>() => new Promise<T>(() => {});

export function sampleIncidents(name: string, q: IncidentsQuery = {}): Promise<IncidentsData> {
  if (name === "list-loading") return never();
  if (name === "list-error") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(listFor(name, q));
}

export function sampleIncident(name: string): Promise<IncidentData> {
  if (name === "inc-loading") return never();
  if (name === "inc-error") return Promise.reject(new Error("The cell did not answer."));
  if (name === "inc-missing") return Promise.reject(new ApiError("No incident with that id", 404));
  return Promise.resolve(incidentFor(FLAVOURS[name] ?? FLAVOURS["inc-open"]));
}

/** A change on a sample has no cell to make it: the answer is the incident as the cell would send it
 *  after the change. `inc-action-err` answers as a key that can read but not change. */
export function sampleAct(name: string, now: IncidentData, action: IncidentAction | WorthAction): Promise<IncidentData> {
  if (name === "inc-action-err") return Promise.reject(new ApiError("This key can read but not change incidents.", 403));
  const t = iso(SAMPLE_NOW);
  switch (action) {
    case "on_it":
      return Promise.resolve({ ...now, state: "on_it", who: { name: SAMPLE_ME, since: t }, resolved: null, state_words: `${SAMPLE_ME} is on it since ${clock(SAMPLE_NOW)}`, actions: ["resolve", "reopen"] });
    case "resolve":
      return Promise.resolve({ ...now, state: "resolved", who: null, resolved: { by: SAMPLE_ME, at: t, words: `Resolved by ${SAMPLE_ME}` }, state_words: `resolved by ${SAMPLE_ME}, just now`, actions: ["reopen"] });
    case "reopen":
      return Promise.resolve({ ...now, state: "open", who: null, resolved: null, state_words: "open, nobody on it", actions: ["on_it", "resolve"] });
    case "worth_yes":
      return Promise.resolve({ ...now, worth: { ...now.worth, answer: true, by: SAMPLE_ME } });
    case "worth_no":
      return Promise.resolve({ ...now, worth: { ...now.worth, answer: false, by: SAMPLE_ME } });
  }
}
