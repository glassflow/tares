// Sample data for the Projects list, shown at /projects?sample=<name> when the browser has the
// sample flag (see sample.ts). It is invented: project-x, project-y, example-org/service-a, Alex, Kim.

import type { FactoryProjectRow, FactoryProjectsData, ProjectState } from "./FactoryProjects.api";

const NOW = "2026-11-02T10:00:00+00:00";
const LABEL: Record<ProjectState, string> = {
  building: "Building", waiting_on_you: "Waiting on you", not_started: "Not started",
  ready_to_close: "Ready to close", done: "Done",
};
const A = "example-org/service-a";
const B = "example-org/service-b";
const C = "example-org/docs";

interface Spec {
  id: string; name: string; state: ProjectState; goal: string; merged: number; total: number;
  blocked?: number; milestone?: string; repos?: string[]; builders?: number; meta?: string;
}

const row = (s: Spec): FactoryProjectRow => ({
  id: s.id, name: s.name, goal: s.goal, state: s.state, state_label: LABEL[s.state],
  milestone: s.milestone ?? "", tickets: { merged: s.merged, blocked: s.blocked ?? 0, total: s.total },
  repos: (s.repos ?? [A]).map((r) => r.split("/").pop() ?? r), builders: s.builders ?? 0,
  last_activity_at: NOW, last_activity: null, meta: s.meta ?? "",
  can_close: s.state === "ready_to_close", repo_names: s.repos ?? [A],
});

const BUSY: Spec[] = [
  { id: "p1", name: "project-x", state: "waiting_on_you", goal: "Publish 1,000 answer articles on the docs site.",
    merged: 15, total: 21, blocked: 4, milestone: "Now on M4: 1,000 articles live", repos: [C], builders: 1,
    meta: "1 builder · Active 1 day ago · 2 PRs to review" },
  { id: "p2", name: "project-y", state: "building", goal: "Give the console an edition for each kind of team.",
    merged: 34, total: 61, blocked: 23, milestone: "Now on 1. Foundations", repos: [A, B, C], builders: 3,
    meta: "3 builders · Active 1 minute ago" },
  { id: "p3", name: "project-z", state: "not_started", goal: "Let customers connect their own tools, with a setup page for each.",
    merged: 0, total: 15, blocked: 11, milestone: "6 milestones", repos: [A], meta: "No builders yet · Written 5 days ago" },
  { id: "p4", name: "project-v", state: "ready_to_close", goal: "Let a team share one crew.", merged: 27, total: 27,
    milestone: "All milestones done", repos: [A, B, C], meta: "No builders working · Merged 3 days ago" },
  { id: "p5", name: "project-a", state: "done", goal: "Track how often AI assistants name the product.", merged: 15, total: 15,
    milestone: "Finished 20 Oct", meta: "" },
  { id: "p6", name: "project-b", state: "done", goal: "Let the crew take its tickets from a shared issue list.", merged: 8, total: 8,
    milestone: "Finished 20 Oct" },
  { id: "p7", name: "project-c", state: "done", goal: "Install the product with one command.", merged: 6, total: 6,
    milestone: "Finished 14 Oct" },
  { id: "p8", name: "project-d", state: "done", goal: "Make the docs easy for AI assistants to quote.", merged: 9, total: 9,
    milestone: "Finished 14 Oct" },
];

const STATES: ProjectState[] = ["building", "waiting_on_you", "not_started", "ready_to_close", "done", "building"];
const MANY: Spec[] = Array.from({ length: 60 }, (_, i) => {
  const state = STATES[i % 6];
  const total = 20;
  const merged = state === "ready_to_close" || state === "done" ? total : state === "not_started" ? 0 : (i * 3) % 20;
  return {
    id: `m${i}`, name: `project-${i + 1}`, state, goal: `Short goal sentence for project ${i + 1}.`, merged, total,
    blocked: state === "ready_to_close" || state === "done" ? 0 : i % 5,
    milestone: state === "done" ? "Finished 20 Oct" : state === "ready_to_close" ? "All milestones done" : `Now on M${1 + (i % 4)}`,
    builders: state === "building" ? 1 : 0, meta: state === "done" ? "" : `${i + 1} hours ago`,
  };
});

const LONG_GOAL =
  "Move every answer article off the old docs template, check each against the fact sheet, rewrite the ones that fail, " +
  "and keep the whole library under review so that a new reader finds the same answer in the docs, in search and in an " +
  "assistant, with the dates current and nothing that contradicts the product as it ships today, for all 1,000 articles, " +
  "and then do the same for the how-to guides, one section at a time until the last one is done.";   // 300+ characters
const LONG: Spec[] = [
  { id: "l1", name: "project-with-a-very-long-name-that-goes-on-and-on-without-a-break-1",
    state: "building", goal: LONG_GOAL, merged: 3, total: 15, blocked: 11,
    milestone: "Now on M1: A very long milestone name that should wrap and never push the bar out of its card",
    repos: [A, B, C, "example-org/another-service-with-a-long-name"], builders: 4,
    meta: "4 builders · Active 12 minutes ago" },
  ...BUSY.slice(0, 2),
];

const READY: Spec[] = [
  BUSY[3],
  { id: "p9", name: "project-w", state: "ready_to_close", goal: "Prepare the workshop.", merged: 19, total: 19,
    milestone: "All milestones done", meta: "No builders working · Merged 10 days ago" },
];

const data = (specs: Spec[]): FactoryProjectsData => {
  const projects = specs.map(row);
  const n = (s: ProjectState) => projects.filter((p) => p.state === s).length;
  return {
    projects,
    counts: { all: projects.length, building: n("building"), waiting_on_you: n("waiting_on_you"),
              not_started: n("not_started"), ready_to_close: n("ready_to_close"), done: n("done") },
    now: NOW,
  };
};

const SAMPLES: Record<string, Spec[] | "load" | "err"> = {
  busy: BUSY, empty: [], many: MANY, long: LONG, ready: READY, dialog: BUSY, fail: BUSY, load: "load", err: "err",
};

/** The Close a sample has part way: `dialog` has the confirm open on project-v, `fail` has the answer
 *  to a Close already shown under that row. */
export function sampleClose(name: string): { open: string; row: string; error: string } {
  if (name === "dialog") return { open: "p4", row: "", error: "" };
  if (name === "fail") return { open: "", row: "p4", error: "2 pull requests of this project are still open. Merge or close them first." };
  return { open: "", row: "", error: "" };
}

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** What a read of the page answers for a sample: the data, never an answer (`load`), or an error (`err`). */
export function sampleLoad(name: string): Promise<FactoryProjectsData> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(data(s));
}

/** A sample has no cell to close a project on: it moves the row to Done on this copy of the data. */
export function sampleClosed(d: FactoryProjectsData, id: string): FactoryProjectsData {
  const projects = d.projects.map((p) => p.id !== id ? p
    : { ...p, state: "done" as const, state_label: LABEL.done, milestone: "Finished 2 Nov", can_close: false, meta: "" });
  const n = (s: ProjectState) => projects.filter((p) => p.state === s).length;
  return { ...d, projects, counts: { ...d.counts, ready_to_close: n("ready_to_close"), done: n("done") } };
}
