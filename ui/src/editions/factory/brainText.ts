// The words and small rules of the Repos page (TR-1204), kept apart from the page so a script can
// run them without a browser. No JSX and no imports beyond durationWords.

import { durationWords } from "../words";
import type { BrainReposData, BrainRepoRow } from "./BrainRepos.api";

/** While a repo is queued, cloning, indexing or being removed the page reads every 10 seconds. */
export const POLL_FAST_MS = 10_000;
/** Otherwise every minute. */
export const POLL_SLOW_MS = 60_000;

export type CardKind = "queued" | "cloning" | "indexing" | "ready" | "failed" | "removing" | "remove_failed";

/** Which card a row is. A removal in progress or one that gave up comes before the index state. */
export function cardKind(r: BrainRepoRow): CardKind {
  if (r.removing) return "removing";
  if (r.remove_error) return "remove_failed";
  return r.state;
}

/** Whether the row is still changing by itself: the page reads often until nothing is. A repo whose
 *  index failed, or whose removal gave up, waits for a person and does not change on its own. */
export function isMoving(r: BrainRepoRow): boolean {
  const k = cardKind(r);
  return k === "queued" || k === "cloning" || k === "indexing" || k === "removing";
}

/** How long to wait before the next read: 10 seconds while any repo is moving, else 60. */
export function pollMs(data: BrainReposData | undefined): number {
  return data && data.repos.some(isMoving) ? POLL_FAST_MS : POLL_SLOW_MS;
}

export const STATE_WORDS: Record<CardKind, string> = {
  queued: "Queued",
  cloning: "Cloning",
  indexing: "Indexing",
  ready: "Ready",
  failed: "Not indexed",
  removing: "Removing",
  remove_failed: "Not removed",
};

export const REMOVING_WORDS = "Removing. This takes a minute or two.";
export const NOT_SET_UP_WORDS = "The second brain is not set up on this cell.";
export const EMPTY_WORDS = "Connect a repo so the crew can ask about its code instead of searching it.";
export const NO_ROOM_WORDS = "Starts when the brain has room.";
export const REMOVE_WORDS = "Removes its index and decisions from the brain.";

/** A whole number with thousands separators: 1,816. */
export const count = (n: number | null | undefined): string => (Number.isFinite(n) ? Math.max(0, Number(n)) : 0).toLocaleString("en-US");

/** The first 7 characters of a commit. */
export const shortCommit = (c: string | null | undefined): string => (c ? c.slice(0, 7) : "");

/** Milliseconds of an ISO time from the cell. A time with no zone is UTC. NaN when there is none. */
export function timeOf(iso: string | null | undefined): number {
  if (!iso) return NaN;
  return Date.parse(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
}

/** "9 minutes ago" for an ISO time, or "" when the row has none. */
export function ago(iso: string | null | undefined, now: number): string {
  const t = timeOf(iso);
  if (Number.isNaN(t)) return "";
  return `${durationWords(Math.max(0, (now - t) / 1000))} ago`;
}

const ORDINALS = ["", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"];

/** "Third in line." for a place in the control plane's queue (1 is next). Past ten, "Number 11 in line." */
export function placeWords(place: number): string {
  return `${ORDINALS[place] || `Number ${place}`} in line.`;
}

/** What a queued repo waits for. Behind other queued repos (`place` 2 or more) it says where it stands; next in
 *  line it waits for the repo being cloned or indexed now, else a general line. */
export function queuedWords(r: BrainRepoRow, all: BrainRepoRow[]): { waitsFor: string | null; line: string } {
  if (typeof r.place === "number" && Number.isInteger(r.place) && r.place >= 2) return { waitsFor: null, line: placeWords(r.place) };
  const busy = all.find((x) => x.repo !== r.repo && !x.removing && (x.state === "cloning" || x.state === "indexing"));
  return { waitsFor: busy ? busy.repo : null, line: busy ? "finishes." : NO_ROOM_WORDS };
}

/** "Added by Ashish Bagri 9 minutes ago.", "Added 9 minutes ago.", or "" with no time. `who` is the person's own text. */
export function addedParts(r: BrainRepoRow, now: number): { who: string | null; when: string } {
  return { who: r.added_by_name || null, when: ago(r.added_at, now) };
}

/** Progress 0 to 100 for the bar: files done of files total, 0 until the total is known. */
export function percent(r: BrainRepoRow): number {
  if (!(r.files_total > 0)) return 0;
  return Math.min(100, Math.max(0, Math.round((r.files_done / r.files_total) * 100)));
}

/** The sentence an error from the cell becomes in a message: a capital, a full stop. */
export function plainError(message: string | undefined, fallback: string): string {
  const t = (message ?? "").trim();
  if (!t) return fallback;
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}

export const NOT_CONNECTED_WORDS = "This repo is no longer connected.";

/** The words for a failed Remove or Retry: the cell's own sentence, or for a 404 (the repo is already gone) a plain one. */
export function actionError(e: { message?: string; status?: number } | null | undefined, fallback: string): string {
  if (e && e.status === 404) return NOT_CONNECTED_WORDS;
  return plainError(e?.message, fallback);
}
