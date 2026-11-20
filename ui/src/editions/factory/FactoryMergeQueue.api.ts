import { request } from "../../api";
import { deskReply } from "./FactoryHome.api";
import type { TicketTurnFields } from "./FactoryProject.api";

// The merge queue page's one read (TR-1094): GET /api/projects/{uid}/merge-queue. Every sentence
// comes from the API worded for people; the page only shows it.

export type MqChipKey =
  | "approval" | "other" | "moved" | "approved" | "hold" | "asking" | "merging"
  | "ci" | "queued" | "next" | "blocked" | "sent_back";

export type MqState = "waiting_approval" | "merging" | "waiting_ci" | "queued" | "blocked" | "sent_back";

export type MqAnswerKind = "mine" | "other" | "moved" | "done" | "withdrawn" | "unrecorded" | "missing";

export interface MqAnswer {
  kind: MqAnswerKind;
  text: string; lead: string; note: string;
  item: { id: string; label: string | null } | null;
  can_answer: boolean;
  for: { id: string; name: string } | null;
  asked: string; now: string;
  decision: "yes" | "no" | null;
  by: string | null;
  where: { key: string; words: string } | null;
  when: string; commit: string;
}

/** `turn` and its words are the ticket's (whose turn it is); `chip` is what the queue does with it. A row with no ticket has turn null. */
export interface MqRow extends Omit<TicketTurnFields, "turn" | "whose"> {
  turn: TicketTurnFields["turn"] | null;
  whose: TicketTurnFields["whose"] | null;
  id: string;
  position: number | null;
  state: MqState;
  chip: { key: MqChipKey; text: string };
  ticket: { ref: string; title: string };
  pr: { number: number; url: string };
  branch: string;
  builder: { id: string | null; name: string; text: string } | null;
  review: string; ci: string; guardrail: string;
  status: { lead: string; text: string; note: string };
  answer: MqAnswer | null;
  bounce: { count: number; text: string } | null;
  needs_you: boolean;
}

export interface MqMerged { n: number; tk: string; title: string; when: string; by: string }

export interface MqGroup {
  repo: string; base: string; title: string; count: string; auto: string; sub: string;
  line: number; you: number; blocked: number; sent: number;
  rows: MqRow[];
  merged: { title: string; items: MqMerged[] } | null;
}

export interface MergeQueueData {
  project: { id: string; name: string };
  aside: string; intro: string; empty: string;
  tab: { line: number; need: number; need_text: string; note: string };
  footer: string[];
  groups: MqGroup[];
}

export const mergeQueue = (id: string) =>
  request<MergeQueueData>(`/api/projects/${encodeURIComponent(id)}/merge-queue`);

// The question a pull request waits on offers these two options, in this order
// (tares/merge_queue.py asks them), so a click is a pick by position: A is yes, B is no.
export const YES = { words: "A", choice: "Yes, merge" } as const;
export const NO = { words: "B", choice: "No, hold" } as const;

export const answerRow = (desk: string, given: typeof YES | typeof NO) =>
  deskReply(desk, given.words, false, given.choice);
