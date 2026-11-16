// The words and small rules of the Ask page (TR-1206), kept apart from the page so a script can run them
// without a browser. No JSX and no imports beyond durationWords.

import { durationWords } from "../words";
import type { AskRepoRow, BrainCitation } from "./BrainAsk.api";

/** The cell refuses a longer question (ee brain/ask.py, MAX_QUESTION). */
export const MAX_QUESTION = 2000;

export const NOT_SET_UP_WORDS = "The second brain is not set up on this cell.";
export const NOTHING_TO_ASK_TITLE = "Nothing to ask yet";
export const CONNECT_FIRST_WORDS = "Connect a repo first.";
export const NOT_READY_WORDS = "No repo is ready yet.";
export const ANSWER_READY_WORDS = "Answer ready.";
export const NO_ANSWER_WORDS = "No answer came back. Try asking again.";
export const NO_ACCESS_WORDS = "You do not have access to the brain. Ask an admin of this cell.";
export const NOT_AVAILABLE_WORDS = "The second brain is not available on this cell.";
const LIMIT_RESET_WORDS = "Try again after midnight UTC.";
export const WAITING_WORDS = "Asking the brain...";
export const WAITING_DEEP_WORDS = "Asking the brain to look deeper... This can take a minute.";
export const TOO_LONG_WORDS = "That question is too long. Keep it to 2,000 characters or fewer.";
export const NETWORK_WORDS = "Could not reach the cell. Check your connection and try again.";
export const NOT_ANSWERED_WORDS = "The brain could not answer. Try again in a moment.";
export const READ_FAILED_WORDS = "The cell did not answer. Try again in a moment.";

/** The characters of a question as the cell counts them (a person's emoji is one, not two). */
export const questionLength = (q: string): number => Array.from(q.trim()).length;

/** "1 repo", "3 repos". */
export const repoCount = (n: number): string => `${n} ${n === 1 ? "repo" : "repos"}`;

/** The line under the heading. Without a repo it does not count. */
export function subtitle(n: number): string {
  return `Ask about the code and decisions in ${n > 0 ? repoCount(n) : "the repos on the brain"}. The crew asks the same brain.`;
}

/** The repos on the brain: every one that is not being removed. */
export const reposOnBrain = (rows: AskRepoRow[]): AskRepoRow[] => rows.filter((r) => !r.removing);

/** A repo an answer can draw on. */
export const readyRepos = (rows: AskRepoRow[]): AskRepoRow[] => reposOnBrain(rows).filter((r) => r.state === "ready");

/** The names of the repos still being taken in (queued, cloning or indexing), in the cell's order. */
export const indexingNames = (rows: AskRepoRow[]): string[] =>
  reposOnBrain(rows).filter((r) => r.state === "queued" || r.state === "cloning" || r.state === "indexing").map((r) => r.repo);

/** What goes between name `i` and the next of `n` names: ", " and then " and " before the last. */
export const nameSeparator = (i: number, n: number): string => (i >= n - 1 ? "" : i === n - 2 ? " and " : ", ");

/** The sentence after the names. */
export const indexingTail = (n: number): string =>
  n === 1 ? " is still indexing, so its newest files may be missing from answers."
    : " are still indexing, so their newest files may be missing from answers.";

/** The names of the repos that failed to index, in the cell's order. */
export const failedNames = (rows: AskRepoRow[]): string[] => reposOnBrain(rows).filter((r) => r.state === "failed").map((r) => r.repo);

/** The sentence after the names of failed repos. */
export const failedTail = " failed to index.";

/** The whole line, for a script: "a and b are still indexing, so their newest files may be missing from answers." */
export const indexingLine = (names: string[]): string =>
  names.length === 0 ? "" : names.map((n, i) => n + nameSeparator(i, names.length)).join("") + indexingTail(names.length);

/** "Answered in 6 seconds." from the milliseconds the cell measured, or the page's own clock when it did not say. */
export function answeredWords(ms: number | null | undefined, fallbackMs: number): string {
  const real = typeof ms === "number" && Number.isFinite(ms) && ms >= 0 ? ms : fallbackMs;
  return `Answered in ${durationWords(real / 1000)}.`;
}

const GITHUB_BLOB = /^https:\/\/github\.com\/([^/]+)\/([^/]+)\/blob\//;

/** The repo name (not the owner) a GitHub address names, or "". */
export const repoOfUrl = (url: string | null | undefined): string => GITHUB_BLOB.exec(url ?? "")?.[2] ?? "";

const isLine = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n > 0;

