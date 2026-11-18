// The two brain calls of the Ask page (ee/tares_ee/brain/ask.py and repos.py). The words in an error are
// the cell's own, worded for people; the answer and its citations are the brain's own text.

import { ApiError, request } from "../../api";

/** One entry of the answer's `citations`, as lore gives it. `url` is the address of the cited lines on
 *  GitHub (https://github.com/<owner>/<repo>/blob/<ref>/<path>#L12-L20), or null for a note or a doc. */
export interface BrainCitation {
  label: string | number | null;
  title?: string | null;
  kind?: string | null;
  /** The file, relative to its repo. */
  path: string | null;
  start_line: number | null;
  end_line: number | null;
  url: string | null;
}

/** 200 from POST /api/brain/ask when a repo was ready. */
export interface BrainAnswer {
  ready?: true;
  /** Markdown, the brain's own text. */
  answer: string;
  citations: BrainCitation[];
  /** The repos the answer drew on, `owner/name`. */
  repos: string[];
  /** How long the cell waited for the brain, in milliseconds. */
  duration_ms?: number;
}

/** 200 from POST /api/brain/ask when no repo is ready (it asks the brain nothing). */
export interface BrainNotReady {
  ready: false;
  message: string;
}

/** The repo rows of GET /api/brain that this page reads. */
export interface AskRepoRow {
  repo: string;
  state: "queued" | "cloning" | "indexing" | "ready" | "failed";
  removing?: boolean;
}

export interface AskBrainData {
  /** False when the cell has no brain settings. */
  configured: boolean;
  repos: AskRepoRow[];
}

/** The read of the repos. The shell makes it once (editions/brainRead.ts) and the page takes that read. A cell
 *  with no licence answers 404, as one that never had the route: that is "not set up", the same as
 *  `configured: false`. */
export async function askBrainRepos(): Promise<AskBrainData> {
  try {
    return await request<AskBrainData>("/api/brain");
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { configured: false, repos: [] };
    throw e;
  }
}

/** POST /api/brain/ask. Throws ApiError: 400 a question the cell refused (over 2,000 characters), 402 or 403 no
 *  right to the brain, 404 no brain ("Not Found") or a repo the brain does not hold (with words), 429 the day's cap (the cell says which, in words) or the brain's own limit, 502 the brain did not
 *  answer. A deep ask can take up to two minutes. */
export const askBrain = (question: string, deep: boolean) =>
  request<BrainAnswer | BrainNotReady>("/api/brain/ask", { method: "POST", body: JSON.stringify({ question, deep }) });
