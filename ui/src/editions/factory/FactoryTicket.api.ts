// The body of GET /api/factory/projects/{id}/tickets/{ref} on a factory cell (tares/screens/
// factory_ticket.py). Every string is worded for people and shown as it is. The ticket's title,
// a finding's text, the questions, choices and decisions carry a person's own text. A review's
// `verified` and `not_verified`, a check's `command` and `result`, and a history `text` are the
// reviewer's and the builder's own words, shown as written. The one raw string is a terminal command.

import { request } from "../../api";

/** The key of a ticket's state; `state_label` is the same state in words ("Changes asked"). */
export type TicketStateKey =
  | "to_do" | "in_progress" | "in_review" | "changes_asked" | "blocked" | "merged" | "canceled";

/** A ticket this one depends on, or one that waits for it. `href` is the other ticket's page. */
export interface TicketLink {
  ref: string;
  title: string;
  state_label: string;
  href: string;
}

export interface TicketFinding {
  label: string;
  text: string;
  file: string | null;
  line: number | null;
  blocking: boolean;
  /** "open", "fixed in round 3" or "fixed". */
  state_text: string;
}

export interface TicketReview {
  round: number;
  /** "passed", "asked for changes" or "blocked". */
  verdict_text: string;
  by: string | null;
  when: string;
  at: string;
  verified: string;
  not_verified: string | null;
  findings: TicketFinding[];
}

export interface TicketCheck {
  command: string;
  result: string;
  when: string;
}

export interface TicketCall {
  ref: string;
  question: string;
  choice: string;
  why: string;
  /** "by the builder, 2 days ago". */
  made: string;
  /** "waiting for you", "confirmed" or "you changed it". */
  state_text: string;
  words: string | null;
}

export interface TicketDecision {
  question: string;
  /** What was chosen. */
  answer: string;
  /** The person's words as typed, only when they add something to the answer ("D85 A"). */
  said: string | null;
  when: string;
}

export interface TicketHistoryLine {
  what: string;
  who: string | null;
  when: string;
  at: string;
  text: string | null;
}

export interface FactoryTicketData {
  ref: string;
  title: string;
  state: TicketStateKey;
  state_label: string;
  milestone: { id: string; number: string; name: string } | null;
  in: "Linear" | "GitHub" | "Tares";
  url: string | null;
  who: string | null;
  /** What a blocked ticket waits for, in words; null for any other ticket. */
  blocked_reason: string | null;
  depends_on: TicketLink[];
  blocks: TicketLink[];
  pr: { number: number | null; url: string | null; state_text: string } | null;
  reviews: TicketReview[];
  checks: TicketCheck[];
  calls: TicketCall[];
  decisions: TicketDecision[];
  history: TicketHistoryLine[];
  notes: string;
  terminal: { label: string; command: string }[];
  project: { id: string; name: string; icon?: string | null; href: string };
  now: string;
}

/** The whole ticket page, in one read. */
export const factoryTicket = (id: string, ref: string) =>
  request<FactoryTicketData>(`/api/factory/projects/${encodeURIComponent(id)}/tickets/${encodeURIComponent(ref)}`);
