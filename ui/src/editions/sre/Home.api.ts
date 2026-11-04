// The body of the AI SRE home page (tares/sre_home.py), and the calls that change it:
//   GET  /api/home                          everything the page shows, in one read
//   POST /api/home/actions/{action}         raise_budget; answers { ok, home } with the new home
//   POST /api/incidents/{id}/on_it          the person picks an incident up
//   POST /api/incidents/{id}/resolve        the person marks it resolved
//   PUT  /api/findings/{run_id}/worth       { worth: boolean }, the answer to "Was this worth raising?"
//   POST /api/home/start/hide               hide the Start checklist for this credential (DELETE shows it again)
// The words (`headline`, `*_words`, `doing`, `detail`) are worded for people and shown as they are.
// Service names, titles, causes and blind-spot text come from the person's own data.

import { api, request } from "../../api";
import type { StartBlock } from "../StartChecklist.api";
import type { SreServiceState } from "./Services.api";

export type HomeTone = "broken" | "unwatched" | "quiet" | "new";

export interface HomeHeadline {
  text: string;
  tone: HomeTone;
  /** May be empty. */
  sub: string;
}

export type WatchReason = "budget_used" | "no_agent" | string;

export interface HomeWayOut {
  action: "raise_budget" | string;
  /** "Add $25 and restart". */
  label: string;
  amount_usd: number;
  primary: boolean;
}

export interface HomeWatch {
  on: boolean;
  reason: WatchReason | null;
  /** "the watcher used its $25.00 budget". Lower case, no full stop. Null while the watcher is on. */
  reason_words: string | null;
  since: string | null;
  /** "6 days". */
  off_for_words: string | null;
  missed_checks: number | null;
  /** "every 10 minutes". */
  every_words: string | null;
  last_check: { at: string; said: string } | null;
  cost_per_check_usd: number | null;
  /** "Spent $25.00 of $25", with " this month" when the budget renews. Null when there is no budget. */
  spent_words: string | null;
  /** "1 Nov": the day a monthly budget starts again. Null for a budget that does not renew. */
  resets_on: string | null;
  told: { where: string; at: string } | null;
  ways_out: HomeWayOut[];
}

export interface HomeChart {
  label: string;
  /** ISO time and errors, oldest first. */
  points: [string, number][];
  deploys: { at: string; label: string }[];
}

export interface HomeIncident {
  id: string;
  title: string;
  service: string;
  state: "open" | "on_it" | "resolved";
  opened_at: string;
  /** "open 38 minutes". */
  age_words: string;
  who: { name: string } | null;
  /** "Alex is on it" or "nobody has picked it up". */
  who_words: string;
  failing: string | null;
  likely_cause: string | null;
  next_step: string | null;
  chart: HomeChart | null;
  posted: { where: string; at: string } | null;
  /** "logs and the latest deploy". */
  based_on: string | null;
}

/** One line of the services list, as the Services screen gives it but without the feed and event words. */
export interface HomeService {
  name: string;
  about: string | null;
  state: SreServiceState;
  state_words: string;
  signals_words: string;
  /** "checked 3 minutes ago", "not checked yet", "not checked, it has no events of its own". */
  last_words: string;
  spark: number[];
}

export interface HomeRaised {
  /** The id of the check; the worth route takes it. */
  id: string;
  /** The incident the check's note was filed under, which the incident page takes; null when none. */
  incident_id: string | null;
  at: string;
  /** "14:09" for today, "29 Sep" for an earlier day. */
  when_words: string;
  service: string;
  what: string;
  outcome_words: string;
  /** The person's own answer: true, false, or null when they have not given one. */
  worth: boolean | null;
  can_answer: boolean;
}

export interface HomeToday {
  checks: number;
  quiet: number;
  investigated: number;
  spent_usd: number;
}

export interface HomeOnWatch {
  role: "watcher" | "investigator";
  name: string;
  state: "running" | "stopped" | "waiting" | string;
  /** "Checks rius-prod and glassflow-docs every 10 minutes". */
  doing: string;
  model_words: string;
  /** "Haiku, Spent $3.74 of $25 this month". */
  cost_words: string;
}

