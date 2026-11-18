// The words and small rules of the Decisions page (TR-1444), kept apart from the page so a script can
// run them without a browser. No JSX and no imports beyond lib/time and lib/number.

import { formatCount } from "../../lib/number";
import { ageWords } from "../../lib/time";
import type { DecisionCounts, DecisionRow, DecisionTab } from "./BrainDecisions.api";

export const HEADING = "Decisions";
export const SUBTITLE = "Why things are the way they are, and who decided. The crew reads these before it changes the code they describe.";

export const TABS: { value: DecisionTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "factory_project", label: "Factory projects" },
  { value: "repo_folder", label: "GitHub repos" },
  { value: "by_hand", label: "Second Brain" },
];

const TAB_VALUES = new Set<string>(TABS.map((t) => t.value));

/** The tab an address names (`?where=`), else All. */
export const tabOf = (v: string | null | undefined): DecisionTab => (v && TAB_VALUES.has(v) ? (v as DecisionTab) : "all");

/** "Repo folders 21". The counts are for the whole cell, so with a repo filter on they are left out. */
export function tabLabel(tab: DecisionTab, counts: DecisionCounts | null | undefined, showCounts: boolean): string {
  const label = TABS.find((t) => t.value === tab)?.label ?? "All";
  if (!showCounts || !counts) return label;
  const n = counts[tab];
  return Number.isFinite(n) ? `${label} ${formatCount(Math.max(0, n))}` : label;
}

export const EMPTY_ALL = "No decisions yet. They show here from the decisions folders you pick in GitHub repos, from specs and plans in shared factory projects, and from ones you add below.";
export const EMPTY_ALL_READ_ONLY = "No decisions yet. They show here from the decisions folders of GitHub repos, from specs and plans in shared factory projects, and from ones people add by hand.";

export const EMPTY_TAB: Record<Exclude<DecisionTab, "all">, string> = {
  repo_folder: "Pick a decisions folder on a repo's card to see its files here.",
  factory_project: "The specs, plans and notes of shared factory projects linked to a repo show here.",
  by_hand: "Decisions added on this page show here.",
};

/** The empty line of a tab, or of the list for one repo. */
export function emptyWords(tab: DecisionTab, repo: string | null, canAdd: boolean): string {
  if (repo) return "No decisions for this repo yet.";
  if (tab === "all") return canAdd ? EMPTY_ALL : EMPTY_ALL_READ_ONLY;
  return EMPTY_TAB[tab];
}

/** "Ana", "Ana and Kiran", "Ana, Kiran and Lee". */
export function joinNames(names: readonly string[] | null | undefined): string {
  const n = (names ?? []).map((x) => (typeof x === "string" ? x.trim() : "")).filter(Boolean);
  if (n.length <= 1) return n[0] ?? "";
  return `${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}`;
}

/** Who decided, and who updated it when that is someone else: `{decided: "Ana", updated: "Updated by Kiran"}`.
 *  The page shows them on two lines. */
export function whoParts(r: Pick<DecisionRow, "decided_by" | "changed_by" | "added_by">): { decided: string; updated: string } {
  const decided = joinNames(r.decided_by) || (r.added_by ?? "").trim();
  const changed = joinNames(r.changed_by);
  return { decided, updated: changed && changed !== decided ? `Updated by ${changed}` : "" };
}

/** Milliseconds of a time from the cell: an RFC 3339 time (no zone is UTC) or a bare day. NaN when there is none. */
export function timeOf(v: string | null | undefined): number {
  if (!v || typeof v !== "string") return NaN;
  const s = v.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return Date.parse(`${s}T00:00:00Z`);
  return Date.parse(/(Z|[+-]\d\d:?\d\d)$/i.test(s) ? s : `${s}Z`);
}

/** "3 months ago", from the last change when there is one, else when it was decided. A bare day says
 *  "today" rather than a number of hours. "" with no time. */
export function whenWords(r: Pick<DecisionRow, "decided_at" | "changed_at">, now: number): string {
  const v = r.changed_at || r.decided_at;
  const t = timeOf(v);
  if (Number.isNaN(t)) return "";
  const bareDay = /^\d{4}-\d{2}-\d{2}$/.test((v ?? "").trim());
  if (bareDay && now - t < 24 * 3_600_000) return "today";
  return ageWords(Math.max(0, now - t));
}

/** A decision's origin, as the page lays it out: a label, then the project or repo it came from, with a link. */
export interface Origin { label: string; name?: string; href?: string }

/** The repo's page on GitHub, from `owner/name`; "" for anything else. */
export function repoHref(repo: string | null | undefined): string {
  const r = (repo ?? "").trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r)) return "";
  return `https://github.com/${r.split("/").map(encodeURIComponent).join("/")}`;
}

