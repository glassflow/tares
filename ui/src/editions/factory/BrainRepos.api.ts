// The body of GET /api/brain on a cell with a licence (ee/tares_ee/brain/repos.py, row_view). The
// words in `error`, `sync_error` and `remove_error` are worded by the cell for people and shown as
// they are. Repo names and the name of who added a repo are a person's own text.

import { ApiError, request } from "../../api";

export type BrainState = "queued" | "cloning" | "indexing" | "ready" | "failed";

export interface BrainRepoRow {
  /** `owner/name`, lowercase. */
  repo: string;
  state: BrainState;
  files_done: number;
  files_total: number;
  /** The commit the index is at, once a first index finished. */
  indexed_commit: string | null;
  /** When the index last caught up with main (ISO time). */
  last_synced: string | null;
  /** Why the index failed (state "failed"), in plain words. */
  error: string | null;
  /** Where the repo stands in the control plane's queue, while it waits. */
  place: number | null;
  /** Updates from main that failed in a row; a ready repo stays ready. */
  sync_failures: number;
  sync_error: string | null;
  added_by: string | null;
  added_by_name: string | null;
  /** When the repo was connected (ISO time). */
  added_at: string | null;
  /** A removal is running in the background. */
  removing: boolean;
  /** Why the last removal gave up; null otherwise. */
  remove_error: string | null;
}

export interface BrainReposData {
  /** False when the cell has no brain settings. */
  configured: boolean;
  repos: BrainRepoRow[];
}

/** 200 `{ok, removed}` is gone; 202 `{ok, removing}` is a removal running in the background. */
export interface BrainRemoveAnswer { ok: boolean; removed?: boolean; removing?: boolean }

const repoPath = (repo: string) => `/api/brain/repos/${repo.split("/").map(encodeURIComponent).join("/")}`;

/** The read. A cell with no licence answers 404, as one that never had the route: that is "not set up". */
export async function brainRepos(): Promise<BrainReposData> {
  try {
    return await request<BrainReposData>("/api/brain");
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { configured: false, repos: [] };
    throw e;
  }
}

/** `request` answers any 2xx as the body, so a 202 comes back as `{ok, removing: true}`, not an error. */
export const removeBrainRepo = (repo: string) =>
  request<BrainRemoveAnswer>(repoPath(repo), { method: "DELETE" });

export const retryBrainRepo = (repo: string) =>
  request<BrainRepoRow>(`${repoPath(repo)}/retry`, { method: "POST" });
