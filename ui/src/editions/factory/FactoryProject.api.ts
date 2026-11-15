// The body of GET /api/factory/projects/{id} on a factory cell (tares/screens/factory_project.py). Every
// string is worded for people and shown as it is. `name`, `goal`, ticket and milestone titles, call
// text, decisions and doc titles carry a person's own text. The one raw string is a terminal command.

import { request } from "../../api";
import type { HomeItem } from "./home.types";
import type { SecretsDoc } from "./ProjectSecrets";

export type ProjectStage = "spec" | "build" | "review" | "done";
export type ProjectPageState = "building" | "waiting_on_you" | "not_started" | "ready_to_close" | "later" | "finished";

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
  /** Each acceptance check: `for` the crew (it runs and records them) or a person; `result` the latest run, null before one. */
  checks: { n: number; check: string; expect: string; for: "crew" | "person"; result: "pass" | "fail" | null; note: string | null; by: string | null }[];
  /** Accepted: its checks passed, or the person accepted it as it is. */
  accepted: boolean;
  /** Merged and waiting for the person: a check for them, or crew checks never recorded. */
  needs_person: boolean;
  /** The person may accept it as it is (the crew never recorded its checks, none is for them). */
  can_accept: boolean;
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

/** A call the person already kept or changed, with their words when they changed it. */
export interface PastCall extends ProjectCall {
  state: "kept" | "overturned";
  words: string | null;
  decided: string;
}

export interface ProjectDoc {
  id: string;
  title: string;
  kind_label: string;
  updated: string;
  href: string;
}

export interface ProjectPerson { id: string; name: string; picture: string | null; tickets?: number }

/** Who is on the project: the lead, the members (added, or holding a ticket), and what the caller may change. */
export interface ProjectPeople {
  lead: ProjectPerson | null;
  members: ProjectPerson[];
  can_set_lead: boolean;
  can_add: boolean;
  you: string | null;
  /** The people the caller may pick from, when they may change something. */
  people: ProjectPerson[];
}

export interface FactoryProjectData {
  id: string;
  name: string;
  /** One emoji shown beside the name, or none. */
  icon?: string | null;
  /** The caller may change the icon (an edit of the project); a sample leaves it out. */
  can_edit_icon?: boolean;
  goal: string;
  stage: ProjectStage;
  stage_label: string;
  state: ProjectPageState;
  state_label: string;
  /** how many pull requests the project's GitHub page lists */
  prs_total?: number;
  /** the three newest pull requests */
  prs_recent?: ProjectPr[];
  waiting: HomeItem[];
  /** On a team cell, the questions and permission requests on this project addressed to someone
   *  else: labelled for that person, with nothing to press (only they may answer). */
  waiting_others?: HomeItem[];
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
  calls_past?: PastCall[];
  docs: ProjectDoc[];
  /** How many documents the project has, working notes and notes too (its docs page lists them). */
  docs_total?: number;
  terminal: { label: string; command: string }[];
  /** Pull requests by state: open counts drafts too. */
  prs_counts?: { open: number; merged: number; closed: number };
  /** The repos handed over to the crew, by name. */
  repos?: string[];
  linked: { tickets_in: "Linear" | "GitHub" | null; url: string | null };
  can_close: boolean;
  /** Kept for later: off the home until someone starts it. `can_later` when it may be kept for later now. */
  later?: boolean;
  can_later?: boolean;
  /** Who is on the project (TR-1023). */
  people?: ProjectPeople;
  /** The project's secrets, names only (TR-775); null for someone not on the project. */
  secrets?: SecretsDoc | null;
  now: string;
}

/** Keep the project for later, off the home, or start it again. */
export const setLater = (id: string, later: boolean) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/later`, { method: "POST", body: JSON.stringify({ later }) });

/** The whole project page, in one read. */
export const setProjectLead = (id: string, personId: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ lead_person_id: personId }) });

export const setProjectIcon = (id: string, icon: string | null) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ icon }) });

export const addProjectMember = (id: string, personId: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/members`, { method: "POST", body: JSON.stringify({ person_id: personId }) });

export const factoryProject = (id: string) =>
  request<FactoryProjectData>(`/api/factory/projects/${encodeURIComponent(id)}`);

/** Keep a call the crew made, or overturn it with the person's own words. */
export const decideCall = (id: string, ref: string, state: "kept" | "overturned", words?: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/assumptions/${encodeURIComponent(ref)}/state`, {
    method: "POST",
    body: JSON.stringify(words ? { state, words } : { state }),
  });

/** The person's answer to a milestone check marked for them (TR-1017): it works, or what is wrong. */
export const answerCheck = (id: string, milestone: string, n: number, result: "pass" | "fail", note?: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/milestones/${encodeURIComponent(milestone)}/checks/${n}`, {
    method: "POST", body: JSON.stringify({ result, note: note || undefined }),
  });

/** Accept a merged milestone as it is: its crew checks were never recorded (TR-1017). */
export const acceptMilestone = (id: string, milestone: string) =>
  request<unknown>(`/api/projects/${encodeURIComponent(id)}/milestones/${encodeURIComponent(milestone)}/accept`, {
    method: "POST", body: JSON.stringify({}),
  });

// The body of GET /api/factory/projects/{id}/github: the project's pull requests by repo, as GitHub
// said when Tares last read them. `title` and `ticket_title` carry a person's own text.
export type PrState = "open" | "draft" | "merged" | "closed";

export interface ProjectPr {
  number: number | null;
  url: string | null;
  repo: string | null;
  title: string;
  ticket: string;
  ticket_title: string;
  ticket_href: string;
  state: PrState;
  state_label: string;
  /** "CI green", "CI failing", "CI running"; null when merged, closed or unknown. */
  ci: string | null;
  /** "round 2: passed", from the crew's reviewer; null before a review. */
  review: string | null;
  branch: string | null;
  base: string | null;
  head: string | null;
  /** "merged 2 days ago by kim", "opened 3 hours ago by tares-bot", "closed 1 day ago". */
  when: string;
  size: { files: number; additions: number; deletions: number; commits: number | null } | null;
  error: string | null;
}

export interface ProjectRepoPrs {
  name: string | null;
  url: string | null;
  counts: { open: number; merged: number; closed: number };
  /** lines added and removed by the merged pull requests */
  added: number;
  removed: number;
  prs: ProjectPr[];
}

export interface ProjectGitHubData {
  id: string;
  name: string;
  icon?: string | null;
  repos: ProjectRepoPrs[];
  counts: { open: number; merged: number; closed: number };
  /** "1 minute ago": when Tares last read GitHub for this project; null before any read. */
  checked: string | null;
  github_error: string | null;
  now: string;
}

export const factoryProjectGitHub = (id: string) =>
  request<ProjectGitHubData>(`/api/factory/projects/${encodeURIComponent(id)}/github`);