/** "12-20", "12" or "" for the cited lines. */
export function lineRange(c: Pick<BrainCitation, "start_line" | "end_line">): string {
  if (!isLine(c.start_line)) return "";
  return isLine(c.end_line) && c.end_line > c.start_line ? `${c.start_line}-${c.end_line}` : String(c.start_line);
}

/** Where a citation links: the address the brain gave when it is a web address; else, with one repo behind the
 *  answer and a file, the file on that repo's main branch, at the lines. Else nothing, and the source is plain text. */
export function sourceHref(c: BrainCitation, repos: string[]): string {
  const url = (c.url ?? "").trim();
  if (/^https?:\/\//i.test(url)) return url;
  if (repos.length !== 1 || !c.path || !/^[\w.-]+\/[\w.-]+$/.test(repos[0])) return "";
  const lines = lineRange(c);
  const [from, to] = lines.split("-");
  const frag = !from ? "" : to ? `#L${from}-L${to}` : `#L${from}`;
  return `https://github.com/${repos[0]}/blob/HEAD/${c.path.split("/").map(encodeURIComponent).join("/")}${frag}`;
}

/** What the source says, in mono: "repo/path/to/file.py:12-20". A note or a doc with no file says its title. */
export function sourceText(c: BrainCitation, repos: string[]): string {
  if (!c.path) return (c.title ?? "").trim() || "Untitled";
  const repo = repoOfUrl(c.url) || (repos.length === 1 ? repos[0].split("/")[1] : "");
  const lines = lineRange(c);
  return `${repo ? `${repo}/` : ""}${c.path}${lines ? `:${lines}` : ""}`;
}

/** The "[1]" in front of a source: the brain's own label, else its place in the list. */
export function sourceLabel(c: BrainCitation, i: number): string {
  const l = c.label === null || c.label === undefined ? "" : String(c.label).trim();
  return `[${l || i + 1}]`;
}

/** The answer text an API reply holds, trimmed, or "" when it is missing, blank or not text. */
export const answerText = (answer: unknown): string => (typeof answer === "string" ? answer.trim() : "");

/** A link an answer may become an anchor: http, https, mailto or a relative address. Anything else
 *  (javascript:, data:, a protocol-relative //host) is "" and stays plain text. */
export function safeHref(href: string | null | undefined): string {
  const h = String(href ?? "").trim();
  if (!h) return "";
  // browsers ignore tabs, line breaks and other control characters inside a scheme ("java\nscript:")
  const bare = h.replace(/[\u0000-\u0020\u007f]/g, "");
  if (bare.startsWith("//")) return "";
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(bare);
  if (!scheme) return h;
  return ["http", "https", "mailto"].includes(scheme[1].toLowerCase()) ? h : "";
}

/** The sentence an error from the cell becomes: a capital, a full stop. */
export function plainError(message: string | undefined, fallback: string): string {
  const t = (message ?? "").trim();
  if (!t) return fallback;
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".");
}

/** The words for a failed ask. `status` is the cell's HTTP status, or undefined when it never answered.
 *  A daily cap is said by which cap it was and when to come back; the cell's other words are kept.
 *  404 "Not Found" is the cell with no brain; a 404 with words is the brain's own (a repo it does not hold).
 *  402 and 403 will not pass by waiting, so they carry no "try again". */
export function askErrorWords(e: { message?: string; status?: number } | null | undefined, deep: boolean): string {
  const status = e?.status;
  if (status === undefined) return NETWORK_WORDS;
  const said = (e?.message ?? "").trim();
  if (status === 402) return NOT_AVAILABLE_WORDS;
  if (status === 403) return NO_ACCESS_WORDS;
  if (status === 404) return !said || /^not found\.?$/i.test(said) ? NOT_SET_UP_WORDS : `${plainError(said, "")} Try again in a moment.`;
  if (status === 429) {
    const cap = /has used its ([\d,]+) (deep answers|answers) for today/i.exec(said);
    if (cap) return `This cell has used its ${cap[1]} ${cap[2].toLowerCase()} for today. ${LIMIT_RESET_WORDS}`;
    if (/for today/i.test(said)) return deep ? `This cell has used its deep answers for today. ${LIMIT_RESET_WORDS}` : `This cell has used its answers for today. ${LIMIT_RESET_WORDS}`;
    return plainError(said, "The brain is busy. Try again in a moment.");
  }
  if (status === 502) return said ? `${plainError(said, "")} Try again in a moment.` : NOT_ANSWERED_WORDS;
  if (status === 400) return plainError(said, NOT_ANSWERED_WORDS);
  return NOT_ANSWERED_WORDS;
}
