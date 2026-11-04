// The body of GET /api/factory/crew on a factory cell (tares/screens/factory_crew.py). Every sentence
// is worded for people and shown as it is. What a member is doing, a project's name and a rule's text
// carry a person's own words, so the page shows every string as text, never as markup. `station` is
// only the key for the detail call; the page never shows it.

import { request } from "../../api";

export type MemberState = "working" | "idle" | "waiting for you" | "not heard" | "ended";

export interface CrewMember {
  station: string;
  /** "The one you talk to", "Reviewer", "Releaser" or "Builder". */
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
}

export interface CrewBuilders {
  /** `id` is null for builders that are not on a project the cell knows. */
  project: { id: string | null; name: string };
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
}

export interface CrewPage {
  /** "Working, last heard 12 seconds ago", "Not heard from in 2 hours", "Not running" or "Not started". */
  summary: string;
  talk_to: CrewMember | null;
  reviewer: CrewMember | null;
  releaser: CrewMember | null;
  builders: CrewBuilders[];
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

/** The part of GET /api/crew/{station} the fold reads. */
export interface MemberDetail {
  /** The command that opens the member's session in a terminal. */
  attach: string;
  recent: MemberLine[];
}

export const factoryCrew = () => request<CrewPage>("/api/factory/crew");

/** What a member did lately, read only when its fold is opened. */
export const crewMember = (station: string) =>
  request<MemberDetail>(`/api/crew/${encodeURIComponent(station)}?recent=30`);

/** Asks for a new reading of the member's memory; the crew's machine answers within seconds. */
export const readMemoryAgain = (station: string) =>
  request<unknown>(`/api/crew/${encodeURIComponent(station)}/context`, { method: "POST" });

/** A person decides a rule the reviewer proposed. */
export const decideRule = (id: string, decision: "accept" | "reject") =>
  request<unknown>(`/api/rule-proposals/${encodeURIComponent(id)}/${decision}`, { method: "POST", body: "{}" });