export interface HomeBlindSpot {
  service: string;
  kind: string;
  title: string;
  detail: string;
  action: { label: string; to: string };
}

export interface HomeTool {
  name: string;
  ok: boolean;
}

export interface SreHome {
  edition: "sre";
  /** The system's name, such as "Acme". */
  system: string;
  headline: HomeHeadline;
  watch: HomeWatch;
  /** At most three. */
  incidents: HomeIncident[];
  /** Open incidents beyond the ones above. */
  incidents_more: number;
  services: HomeService[];
  /** "from the glassflow-docs and rius-prod feeds". Empty when no service has a feed. */
  services_from_words: string;
  raised: HomeRaised[];
  /** A line under the list; null when the latest check was not quiet. */
  raised_note: string | null;
  today: HomeToday;
  on_watch: HomeOnWatch[];
  notes_go_to: { label: string } | null;
  blind_spots: HomeBlindSpot[];
  tools: HomeTool[];
  /** The Start checklist (tares/start_steps.py). */
  start: StartBlock;
}

export const DEMO_TEMPLATE = "ai_sre_demo";

/** Starts the demo service the way the Start list on the platform pages does: detect what it needs,
 *  then create it. Answers the path to open when the cell needs a step-by-step setup instead (a
 *  required value could not be found), and null once the demo is created. */
export async function startDemo(): Promise<string | null> {
  const { templates } = await api.templates();
  const template = templates.find((t) => t.key === DEMO_TEMPLATE);
  if (!template) throw new Error("This cell has no demo.");
  const found = await api.detectRecipe(template.key);
  const required = Object.entries(template.params).filter(([, p]) => p.required).map(([k]) => k);
  if (required.some((k) => found.params[k] === undefined || found.params[k] === "")) {
    return `/projects/new/${template.key}`;
  }
  await api.createProject({ template: template.key, params: found.params });
  return null;
}

export const sreHome = () => request<SreHome>("/api/home");

/** Runs one of the page's own actions and answers the new home. */
export async function homeAction(action: string, body: Record<string, unknown> = {}): Promise<SreHome> {
  const out = await request<{ ok: boolean; home: SreHome }>(`/api/home/actions/${encodeURIComponent(action)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return out.home;
}

export const pickUpIncident = (id: string) =>
  request<unknown>(`/api/incidents/${encodeURIComponent(id)}/on_it`, { method: "POST", body: JSON.stringify({}) });

export const resolveIncident = (id: string) =>
  request<unknown>(`/api/incidents/${encodeURIComponent(id)}/resolve`, { method: "POST", body: JSON.stringify({}) });

export const answerWorth = (runId: string, worth: boolean) =>
  request<unknown>(`/api/findings/${encodeURIComponent(runId)}/worth`, { method: "PUT", body: JSON.stringify({ worth }) });

// What the page shows at once, before the cell's next answer. The incident and worth routes do not
// clear the cell's short cache of the home, so an immediate read could still show the old page.

/** The home with an incident picked up or marked resolved. */
export function withIncident(home: SreHome, id: string, to: "on_it" | "resolved"): SreHome {
  const next = structuredClone(home);
  if (to === "resolved") {
    next.incidents = next.incidents.filter((i) => i.id !== id);
    // With the watcher off the cell words the page itself ("Nobody has watched ..."), so leave it to the reload.
    if (next.incidents.length === 0 && next.incidents_more === 0 && home.watch.on) {
      next.headline = { text: "All quiet", tone: "quiet", sub: "" };
      next.services = next.services.map((s) => (s.state === "incident" ? { ...s, state: "normal", state_words: "Normal" } : s));
    }
    return next;
  }
  next.incidents = next.incidents.map((i) => (i.id === id ? { ...i, state: "on_it", who: { name: "You" }, who_words: "You are on it" } : i));
  return next;
}

/** The home with the person's answer on one raised item. */
export function withWorth(home: SreHome, runId: string, worth: boolean): SreHome {
  return { ...home, raised: home.raised.map((r) => (r.id === runId ? { ...r, worth } : r)) };
}
