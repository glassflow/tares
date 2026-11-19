// The two brain calls of the Connect a repo page (ee/tares_ee/brain/repos.py). Repo names are a
// person's own text, and the words in an error are the cell's own, worded for people.

import { ApiError, request } from "../../api";

/** One entry of GET /api/brain/repos/available: nothing else is in it (no file count, no branch). */
export interface AvailableRepo {
  /** `owner/name`, lowercase. */
  repo: string;
  /** GitHub says the repo is private. */
  private: boolean;
  /** The repo is already on the brain. */
  connected: boolean;
}

export interface AvailableData {
  /** False when the cell has no GitHub connection at all. */
  github: boolean;
  /** Every repo the connections can read, sorted by name. */
  repos: AvailableRepo[];
}

/** The repos the cell's GitHub connections can read. Null when the cell answers 404: no licence, or no
 *  brain settings, which is "the brain is not set up". A 502 (every connection failed) throws, with
 *  GitHub's words as the message. */
export async function availableRepos(): Promise<AvailableData | null> {
  try {
    return await request<AvailableData>("/api/brain/repos/available");
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

/** POST /api/brain/repos, admin only. 201 `{repos: [row]}` for each repo connected. The whole list is
 *  checked first: 400 for a name that is not owner/name, a repeat, none, or more than 50; 409 for a
 *  repo already connected; 502 when the brain failed part way (the message names who got connected). */
export const connectBrainRepos = (repos: string[], folders: Record<string, string[]> = {}) =>
  request<{ repos: { repo: string }[] }>("/api/brain/repos", {
    method: "POST",
    // only the repos sent, and only with folders: a repo left out gets none (TR-1454)
    body: JSON.stringify({ repos, folders: Object.fromEntries(repos.filter((r) => (folders[r] ?? []).length > 0).map((r) => [r, folders[r]])) }),
  });
