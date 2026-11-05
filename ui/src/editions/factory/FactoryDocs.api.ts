// The body of GET /api/factory/docs and GET /api/factory/docs/{id} on a factory cell
// (tares/screens/factory_docs.py). Every string is worded for people and shown as it is. A document's
// own text (`lines[].text`) is markdown written by people and the crew.

import { request } from "../../api";

export type DocRowKind = "h1" | "h2" | "h3" | "li" | "p";

/** One row of a document. `by` and `when` are set only for a line of a project's memory. */
export interface DocRow {
  kind: DocRowKind;
  text: string;
  by: string;
  when: string;
}

export interface DocLink {
  id: string;
  kind: string;
  label: string;
}

/** One open document. `add` says where a line is added (null when the page does not add to it). */
export interface DocView {
  id: string;
  kind: string;
  label: string;
  title: string;
  project: { id: string; name: string } | null;
  shared: boolean;
  changed: string;
  empty: boolean;
  lines: DocRow[];
  add: { path: string; hint: string } | null;
}

export interface DocProject {
  id: string;
  name: string;
  docs: DocLink[];
}

/** The whole page in one read: the list, and `doc` (null when the cell has no document yet). */
export interface DocsPage {
  empty: boolean;
  shared: (DocLink & { title: string })[];
  projects: DocProject[];
  finished: DocProject[];
  doc: DocView | null;
}

/** The page with the first shared document open. */
export const factoryDocs = () => request<DocsPage>("/api/factory/docs");

/** The page with this document open. A document the page does not show is a 404. */
export const factoryDoc = (id: string) => request<DocsPage>(`/api/factory/docs/${encodeURIComponent(id)}`);

/** Adds one line through the route a document names in `add.path`. `added` is false for a line
 *  that is already there. */
export const addDocLine = (path: string, text: string) =>
  request<{ added?: boolean }>(path, { method: "POST", body: JSON.stringify({ text, by: "console" }) });

/** One document on a project's own docs page; a working note names its ticket. */
export interface ProjectDocLink {
  id: string;
  kind: string;
  label: string;
  title: string;
  updated: string;
  ticket?: string | null;
  ticket_href?: string | null;
}

/** GET /api/factory/projects/{uid}/docs: every document of one project, grouped, and one open. */
export interface ProjectDocsPage {
  project: { id: string; name: string };
  groups: { key: "project" | "working" | "notes"; title: string; docs: ProjectDocLink[] }[];
  total: number;
  doc: DocView | null;
}

export const projectDocs = (project: string, doc?: string | null) =>
  request<ProjectDocsPage>(`/api/factory/projects/${encodeURIComponent(project)}/docs${doc ? `?doc=${encodeURIComponent(doc)}` : ""}`);
