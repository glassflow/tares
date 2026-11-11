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
export const crewMember = (station: string, before?: number) =>
  request<MemberDetail>(`/api/crew/${encodeURIComponent(station)}?recent=0&conversation=200${before !== undefined ? `&before=${before}` : ""}`);

/** One tool call's whole output, for a card whose output the list cut. */
export const crewToolOutput = (station: string, id: string) =>
  request<{ output: string; failed: boolean }>(`/api/crew/${encodeURIComponent(station)}/tool/${encodeURIComponent(id)}`)
    .then((r) => r.output);

/** Asks for a new reading of the member's memory; the crew's machine answers within seconds. */
export const readMemoryAgain = (station: string) =>
  request<unknown>(`/api/crew/${encodeURIComponent(station)}/context`, { method: "POST" });

/** A person decides a rule the reviewer proposed. */
export const decideRule = (id: string, decision: "accept" | "reject") =>
  request<unknown>(`/api/rule-proposals/${encodeURIComponent(id)}/${decision}`, { method: "POST", body: "{}" });
