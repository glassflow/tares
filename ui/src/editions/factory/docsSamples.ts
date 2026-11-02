// Sample data for the Docs and memory page, shown at /docs?sample=<name> when the browser has the
// sample flag (see sample.ts). It is invented: project-x, project-y, service-a, Alex, Kim, Lee.

import type { DocLink, DocProject, DocRow, DocView, DocsPage } from "./FactoryDocs.api";

export const DUP_LINE = "Expand the questions before the checks, not after.";

const li = (text: string, by = "", when = ""): DocRow => ({ kind: "li", text, by, when });
const p = (text: string): DocRow => ({ kind: "p", text, by: "", when: "" });
const h2 = (text: string): DocRow => ({ kind: "h2", text, by: "", when: "" });

const link = (id: string, label: string): DocLink => ({ id, kind: label.toLowerCase(), label });
const project = (id: string, name: string, docs: DocLink[]): DocProject => ({ id, name, docs });

const LIST: Omit<DocsPage, "doc" | "empty"> = {
  shared: [
    { ...link("rules", "Rules"), title: "Rules for every project" },
    { ...link("mem", "Memory"), title: "Memory for every project" },
  ],
  projects: [
    project("px", "project-x", [link("xspec", "Spec"), link("xplan", "Plan"), link("xrules", "Rules"), link("xmem", "Memory")]),
    project("py", "project-y", [link("yspec", "Spec"), link("yplan", "Plan"), link("yrules", "Rules"), link("ymem", "Memory")]),
    project("sa", "service-a", [link("aspec", "Spec"), link("aplan", "Plan")]),
  ],
  finished: ["a", "b", "c", "d"].map((x) => project(`f${x}`, `project-${x}`, [link(`f${x}spec`, "Spec")])),
};

const ADD = (hint: string, path: string) => ({ path, hint });
const THE_CREW = "The crew reads it from the next task.";

const MEMORY_LINES = [
  li("Articles over 600 words fail the length check; split them.", "the Builder", "12 days ago"),
  li(DUP_LINE, "the Reviewer", "9 days ago"),
  li("The first wave needs a yes on each pull request the day it opens.", "you", "6 days ago"),
  li("Answer articles keep one fact per sentence so an assistant can quote them.", "the Builder", "3 days ago"),
];

const LONG_LINE =
  "When a wave pull request touches more than 100 articles at once, split it by topic, check each part, " +
  "post the failures as a list under the pull request, tag each failing article with its question id, " +
  "wait for the writer to redo only those, and then merge the parts in the order of the index so that " +
  "related links never point at an article that is not live yet.";

const ROLES = ["the Builder", "you", "the Reviewer", "the Lead"];
const BIG_LINES = Array.from({ length: 2000 }, (_, i) => {
  const days = Math.max(1, Math.round((2000 - i) / 9));
  return li(`Memory line ${i + 1}: a fact kept for the crew.`, ROLES[i % 4], `${days} ${days === 1 ? "day" : "days"} ago`);
});

const doc = (id: string, kind: string, label: string, title: string, changed: string, lines: DocRow[],
             add: DocView["add"], proj: DocView["project"] = null): DocView => ({
  id, kind, label, title, project: proj, shared: proj === null, changed, empty: lines.length === 0, lines, add,
});
const X = { id: "px", name: "project-x" };
const Y = { id: "py", name: "project-y" };

const DOCS: Record<string, DocView> = {
  rules: doc("rules", "agents", "Rules", "Rules for every project", "Changed by the Lead, 5 days ago", [
    p("Rules every session follows in every project on this Tares. One line each; a project's own rules add to these."),
    li("In service-a, check for a missing test before asking for review: it came back in 4 tickets."),
    li("Stop only the processes you started yourself, by the pid you recorded; never kill by name or pattern."),
    li("Keep each pull request to one ticket, and name the ticket in the title."),
    li("Check for null or empty input before asking for review: it came back in 8 tickets."),
    li("Check for security before asking for review: it came back in 5 tickets."),
  ], ADD(`Add a line to the shared rules. ${THE_CREW}`, "/api/docs/global/agents/lines")),
  mem: doc("mem", "memory", "Memory", "Memory for every project", "Nothing here yet.", [],
           ADD(`Add a line to the shared memory. ${THE_CREW}`, "/api/docs/global/memory/lines")),
  xplan: doc("xplan", "plan", "Plan", "Plan: project-x", "Changed by you, 14 days ago", [
    p("Three milestones, in order, all in `service-a`. Tickets live in Linear project P-1. Ready at the start: T1 and T2."),
    h2("M1: Sitemap and index live"),
    p("Goal: the site has a sitemap and an empty index page."),
    li("T1 Add a sitemap file for the site"),
    li("T2 Add the index page (held)"),
    h2("M2: Checks gate CI"),
    p("Goal: the question bank, the checks and the writer exist and are tested; CI checks every article."),
    li("T3 Scaffold the answers tooling"),
    li("T4 Question bank format and expander (after T3)"),
    li("T5 Blocking article checks (after T3, T4)"),
    h2("M3: First 20 articles live"),
    p("Goal: the seed questions are approved, and the first wave of 20 is live."),
    p("Total spend cap $200, ask at $160."),
  ], null, X),
  xmem: doc("xmem", "memory", "Memory", "Memory: project-x", "Changed by the Builder, 3 days ago", MEMORY_LINES,
            ADD(`Add a line to this memory. ${THE_CREW}`, "/api/projects/px/memory/lines"), X),
  long: doc("xmem", "memory", "Memory", "Memory: project-x", "Changed by the Builder, 3 days ago",
            [li(LONG_LINE, "the Lead", "1 day ago"), ...MEMORY_LINES],
            ADD(`Add a line to this memory. ${THE_CREW}`, "/api/projects/px/memory/lines"), X),
  ymem: doc("ymem", "memory", "Memory", "Memory: project-y", "Changed by the Reviewer, 2 hours ago", BIG_LINES,
            ADD(`Add a line to this memory. ${THE_CREW}`, "/api/projects/py/memory/lines"), Y),
};

type Sample = { doc: string; prefill?: string; error?: string };

/** The samples: the document each opens, and for `dup` the line already typed and the answer to it. */
const SAMPLES: Record<string, Sample | "empty" | "load" | "err"> = {
  busy: { doc: "rules" },
  plan: { doc: "xplan" },
  pmem: { doc: "xmem" },
  long: { doc: "long" },
  big: { doc: "ymem" },
  dup: { doc: "xmem", prefill: DUP_LINE, error: "That line is already there." },
  empty: "empty",
  load: "load",
  err: "err",
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** The line a `dup` sample has typed in, and the answer it shows, or none. */
export function sampleBox(name: string): { prefill: string; error: string } {
  const s = SAMPLES[name];
  return typeof s === "object" && s.prefill ? { prefill: s.prefill, error: s.error ?? "" } : { prefill: "", error: "" };
}

/** What a read of the page answers for a sample: the page, never an answer (`load`), or an error
 *  (`err`). `docId` is the document the address asks for; one the samples do not have opens the
 *  sample's own. */
export function sampleLoad(name: string, docId: string | null): Promise<DocsPage> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  if (s === "empty") return Promise.resolve({ empty: true, shared: LIST.shared, projects: [], finished: [], doc: null });
  const open = docId && DOCS[docId] ? DOCS[docId] : DOCS[s.doc];
  return Promise.resolve({ empty: false, ...LIST, doc: open });
}
