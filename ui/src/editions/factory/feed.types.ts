import type { HomeAction, HomeItem, HomeItemKind, HomeRef } from "./home.types";

// The human-in-the-loop feed (P-TR-273). One card per decision a person owes the crew, each answered
// with one tap either way, one tap deeper for the context. The cards are the items GET /api/home already
// serves (factory_home.py), and their buttons are the items' own actions, run by Home's handler
// (useHomeActions): an answer from the feed takes the same path as one from Home, so the feed adds no
// third way to approve anything. What the home does not send yet is in FeedExtra; every field there
// names the gap it stands for, and only the samples fill it.

/** Where an answer was given. `feed` and `message` are new; the daemon records `console` and
 *  `session` today (factory_store `via`), and does not return the day's answers yet. */
export type FeedVia = "feed" | "console" | "session" | "message";

/** What the home does not send yet. The live feed reads none of it; the samples fill it by hand. */
export interface FeedExtra {
  /** Gap on prs_to_review: "Checks green. Main moved 2 commits since the review." */
  checks?: string | null;
  /** Why the crew suggests its answer, for a sample. A question sends it as `question.why`. */
  why?: string | null;
}

export interface FeedCard {
  id: string;
  kind: HomeItemKind;
  /** The kind in a word or two ("Question", "Permission"), from the home's `label`. */
  label: string;
  ask: string;
  project: HomeRef | null;
  from: string | null;
  waited: string;
  since: string | null;
  holds_up: string | null;
  blocking: boolean;
  recommendation: { answer: string; why: string | null } | null;
  /** Takes what the card offers: one of the item's actions, or a link to the page that has it. */
  yes: HomeAction | null;
  /** Declines or puts it off (later, decline, an answer in words); null when the item has none. */
  no: HomeAction | null;
  deeper: { detail: string; lines: string[]; checks: string | null; href: string | null };
  /** The item itself: Home's handler takes it with the action. */
  item: HomeItem;
}

export interface FeedAnswered {
  id: string;
  ask: string;
  /** Null for an answer read back from the desk, which keeps the words but not which way they went. */
  choice: "yes" | "no" | null;
  /** What was pressed, in its own words ("Yes, Postgres", "Later"). Null for an answer read back from
   *  the desk, which keeps who and when but not the words (gap). */
  said: string | null;
  by: string;
  at: string;
  /** Null when read back from the desk, which does not say where it was answered (gap). */
  via: FeedVia | null;
}

/** One feed: a sample, or the live home read into this shape. */
export interface FeedData {
  now: string;
  crew: { running: boolean; since: string | null; summary: string };
  items: HomeItem[];
  extras: Record<string, FeedExtra>;
  /** The day's answers. Live: the desk's answered questions (GET /api/desk?state=answered) and what was
   *  answered on this page; other kinds leave no record the feed can read yet. */
  answered: FeedAnswered[];
}
