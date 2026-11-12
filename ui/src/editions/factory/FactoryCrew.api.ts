// The body of GET /api/factory/crew on a factory cell (tares/screens/factory_crew.py). Every sentence
// is worded for people and shown as it is. What a member is doing, a project's name and a rule's text
// carry a person's own words, so the page shows every string as text, never as markup. `station` is
// only the key for the detail call; the page never shows it.

import { request } from "../../api";

export type MemberState = "working" | "idle" | "waiting for you" | "not heard" | "ended";

export interface CrewMember {
  station: string;
  /** "Orchestrator", "Reviewer", "Releaser" or "Builder". */
  role_label: string;
  state: MemberState;
  /** The state in words with its length: "Working", "Idle for 8 minutes", "Not heard from in 2 hours". */
  state_text: string;
  doing: string;
  /** "12 seconds ago", or null when the station was never heard from. */
  heard: string | null;
  memory_pct: number | null;
  /** "Memory 82% full", or null when there is no reading. */
  memory_text: string | null;
  helpers: number;
  /** The id of the member's crew, for the detail call: crews repeat station names. */
  crew?: string | null;
}

/** One crew on the cell, under the person it belongs to (TR-1223). */
export interface CrewOfPerson {
  id: string | null;
  person: { id: string | null; name: string | null };
  machine: string;
  /** True when the person has more than one crew: then the machine tells them apart. */
  machine_shown: boolean;
  mine: boolean;
  /** The crew's own name (crew_name in its crew.json), "crew" when it was never given one. */
  name?: string;
  /** Every member ended over a day ago: the page puts it away under "Show N ended crews". */
  ended?: boolean;
  /** When the crew was last heard from, as "2 days ago". */
  last_heard?: string | null;
  summary: string;
  talk_to: CrewMember | null;
  reviewer: CrewMember | null;
  releaser: CrewMember | null;
  builders: CrewBuilders[];
}

export interface CrewBuilders {
  /** `id` is null for builders that are not on a project the cell knows. */
  project: { id: string | null; name: string; icon?: string | null };
  members: CrewMember[];
}

export interface CrewLimit {
  kind: string;
  /** "Current 5 hours" or "This week". */
  label: string;
  pct: number;
  resets: string | null;
  resets_at: string | null;
  /** "11% used, resets Friday". */
  text: string;
}

export interface CrewPlan {
  known: boolean;
  limits: CrewLimit[];
  share_text: string | null;
  by_project: { name: string; share_text: string; cost_text: string }[];
}

export interface CrewProposal {
  id: string;
  text: string;
  /** Always "the reviewer" today. */
  from: string;
  when: string;
  /** Where accepting it writes the rule: "For shop" (that project's AGENTS.md) or "For every project". */
  for?: string;
  /** The tickets the finding came back in, as "shop T3". A rule belongs to no crew. */
  tickets?: string[];
}

export interface CrewPage {
  /** "Working, last heard 12 seconds ago", "Not heard from in 2 hours", "Not running" or "Not started". */
  summary: string;
  talk_to: CrewMember | null;
  reviewer: CrewMember | null;
  releaser: CrewMember | null;
  builders: CrewBuilders[];
  /** Every crew on the cell, the caller's own first; the fields above are the first one's. */
  crews?: CrewOfPerson[];
  how_far: { text: string; detail: string; settings_href: string };
  /** A sentence while the plan's limits press on the crew, else null. */
  pacing: string | null;
  plan: CrewPlan;
  rule_proposals: CrewProposal[];
  standup: { text: string; when: string } | null;
  now: string;
}

/** One line of a member's session, from GET /api/crew/{station}. */
export interface MemberLine {
  at: string | null;
  type: string | null;
  text: string;
}

/** One item of a member's session as a conversation (tares/crew_conversation.py, TR-1177). */
export interface ConvoSaid { kind: "said"; at: string | null; text: string }
export interface ConvoAsked { kind: "asked"; at: string | null; text: string; from: string; quiet: boolean }
export interface ConvoDivider { kind: "divider"; at: string | null; text: string }
export interface ConvoTool {
  kind: "tool"; at: string | null; id: string; name: string; summary: string;
  /** The call's input as JSON text, and its result's text (null while no result came back); both cut
   *  in the list, `more` true when the output was: GET /api/crew/{station}/tool/{id} has it whole. */
  input: string; output: string | null; failed: boolean; ms: number | null; more: boolean;
}
export type ConvoItem = ConvoSaid | ConvoAsked | ConvoDivider | ConvoTool;

/** The part of GET /api/crew/{station} the fold reads. */
export interface MemberDetail {
  /** The command that opens the member's session in a terminal. */
  attach: string;
  recent: MemberLine[];
  /** The session's last lines as a conversation, oldest first, and the line it starts at. */
  conversation?: ConvoItem[];
  conversation_from?: number;
}

export const factoryCrew = () => request<CrewPage>("/api/factory/crew");

/** What a member did lately, read only when its fold is opened. */
const crewQ = (crew?: string | null) => (crew ? `crew=${encodeURIComponent(crew)}` : "");

export const crewMember = (station: string, at?: { before?: number; since?: number }, crew?: string | null) => {
  const extra = at?.before !== undefined ? `&before=${at.before}` : at?.since !== undefined ? `&since=${at.since}` : "";
  return request<MemberDetail>(`/api/crew/${encodeURIComponent(station)}?recent=0&conversation=200${extra}${crew ? `&${crewQ(crew)}` : ""}`);
};

/** One tool call's whole output, for a card whose output the list cut. */
export const crewToolOutput = (station: string, id: string, crew?: string | null) =>
  request<{ output: string; failed: boolean }>(`/api/crew/${encodeURIComponent(station)}/tool/${encodeURIComponent(id)}${crew ? `?${crewQ(crew)}` : ""}`)
    .then((r) => r.output);

/** Asks for a new reading of the member's memory; the crew's machine answers within seconds. */
export const readMemoryAgain = (station: string, crew?: string | null) =>
  request<unknown>(`/api/crew/${encodeURIComponent(station)}/context${crew ? `?${crewQ(crew)}` : ""}`, { method: "POST" });

/** A person decides a rule the reviewer proposed. */
export const decideRule = (id: string, decision: "accept" | "reject") =>
  request<unknown>(`/api/rule-proposals/${encodeURIComponent(id)}/${decision}`, { method: "POST", body: "{}" });
