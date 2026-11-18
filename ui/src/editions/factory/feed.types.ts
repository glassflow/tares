import type { HomeItem, HomeItemKind, HomeRef } from "./home.types";

// The human-in-the-loop feed (P-TR-273, M2 mock). One card per decision a person owes the crew, each
// answered with one tap either way, one tap deeper for the context. The cards are built from the items
// GET /api/home already serves (factory_home.py); what the home does not send yet is in FeedExtra, and
// every field there names the gap of the M1 spec it stands for. Nothing here calls the cell: the page
// shows samples only until the mock is approved.

/** The two answers. Yes takes what the card offers (the crew's recommendation, the merge, the allow). */
export type FeedChoice = "yes" | "no";

/** Where an answer was given. `feed` and `message` are new (spec, "What the trail keeps"); the daemon
 *  records `console` and `session` today (factory_store `via`). */
export type FeedVia = "feed" | "console" | "session" | "message";

/** What No asks for before it is sent. */
export interface FeedNo {
  label: string;
  /** "required": a reason the crew acts on (ask for changes); "optional": a reason it may read; "none": one tap. */
  words: "required" | "optional" | "none";
  /** Answers to pick from instead of writing one (a question's own options, the calls to overturn). */
  options?: string[];
}

export interface FeedYes {
  label: string;
  /** What Yes does to the crew's work, in one sentence, shown one tap deeper. */
  does: string;
}

/** What the home does not send yet. Each field is a gap of the M1 spec; the samples fill them by hand. */
export interface FeedExtra {
  /** Spec, "What an item is": the ask starts with the verb ("Merge PR 336", not "1 PR to review"). */
  ask?: string;
  /** Spec, "What it holds up". A desk question already sends it as `question.blocks`. */
  holds_up?: string | null;
  /** Spec, gap on prs_to_review: "checks green, main moved 3 commits" on the item. */
  checks?: string | null;
  /** The crew's answer and why. A desk question already sends the answer as `question.recommendation`. */
  why?: string | null;
}

export interface FeedCard {
  id: string;
  kind: HomeItemKind;
  /** The kind in one mono word or two ("Question", "Merge"), from the home's `label`. */
  label: string;
  ask: string;
  project: HomeRef | null;
  from: string | null;
  waited: string;
  since: string | null;
  holds_up: string | null;
  blocking: boolean;
  recommendation: { answer: string; why: string | null } | null;
  /** Null when there is nothing to take as it is (a question with no recommendation): No is the only way. */
  yes: FeedYes | null;
  no: FeedNo;
  deeper: { detail: string; lines: string[]; checks: string | null; href: string | null };
}

export interface FeedAnswered {
  card: FeedCard;
  choice: FeedChoice;
  /** The option picked or the words given with No. */
  words: string | null;
  by: string;
  at: string;
  via: FeedVia;
}

/** One sample of the feed. The live page would read /api/home and the day's answers (spec gap: the
 *  desk does not return the choice, who or when). */
export interface FeedData {
  now: string;
  crew: { running: boolean; since: string | null; summary: string };
  items: HomeItem[];
  extras: Record<string, FeedExtra>;
  answered: FeedAnswered[];
}
