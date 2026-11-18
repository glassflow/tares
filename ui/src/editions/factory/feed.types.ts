import type { HomeAction, HomeItem, HomeItemKind, HomeRef } from "./home.types";

// The human-in-the-loop feed (P-TR-273). One card per decision a person owes the crew, each answered
// with one tap either way, one tap deeper for the context. The cards are the items GET /api/home already
// serves (factory_home.py), and their buttons are the items' own actions, run by Home's handler
// (useHomeActions): an answer from the feed takes the same path as one from Home, so the feed adds no
// third way to approve anything. What the home does not send yet is in FeedExtra; every field there
// names the gap it stands for, and only the samples fill it.

/** Where an answer was given, as the cell records it (tares/answers.py): Home is `console`, this page
 *  `feed`, a desk answer matched in the person's session `session`. `message` is for a reply by message. */
export type FeedVia = "feed" | "console" | "session" | "message";

/** What the home does not send yet. The live feed reads none of it; the samples fill it by hand. */
export interface FeedExtra {
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
  /** Null for an answer that is neither: an option picked or words given to a question. */
  choice: "yes" | "no" | null;
  /** What was pressed or said, in its own words ("Postgres", "Later", "Agree"). */
  said: string;
  by: string;
  at: string;
  via: FeedVia;
}

/** One feed: a sample, or the live home read into this shape. */
export interface FeedData {
  now: string;
  crew: { running: boolean; since: string | null; summary: string };
  items: HomeItem[];
  extras: Record<string, FeedExtra>;
  /** The day's answers. Live: the cell's record (GET /api/answers) and what was answered on this page
   *  before the record is read again. */
  answered: FeedAnswered[];
}
