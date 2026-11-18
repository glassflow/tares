// The words and small rules of decision folders (TR-1454) and of a repo's update history (TR-1463), kept
// apart from the pages so a script can run them without a browser. No JSX and no imports beyond lib/.

import { formatCount } from "../../lib/number";
import { ageWords } from "../../lib/time";

export const FOLDERS_LEGEND = "Folders with decisions";
export const LOOKING_WORDS = "Looking for decision folders…";
export const NONE_FOUND_WORDS = "No decision folders found. Add one if your decisions live in the repo.";
export const PARTIAL_WORDS = "This repo is big, so Tares only checked the usual folder names.";
export const ADD_FOLDER_LABEL = "Add another folder";
export const FOUND_BY = "found by Tares";
export const REMOVING_409 = "This repo is being removed.";
export const SAVE_FAILED = "The folders were not saved. Try again.";
export const NOT_ADMIN_403 = "Only an admin can change the decision folders.";
export const MAX_FOLDERS = 50;
export const MAX_PATH = 200;

/** "12 files", "1 file". */
export const filesWords = (n: number | null | undefined): string =>
  typeof n === "number" && Number.isFinite(n) ? `${formatCount(n)} ${n === 1 ? "file" : "files"}` : "";

/** The line under a found folder: "12 files, found by Tares". */
export const foundWords = (files: number): string => [filesWords(files), FOUND_BY].filter(Boolean).join(", ");

/** A typed folder as the cell stores it (no slash at either end), or a plain sentence saying what is wrong.
 *  The same rules as `check_folder` in ee/tares_ee/brain/folders.py. */
export function checkFolder(value: string, have: readonly string[]): { path: string } | { problem: string } {
  const path = value.trim().replace(/^\/+|\/+$/g, "");
  const parts = path.split("/");
  const control = [...path].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127);
  if (!path) return { problem: "Write a folder, like docs/adr." };
  if (path.length > MAX_PATH) return { problem: `Keep the folder to ${MAX_PATH} characters.` };
  if (path.includes("\\") || control || parts.some((p) => p === "" || p === "." || p === "..")) {
    return { problem: "Write a folder inside the repo, like docs/adr." };
  }
  if (have.includes(path)) return { problem: `${path} is already in the list.` };
  if (have.length >= MAX_FOLDERS) return { problem: `Choose at most ${MAX_FOLDERS} folders for a repo.` };
  return { path };
}

/** The Decisions line of a Ready card. With folders: the count and where from; without: none. */
export function decisionsWhere(folders: readonly string[]): string {
  if (folders.length === 0) return "";
  return folders.length === 1 ? folders[0] : `${formatCount(folders.length)} folders`;
}
export const NO_DECISIONS_WORDS = "none.";
export const PICK_FOLDER_WORDS = "Pick a folder if your decisions live in this repo.";

/** Where a repo's decisions are listed: the Decisions page with Origin GitHub repos and this repo. */
export const decisionsHref = (repo: string) => `/brain/decisions?where=repo_folder&repo=${encodeURIComponent(repo)}`;

/** The commit on GitHub, from `owner/name` and a full commit id; "" for anything else. */
export function commitHref(repo: string, commit: string | null | undefined): string {
  const r = (repo ?? "").trim();
  const c = (commit ?? "").trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r) || !/^[0-9a-f]{7,64}$/i.test(c)) return "";
  return `https://github.com/${r.split("/").map(encodeURIComponent).join("/")}/commit/${c}`;
}

// ---- the update history

export const HISTORY_TITLE = "Index history";
export const HISTORY_LINE = "The first index and every update after a push to main, newest first.";
export const HISTORY_EMPTY = "Nothing yet. The first index shows here once it ends.";
export const HISTORY_FAILED = "The history did not load. Try again.";

/** What one entry did, in a line: "First index, 1,507 files", "Update, 3 files changed, 1 deleted". */
export function updateWords(u: { kind: string; ok: boolean; files_changed: number; files_deleted: number; files_total: number }): string {
  const what = u.kind === "index" ? "First index" : "Update";
  if (!u.ok) return `${what} did not finish`;
  if (u.kind === "index") return u.files_total > 0 ? `${what}, ${filesWords(u.files_total)}` : what;
  const parts = [`${what}, ${filesWords(u.files_changed)} changed`];
  if (u.files_deleted > 0) parts.push(`${formatCount(u.files_deleted)} deleted`);
  return parts.join(", ");
}

/** "Last updated 22 minutes ago" for a Ready card, from the newest entry or the last sync time. */
export function updatedWords(at: number, now: number): string {
  if (!Number.isFinite(at)) return "";
  return `Updated ${ageWords(Math.max(0, now - at))}`;
}
