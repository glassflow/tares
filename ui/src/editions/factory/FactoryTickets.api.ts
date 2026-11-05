// The body of GET /api/factory/tickets on a factory cell (tares/screens/factory_tickets.py). The
// state, who and pull request words come ready for people and are shown as they are, apart from the
// short state labels the page keeps for its columns. Titles, project and milestone names are a
// person's own text, and the page shows every string as text.

import { request } from "../../api";

/** The states a ticket is in, the way the cell counts them. */
export type StateKey = "to_do" | "in_progress" | "in_review" | "changes_asked" | "blocked" | "merged" | "canceled";

export interface TicketRow {
  /** "DS-21": the ticket's own number, unique inside its project. */
  ref: string;
  title: string;
  /** "in review", in words. */
  state: string;
  state_key: StateKey;
  project: { id: string; name: string };
  milestone: { id: string; number: string; name: string } | null;
  /** "Builder for M2", "Reviewer", "Lead", "Not picked up yet"; null once the ticket is finished. */
  who: string | null;
  pr: { number: number | null; url: string | null; state_text: string } | null;
  /** The labels of the tickets this one waits for, when it is blocked. */
  blocked_by: string[];
  /** "27 minutes ago", "just now"; null when nothing is recorded. */
  changed: string | null;
  changed_at: string | null;
  /** The ticket's page: /projects/<id>/tickets/<ref>. */
  href: string;
}

export interface TicketCounts {
  total: number;
  moving: number;
  to_do: number;
  in_progress: number;
  in_review: number;
  changes_asked: number;
  blocked: number;
  merged: number;
  canceled: number;
}

export interface FactoryTicketsData {
  tickets: TicketRow[];
  /** Matching tickets, not counting canceled ones unless the state asks for them. */
  total: number;
  offset: number;
  limit: number;
  /** More tickets follow the ones returned. */
  more: boolean;
  /** For the project, milestone and search chosen, whatever the state. */
  counts: TicketCounts;
  /** Every project, in the order of the Projects page, with its ticket count. */
  projects: { id: string; name: string; count: number }[];
  /** The chosen project's milestones; empty with no project chosen. */
  milestones: { id: string; project: string; number: string; name: string }[];
  /** How many canceled tickets the list leaves out. */
  hidden_canceled: number;
  now: string;
}

/** "open": every ticket not merged first, then merged, newest first in each (the Board and Swimlanes). */
export type TicketSort = "new" | "old" | "ref" | "open";

export interface TicketsQuery {
  project?: string;
  milestone?: string;
  /** "to-do", "in-progress", "in-review", "changes-asked", "blocked", "merged", "canceled" or "moving". */
  state?: string;
  q?: string;
  sort?: TicketSort;
  limit?: number;
  offset?: number;
}

/** Tickets across projects, in one read; the query narrows and pages it. */
export function factoryTickets(query: TicketsQuery = {}): Promise<FactoryTicketsData> {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== "" && v !== null) p.set(k, String(v));
  const s = p.toString();
  return request<FactoryTicketsData>(`/api/factory/tickets${s ? `?${s}` : ""}`);
}
