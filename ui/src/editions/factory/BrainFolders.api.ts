// The decision folders of a repo (ee/tares_ee/brain/repos.py, TR-1209, TR-1441) and a repo's history of
// indexes and updates (TR-1463). Paths, names and the cell's error words are a person's or the cell's own.

import { request } from "../../api";

/** A folder Tares found in the repo's git tree. */
export interface SuggestedFolder {
  path: string;
  /** Markdown files in it. */
  files: number;
  /** "known" (a usual name like docs/adr) or "numbered" (files named like 0007-title.md). */
  why: string;
  /** Already chosen for this repo. */
  chosen: boolean;
}

export interface ChosenFolder {
  path: string;
  chosen_by: string | null;
  chosen_by_name: string | null;
  chosen_at: string | null;
  /** Markdown files in it, or null when GitHub could not tell. */
  files: number | null;
}

export interface FolderScan {
  repo: string;
  folders: ChosenFolder[];
  suggested: SuggestedFolder[];
  /** GitHub cut a huge tree short: only the usual names were looked at. */
  partial: boolean;
  /** False when GitHub could not be asked; `scan_error` says why, in plain words. */
  scanned: boolean;
  scan_error: string | null;
}

const repoPath = (repo: string) => repo.split("/").map(encodeURIComponent).join("/");

/** Suggestions for a repo not connected yet (admin only). Can take up to 45 seconds on a big repo. */
export const availableFolders = (repo: string) =>
  request<FolderScan>(`/api/brain/repos/available/${repoPath(repo)}/folders`);

/** The chosen folders of a connected repo, and the suggestions. Anyone may read it. */
export const repoFolders = (repo: string) =>
  request<FolderScan>(`/api/brain/repos/${repoPath(repo)}/folders`);

/** The full new list of a connected repo's folders (admin only). `[]` clears it. */
export const saveRepoFolders = (repo: string, folders: string[]) =>
  request<{ repo: string; folders: ChosenFolder[]; added: string[]; removed: string[] }>(
    `/api/brain/repos/${repoPath(repo)}/folders`, { method: "PUT", body: JSON.stringify({ folders }) });

/** One first index or later update of a repo. */
export interface RepoUpdate {
  at: string;
  /** "index": a first index (or one after a Retry); "update": one that followed a push to main. */
  kind: "index" | "update" | string;
  ok: boolean;
  indexed_commit: string | null;
  files_changed: number;
  files_deleted: number;
  /** The repo's file count after it. */
  files_total: number;
  error: string | null;
}

/** The repo's history, newest first, at most 20. */
export const repoUpdates = (repo: string) =>
  request<{ repo: string; updates: RepoUpdate[] }>(`/api/brain/repos/${repoPath(repo)}/updates`);
