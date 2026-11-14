// One table from every status the console shows to the tone it reads in (TR-1265). A page looks its
// status up here and renders it with StatusLabel or StatusDot; it never picks a colour itself.
//
// Each table is `satisfies Record<TheApiUnion, Tone>`, so a status added to an API type later fails
// tsc until it has a tone here. The gallery (/dev/components) walks every table, in light and dark.
//
// The tones (.impeccable.md, States): working is live work and the only amber; waiting is a person's
// turn or a pause; ready is jade; done is plain; bad is rose; quiet is not started, stale or out of play.

import type { Tone } from "./StatusLabel";
import type { ProjectState } from "../../editions/factory/FactoryProjects.api";
import type { ProjectPageState, PrState, TicketState } from "../../editions/factory/FactoryProject.api";
import type { StateKey } from "../../editions/factory/FactoryTickets.api";
import type { TicketStateKey } from "../../editions/factory/FactoryTicket.api";
import type { MemberState } from "../../editions/factory/FactoryCrew.api";
import type { HomeCrewState } from "../../editions/factory/home.types";
import type { CiState } from "../../editions/factory/FactoryReviews.api";
import type { GroupState } from "../../editions/sre/Incidents.api";
import type { SreServiceState } from "../../editions/sre/Services.api";
import type { CrewState, ProjectSession, TicketStatus } from "../../types";

/** A source's health as tares/runtime.py SourceHealth.status sends it. The API types it as a string. */
export type SourceHealthState = "starting" | "ok" | "error" | "paused" | "push";
type SessionState = NonNullable<ProjectSession["state"]>;
/** A review round, a review finding and a crew call, as tares/screens/factory_ticket.py words them. A fixed
 *  finding is sent as a phrase ("fixed in round 3"), so the page keys it from its own open/fixed flag. */
export type ReviewVerdict = "passed" | "asked for changes" | "blocked";
export type FindingState = "open" | "fixed";
export type CallState = "waiting for you" | "confirmed" | "you changed it";
/** "changes asked" -> "changes_asked": several screens send states in words, and toneOf keys them this way. */
type Keyed<S extends string> = S extends `${infer A} ${infer B}` ? Keyed<`${A}_${B}`> : S;

const project = {
  building: "working",
  waiting_on_you: "waiting",
  not_started: "quiet",
  ready_to_close: "ready",
  later: "quiet",
  done: "done",
  finished: "done",
} satisfies Record<ProjectState | ProjectPageState, Tone>;

// A ticket's state, by key. The project page sends the same states in words ("in review"); `ticketTone`
// maps both. A merged ticket stays jade: the dot that tells a list "this one landed".
const ticket = {
  to_do: "quiet",
  in_progress: "working",
  in_review: "waiting",
  changes_asked: "waiting",
  blocked: "bad",
  merged: "ready",
  canceled: "quiet",
} satisfies Record<StateKey | TicketStateKey | Keyed<TicketState>, Tone>;

const pr = {
  open: "waiting",
  draft: "quiet",
  merged: "ready",
  closed: "quiet",
} satisfies Record<PrState, Tone>;

const ci = {
  ok: "ready",
  bad: "bad",
  go: "working",
  none: "quiet",
} satisfies Record<CiState, Tone>;

// A crew member (Crew page, Home) and a crew session (legacy project and crew pages). "Not heard" and
// a quiet session are stale, not broken: a dashed neutral line. Idle is a turn that ended with
// nobody needed, which is healthy: jade, as Crew and Home always showed it.
const crew = {
  working: "working",
  idle: "ready",
  waiting: "waiting",
  waiting_for_you: "waiting",
  not_heard: "quiet",
  quiet: "quiet",
  replaced: "done",
  ended: "done",
} satisfies Record<Keyed<MemberState | HomeCrewState> | CrewState | SessionState, Tone>;

// A review round, a finding in it, and a call the crew made that you can confirm or change.
const verdict = {
  passed: "ready",
  asked_for_changes: "waiting",
  blocked: "bad",
} satisfies Record<Keyed<ReviewVerdict>, Tone>;

const finding = {
  open: "waiting",
  fixed: "ready",
} satisfies Record<FindingState, Tone>;

const call = {
  waiting_for_you: "waiting",
  confirmed: "ready",
  you_changed_it: "done",
} satisfies Record<Keyed<CallState>, Tone>;

// An incident, or a finding that was raised and did not become one.
const incident = {
  open: "bad",
  on_it: "working",
  resolved: "ready",
  raised: "quiet",
} satisfies Record<GroupState, Tone>;

const service = {
  incident: "bad",
  unusual: "waiting",
  silent: "waiting",
  not_watched: "quiet",
  no_own_signal: "quiet",
  normal: "ready",
} satisfies Record<SreServiceState, Tone>;

// A push source has no poll loop, so its health is not measured: quiet, not jade.
const source = {
  starting: "quiet",
  ok: "ready",
  error: "bad",
  paused: "waiting",
  push: "quiet",
} satisfies Record<SourceHealthState, Tone>;

// The legacy project docs' ticket status.
const docTicket = {
  todo: "quiet",
  in_progress: "working",
  done: "done",
  canceled: "quiet",
} satisfies Record<TicketStatus, Tone>;

/** Every table, by domain. The gallery and its test walk this, so a new domain shows up there too. */
export const STATUS_TONES = { project, ticket, pr, ci, crew, verdict, finding, call, incident, service, source, docTicket } as const;
export type StatusDomain = keyof typeof STATUS_TONES;

/** The tone of a status the API sends as a plain string. An unknown one reads quiet and warns once,
 *  so a new backend state is visible in the console log instead of silently borrowing a colour. */
export function toneOf<D extends StatusDomain>(domain: D, status: string | null | undefined): Tone {
  const table = STATUS_TONES[domain] as Record<string, Tone>;
  const key = (status ?? "").toLowerCase().replace(/ /g, "_");
  const tone = table[key];
  if (tone) return tone;
  const seen = `${domain}:${status}`;
  if (status && !warned.has(seen)) {
    warned.add(seen);
    console.warn(`[status] no tone for ${domain} status "${status}"; showing it as quiet`);
  }
  return "quiet";
}
const warned = new Set<string>();
