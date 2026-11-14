// The body of GET /api/factory/reviews on a factory cell (tares/screens/factory_reviews.py). The
// sentences (`review`, `ci`, `holds_up`, `waited`, `autonomy_text`) are worded for people and shown as
// they are. Project, ticket and pull request titles, the repo and the milestone in `holds_up` carry
// a person's own text, and the page shows every string as text.

import { request } from "../../api";

/** The dot of a CI sentence: green, red, still going, or no CI at all. */
export type CiState = "ok" | "bad" | "go" | "none";

export interface ReviewRow {
  project: { id: string; name: string };
  ticket: { ref: string; title: string; href: string };
  pr: { number: number | null; url: string | null; title: string | null; repo: string | null };
  /** How long it has waited, in words: "10 days", "8 minutes". */
  waited: string;
  since: string | null;
  /** What the reviewer said: "Passed, round 2", "Changes asked, round 1", "Not reviewed yet", "Passed on an older commit". */
  review: string;
  /** "CI passed", "CI failing", "CI in progress" or "No CI". */
  ci: string;
  ci_state: CiState;
  /** "Holds up M2: Faster lists" when this is the last pull request its milestone waits for, else null. */
  holds_up: string | null;
  /** The challenger found something blocking that nobody has answered yet. */
  codex_open: boolean;
  /** Who has it next ("The reviewer reviews it") for a crew row, why nobody does for a nobody row; null when it waits for the person. */
  why: string | null;
}

export interface FactoryReviewsData {
  /** The ones that wait for the person to merge, the longest waiting first. */
  waiting_for_you: ReviewRow[];
  /** The ones that wait for someone else's merge (their ticket, or a project they lead), named for them. */
  waiting_for_others?: (ReviewRow & { for_people: { id: string; name: string }[] })[];
  /** The ones a named role of the crew has next, the longest waiting first. */
  with_the_crew: ReviewRow[];
  /** The ones nobody is on (project finished, never handed over, closed, blocked), each with its reason. */
  nobody_on_it: ReviewRow[];
  autonomy_text: string;
  /** The project the list is cut to (?project=), or null for every project. */
  project: { id: string; name: string } | null;
  now: string;
}

/** Every pull request in review, in one read; `project` keeps one project's. */
export const factoryReviews = (project?: string) =>
  request<FactoryReviewsData>(`/api/factory/reviews${project ? `?project=${encodeURIComponent(project)}` : ""}`);
