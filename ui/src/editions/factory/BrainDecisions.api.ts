// The decisions the brain holds (ee/tares_ee/brain/decisions_hand.py, TR-1212 and TR-1443): one list
// from every place, with counts, and adding or taking back a decision by hand. Titles, summaries and
// names are a person's own text.

import { request } from "../../api";

export type DecisionWhere = "repo_folder" | "factory_project" | "by_hand";
export type DecisionTab = "all" | DecisionWhere;
export type DocKind = "spec" | "plan" | "note";

export interface DecisionRow {
  /** The decision's id. A decision added by hand has one that starts with `hand:`; a repo file's is `<repo>:<path>`. */
  id: string;
  title: string | null;
  /** Only on a decision added by hand. */
  summary: string | null;
  where: DecisionWhere | null;
  decided_by: string[];
  changed_by: string[];
  /** An RFC 3339 time, or a bare day (YYYY-MM-DD) for a project doc. */
  decided_at: string | null;
  /** Only on a repo file. */
  changed_at: string | null;
  /** The GitHub file, the project doc (relative when the cell has no public URL), or null. */
  link: string | null;
  /** `owner/name` on a repo file. */
  repo: string | null;
  project: string | null;
  project_name: string | null;
  doc_kind: DocKind | null;
  added_by: string | null;
  can_delete: boolean;
}

export interface DecisionCounts { all: number; repo_folder: number; factory_project: number; by_hand: number }

/** What the dropdowns offer: every repo and project with a decision, whatever the filters say. */
export interface DecisionFilters { repos: string[]; projects: { id: string; name: string }[] }

export interface DecisionsPage {
  decisions: DecisionRow[];
  next_cursor: string | null;
  counts: DecisionCounts;
  filters?: DecisionFilters;
}

export interface NewDecision {
  title: string;
  text?: string;
  file?: { name: string; content_base64: string };
  /** A person's id; left out, it is the person adding it. */
  decided_by?: string;
}

export const PAGE_SIZE = 25;

/** One page of the list. `where` "all" is no filter. A 404 means the cell has no brain. */
export function brainDecisions(opts: { where?: DecisionTab; repo?: string | null; project?: string | null; cursor?: string | null } = {}): Promise<DecisionsPage> {
  const q = new URLSearchParams();
  if (opts.where && opts.where !== "all") q.set("where", opts.where);
  if (opts.repo) q.set("repo", opts.repo);
  if (opts.project) q.set("project", opts.project);
  if (opts.cursor) q.set("cursor", opts.cursor);
  q.set("limit", String(PAGE_SIZE));
  return request<DecisionsPage>(`/api/brain/decisions?${q.toString()}`);
}

export const addDecision = (body: NewDecision) =>
  request<DecisionRow>("/api/brain/decisions", { method: "POST", body: JSON.stringify(body) });

export const deleteDecision = (id: string) =>
  request<{ ok: boolean; removed: string }>(`/api/brain/decisions/${encodeURIComponent(id)}`, { method: "DELETE" });
