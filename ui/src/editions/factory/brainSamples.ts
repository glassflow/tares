import type { BrainReposData, BrainRepoRow } from "./BrainRepos.api";

// Sample bodies of the Repos page, in the shape of GET /api/brain. They show only behind the sample
// flag (src/editions/sample.ts), never on a live cell. Every name here is invented. Times are
// relative to NOW, which the page uses as the clock in a sample, so the words never drift.

export const SAMPLE_NOW = "2026-11-12T10:00:00Z";
const NOW = Date.parse(SAMPLE_NOW);
const before = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

const base: BrainRepoRow = {
  repo: "glassflow/tares", state: "ready", files_done: 0, files_total: 0, indexed_commit: null, last_synced: null,
  error: null, place: null, sync_failures: 0, sync_error: null, added_by: "p1", added_by_name: "Ashish Bagri",
  added_at: before(60), removing: false, remove_error: null,
};
const row = (over: Partial<BrainRepoRow>): BrainRepoRow => ({ ...base, ...over });

const queued = row({ repo: "glassflow/glassflow-docs", state: "queued", place: 1, added_at: before(9) });
const queuedThird = row({ repo: "glassflow/tares-site-v2", state: "queued", place: 3, added_at: before(6) });
const cloning = row({ repo: "glassflow/tares-site", state: "cloning", added_at: before(2) });
const indexing = row({ repo: "glassflow/tares", state: "indexing", files_done: 812, files_total: 1816, added_at: before(14) });
const ready = row({
  repo: "glassflow/tares-cloud", state: "ready", files_done: 640, files_total: 640,
  indexed_commit: "8c41d2e9a7b0c3d4e5f60718293a4b5c6d7e8f90", last_synced: before(2), added_at: before(300),
});
const syncing = row({
  repo: "glassflow/fernpage", state: "ready", files_done: 212, files_total: 212,
  indexed_commit: "3fa91b07c2d84e5f6a7b8c9d0e1f2a3b4c5d6e7f", last_synced: before(95), added_at: before(2000),
  sync_failures: 3, sync_error: "GitHub did not answer when the brain asked for the latest push.",
});
const failed = row({
  repo: "glassflow/factory-test-api", state: "failed", added_at: before(30),
  error: "GitHub refused access while cloning. Give the GitHub connection read access to this repo, then try again.",
});
const removing = row({ repo: "glassflow/old-prototype", state: "ready", files_done: 90, files_total: 90, indexed_commit: "1a2b3c4d5e6f", last_synced: before(500), added_at: before(4000), removing: true });
const removeFailed = row({
  repo: "glassflow/old-prototype", state: "ready", files_done: 90, files_total: 90, indexed_commit: "1a2b3c4d5e6f", last_synced: before(500), added_at: before(4000),
  remove_error: "Still indexing. Try again in a minute.",
});

const data = (...repos: BrainRepoRow[]): BrainReposData => ({ configured: true, repos });

export const BRAIN_SAMPLES: Record<string, BrainReposData> = {
  queued: data(indexing, queued, queuedThird),
  "queued-idle": data(row({ ...queued, repo: "glassflow/glassflow-docs" })),
  cloning: data(cloning),
  indexing: data(indexing),
  ready: data(ready),
  "ready-sync-failing": data(syncing),
  failed: data(failed),
  removing: data(removing),
  "remove-failed": data(removeFailed),
  all: data(indexing, ready, queued, queuedThird, failed, syncing, removing, row({ ...removeFailed, repo: "glassflow/old-docs-site" }), cloning),
  empty: { configured: true, repos: [] },
  "not-set-up": { configured: false, repos: [] },
};

/** The sample names the page understands, besides `loading` and `error`. */
export const isBrainSample = (name: string | null): name is string =>
  name === "loading" || name === "error" || (name !== null && Object.prototype.hasOwnProperty.call(BRAIN_SAMPLES, name));
