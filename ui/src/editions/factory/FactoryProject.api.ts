// The body of GET /api/factory/projects/{id} on a factory cell (tares/screens/factory_project.py). Every
// string is worded for people and shown as it is. `name`, `goal`, ticket and milestone titles, call
// text, decisions and doc titles carry a person's own text. The one raw string is a terminal command.

import { request } from "../../api";
import type { HomeItem } from "./home.types";

export type ProjectStage = "spec" | "build" | "review" | "done";
export type ProjectPageState = "building" | "waiting_on_you" | "not_started" | "ready_to_close" | "finished";

/** A ticket's state in words, as the cell says it. */
export type TicketState = "to do" | "in progress" | "in review" | "changes asked" | "blocked" | "merged" | "canceled";

export interface MovingTicket {
  ref: string;
  title: string;
  state: TicketState;
  who: string | null;
  pr: { number: number; url: string; state_text: string } | null;
  review: string | null;
  blocked_by: string[];
  href: string;
}

export interface ProjectMilestone {
  id: string;
  number: string;
  name: string;
  goal: string;
  done: boolean;
  counts: { total: number; merged: number };
  checks: { check: string; expect: string }[];
  tickets_href: string;
}

export interface ProjectCall {
  ref: string;
  question: string;
  choice: string;
  why: string;
  made: string;
  tickets: string[];
}

export interface ProjectDoc {
  id: string;
  title: string;
  kind_label: string;
  updated: string;
  href: string;
}

export interface FactoryProjectData {
  id: string;
  name: string;
  goal: string;
  stage: ProjectStage;
  stage_label: string;
  state: ProjectPageState;
  state_label: string;
  waiting: HomeItem[];
  counts: {
    total: number; to_do: number; in_progress: number; in_review: number;
    changes_asked: number; blocked: number; merged: number; canceled: number;
  };
  moving: MovingTicket[];
  moving_total: number;
  tickets_href: string;
  next: { ticket: string; title: string; why: string } | null;
  milestones: ProjectMilestone[];
  /** `answer` is what was chosen; `said` the person's words as typed, only when they add something ("D85 A"). */
  decisions: { question: string; answer: string; said: string | null; when: string; at: string }[];
  calls: ProjectCall[];
  docs: ProjectDoc[];
  terminal: { label: string; command: string }[];
  linked: { tickets_in: "Linear" | "GitHub" | null; url: string | null };
  can_close: boolean;
  now: string;
}

/** The whole project page, in one read. */
export const factoryProject = (id: string) =>
  request<FactoryProjectData>(`/api/factory/projects/${encodeURIComponent(id)}`);

/** Keep a call the crew made, or overturn it with the person's own words. */
export const decideCall = (id: string, ref: string, state: "kept" | "overturned", words?: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/assumptions/${encodeURIComponent(ref)}/state`, {
    method: "POST",
    body: JSON.stringify(words ? { state, words } : { state }),
  });
