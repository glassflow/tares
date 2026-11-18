// The words and small rules of the Connect a repo page (TR-1205), kept apart from the page so a script
// can run them without a browser. No JSX and no imports beyond types.

import type { AvailableRepo } from "./BrainConnect.api";

/** The most repos one request connects (the cell refuses more). */
export const MAX_CONNECT = 50;

export const NOTICE_WORDS = "Everyone on this cell will be able to ask about the code in these repos, including people who cannot open them on GitHub.";
export const SLOW_WORDS = "The first index of a large repo can take a while. Its card shows how far it has got.";
export const SUBTITLE_WORDS = "Repos the GitHub connection can read. Pick the ones the brain should know.";
export const NOT_SET_UP_WORDS = "The second brain is not set up on this cell.";
export const NO_GITHUB_WORDS = "The GitHub connection is not set up yet.";
export const NO_REPOS_WORDS = "The connection cannot read any repos yet.";
export const ALL_CONNECTED_WORDS = "Every repo the connection can read is already connected.";
export const NOT_ADMIN_WORDS = "Only admins can connect repos.";
export const ASK_ADMIN_WORDS = "Ask an admin of this cell.";
export const TOO_MANY_WORDS = `Connect at most ${MAX_CONNECT} repos at a time.`;
export const NOT_DONE = "That did not go through. Try again in a moment.";
/** Where the GitHub connection is set up: the GitHub tab of Settings. */
export const GITHUB_SETTINGS = "/settings?tab=github";

/** The button: "Connect repos" with nothing chosen, then "Connect 1 repo", "Connect 3 repos". */
export function connectLabel(n: number): string {
  return n <= 0 ? "Connect repos" : n === 1 ? "Connect 1 repo" : `Connect ${n} repos`;
}

/** The repos a person can still choose: the ones not on the brain yet. */
export const choosable = (all: AvailableRepo[]): AvailableRepo[] => all.filter((r) => !r.connected);

/** The names of the repos already on the brain, in the cell's order. */
export const connectedNames = (all: AvailableRepo[]): string[] => all.filter((r) => r.connected).map((r) => r.repo);

/** The repos whose name holds the words typed (case does not matter, spaces at the ends do not count). */
export function matching(repos: AvailableRepo[], query: string): AvailableRepo[] {
  const q = query.trim().toLowerCase();
  return q ? repos.filter((r) => r.repo.toLowerCase().includes(q)) : repos;
}

/** What the muted line under a repo says. */
export const visibility = (r: AvailableRepo): string => (r.private ? "Private" : "Public");

/** How many of the repos about to be sent the search hides: chosen, and not among the repos shown. */
export function hiddenChosen(shown: AvailableRepo[], sending: string[]): number {
  const on = new Set(shown.map((r) => r.repo));
  return sending.filter((name) => !on.has(name)).length;
}

/** The line that says so: "1 chosen repo is hidden by the search.", "2 chosen repos are hidden by the search." Empty for none. */
export const hiddenWords = (n: number): string => (n <= 0 ? "" : n === 1 ? "1 chosen repo is hidden by the search." : `${n} chosen repos are hidden by the search.`);

/** The repos to send: those chosen, in the list's order, and only ones still on offer. */
export function picked(all: AvailableRepo[], chosen: ReadonlySet<string>): string[] {
  return choosable(all).filter((r) => chosen.has(r.repo)).map((r) => r.repo);
}

/** The names of the admins among the people of the cell. */
export const adminNames = (people: { name: string; admin: boolean }[]): string[] => people.filter((p) => p.admin).map((p) => p.name);

/** The sentence an error from the cell becomes in a message: a capital, a full stop. */
export function plainError(message: string | undefined, fallback: string): string {
  const t = (message ?? "").trim();
  if (!t) return fallback;
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}