/** The project's page on this cell, from its id; "" without one. */
export const projectHref = (id: string | null | undefined): string => ((id ?? "").trim() ? `/projects/${encodeURIComponent((id ?? "").trim())}` : "");

/** Where a decision came from: one of the three tabs. */
export function originOf(r: DecisionRow): Origin | null {
  switch (r.where) {
    case "factory_project": {
      const name = (r.project_name ?? "").trim() || (r.project ?? "").trim();
      return { label: "Factory project", name: name || undefined, href: projectHref(r.project) || undefined };
    }
    case "repo_folder": {
      const repo = (r.repo ?? "").trim();
      return { label: "GitHub repo", name: repo || undefined, href: repoHref(repo) || undefined };
    }
    case "by_hand":
      return { label: "Second Brain" };
    default:
      return null;
  }
}

/** The title, or a plain stand-in when the brain has none. */
export const titleOf = (r: Pick<DecisionRow, "title">): string => (r.title ?? "").trim() || "Untitled";

/** A link the page may open: http, https, or one on this cell (starting with one "/"). Anything else is not a link. */
export function safeLink(v: string | null | undefined): string {
  const s = (v ?? "").trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("/") && !s.startsWith("//") && !s.startsWith("/\\")) return s;
  return "";
}

/** A link on this cell opens in the console; one to GitHub in a new tab. */
export const isOnCell = (href: string) => href.startsWith("/");

// ---- adding one

export const ADD_HEADING = "Add a decision by hand";
export const ADD_LINE = "For decisions that live nowhere else. Decisions in GitHub repos and factory projects are added for you.";
export const READ_ONLY_LINE = "Your key can only read, so it cannot add decisions.";
export const MAX_TITLE = 200;
export const MAX_FILE_BYTES = 5 * 1024 * 1024;
export const FILE_TYPES = [".md", ".markdown", ".txt", ".pdf"];

/** What stops the form from being sent, in plain words, or "" when it can go. */
export function formProblem(f: { title: string; text: string; file: { name: string; size: number } | null }): string {
  const title = f.title.trim();
  if (!title) return "Give the decision a title.";
  if ([...title].length > MAX_TITLE) return `Keep the title to ${MAX_TITLE} characters.`;
  if (f.file) return fileProblem(f.file);
  if (!f.text.trim()) return "Paste the decision, or upload a file.";
  return "";
}

/** A file the cell would refuse, said before anything is sent. */
export function fileProblem(file: { name: string; size: number }): string {
  const name = file.name.toLowerCase();
  if (!FILE_TYPES.some((x) => name.endsWith(x))) return "Upload a Markdown, text or PDF file.";
  if (file.size > MAX_FILE_BYTES) return "That file is over 5 MB. Upload a smaller one, or paste the text.";
  if (file.size === 0) return "That file is empty.";
  return "";
}

export const PDF_WORDS = "This cell can't read PDF files yet. Paste the text or upload a Markdown file.";
export const SAVE_FAILED_WORDS = "Couldn't save it. Try again.";
export const TOO_BIG_WORDS = "That is too big to keep. Upload a file under 5 MB, or paste less than 1 MB of text.";
export const NO_WRITE_WORDS = "Your key can only read, so it cannot add decisions.";

/** The sentence an error from the cell becomes: a capital, a full stop. */
export function plainError(message: string | undefined, fallback: string): string {
  const t = (message ?? "").trim();
  if (!t) return fallback;
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}

/** The words for a failed add. A 400 is the cell's own sentence. */
export function addErrorWords(e: { status?: number; message?: string } | null | undefined): string {
  switch (e?.status) {
    case 400: return plainError(e.message, SAVE_FAILED_WORDS);
    case 403: return NO_WRITE_WORDS;
    case 413: return TOO_BIG_WORDS;
    case 501: return PDF_WORDS;
    default: return SAVE_FAILED_WORDS;
  }
}

// ---- taking one back

export const DELETE_TITLE = "Delete this decision?";
export const DELETE_LINE = "It leaves the brain, and the crew stops reading it.";
export const DELETE_FAILED_WORDS = "Couldn't delete it. Try again.";
export const ALREADY_GONE_WORDS = "That decision was already deleted.";
export const NOT_YOURS_WORDS = "Only the person who added it or an admin can delete it.";

export function deleteErrorWords(e: { status?: number; message?: string } | null | undefined): string {
  if (e?.status === 404) return ALREADY_GONE_WORDS;
  if (e?.status === 403) return NOT_YOURS_WORDS;
  return DELETE_FAILED_WORDS;
}

// ---- the list

export const LOADING_WORDS = "Loading decisions. On a big cell this takes a few seconds.";
export const LOAD_FAILED_WORDS = "Decisions did not load.";
export const NOT_SET_UP_WORDS = "The second brain is not set up on this cell.";
