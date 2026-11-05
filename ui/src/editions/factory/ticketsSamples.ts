// Sample data for the Tickets page, shown at /tickets?sample=<name> when the browser has the sample
// flag (see sample.ts). It is invented: project-y, project-z, docs-site, example-org/docs-site.
// A sample answers the page's read the way the cell does: it filters, sorts, pages and
// counts the same invented tickets, so the filters, the layouts and Show more all work on it.

import type { FactoryTicketsData, StateKey, TicketRow, TicketsQuery } from "./FactoryTickets.api";

const NOW = "2026-11-02T10:00:00+00:00";
const REPO = "example-org/docs-site";

type Kind = "todo" | "prog" | "review" | "changes" | "blocked" | "merged" | "canceled";
const KEY: Record<Kind, StateKey> = {
  todo: "to_do", prog: "in_progress", review: "in_review", changes: "changes_asked",
  blocked: "blocked", merged: "merged", canceled: "canceled",
};
const WORD: Record<Kind, string> = {
  todo: "to do", prog: "in progress", review: "in review", changes: "changes asked",
  blocked: "blocked", merged: "merged", canceled: "canceled",
};

interface Project { id: string; name: string; milestones: string[]; tag: string }
const PROJECTS: Project[] = [
  { id: "project-y", name: "project-y", tag: "Y", milestones: ["Faster lists", "Cleaner reviews", "Launch"] },
  { id: "project-z", name: "project-z", tag: "Z", milestones: ["First batch of answers", "Search and tags"] },
  { id: "docs-site", name: "docs-site", tag: "DS", milestones: ["Move the content", "New navigation", "Switch over"] },
];

/** [title, state, milestone index, blocked by (ref numbers), minutes since the last change] */
type Spec = [string, Kind, number, number[]?, number?];

const DOCS: Spec[] = [
  ["Inventory of every page on the old site", "merged", 0], ["Pick the new site layout", "merged", 0],
  ["Copy the install pages over", "merged", 0], ["Copy the guides over", "merged", 0],
  ["Check every copied link", "merged", 0], ["Move the images and shrink them", "merged", 0],
  ["Redirect list for the old addresses", "merged", 0], ["Search box on the new site", "merged", 0],
  ["Dark mode for the new site", "merged", 0], ["Footer with the support links", "merged", 0],
  ["Version picker for the docs", "merged", 1], ["Print layout for guides", "merged", 1],
  ["Page titles and descriptions", "merged", 1], ["Sitemap for the new site", "merged", 1],
  ["Sidebar groups for the install pages", "changes", 1, [], 55], ["Keyboard shortcuts for the search box", "changes", 1, [], 190],
  ["Sidebar groups for the guides", "review", 1, [], 240], ["Breadcrumbs on every page", "review", 1, [], 95],
  ["Previous and next links on guides", "review", 1, [], 410], ["Move the API reference over", "prog", 1, [], 20],
  ["Code samples with a copy button", "prog", 1, [], 50], ["Feedback box on every page", "prog", 1, [], 165],
  ["Switch the docs address to the new site", "blocked", 2, [20], 900], ["Announce the new site to customers", "blocked", 2, [23], 900],
  ["Turn off the old site", "blocked", 2, [23], 900], ["Archive the old site for a year", "blocked", 2, [25], 900],
  ["Weekly check for broken links", "todo", 2, [], 3000], ["Add the release notes page", "todo", 2, [], 3000],
  ["Translate the install pages into German", "todo", 2, [], 3000], ["Landing page for the new site", "todo", 2, [], 3000],
  ["Use a second search provider", "canceled", 1, [], 8000], ["Comment box on every page", "canceled", 1, [], 8000],
];

const YS: Spec[] = [
  ["Add a bench script for the list endpoints", "merged", 0], ["Batch the review queue", "merged", 0],
  ["Show project progress from one query", "merged", 0], ["Cache the project list for a minute", "merged", 0],
  ["Keep the last review of each ticket with the ticket", "review", 0, [], 310], ["Page the tickets list by 25", "review", 0, [], 125],
  ["Turn the review wording into one place", "changes", 1, [], 35], ["Show who is waiting on a review", "prog", 1, [], 1500],
  ["Link a review to its pull request", "prog", 1, [], 2000], ["Round counts for the reviews page", "todo", 1, [], 1500],
  ["Write the guide for the new list", "todo", 2, [], 5760], ["Announce the faster lists", "blocked", 2, [11], 5760],
];

const ZS: Spec[] = [
  ["Write the first ten answers", "merged", 0], ["Check the first ten answers", "merged", 0],
  ["Tag the answers by product area", "merged", 0], ["Add a last checked date to each answer", "review", 0, [], 4800],
  ["Fix up the older answers flagged in two reviews", "changes", 0, [], 5300], ["Write the second ten answers", "prog", 0, [], 5800],
  ["Search page for the answers", "prog", 1, [], 6300], ["Related answers at the foot of each answer", "todo", 1, [], 6800],
  ["Answers by tag", "blocked", 1, [8], 7300], ["Export the answers as one file", "canceled", 1, [], 7800],
];

const LONG_TITLES = [
  "Show a last checked date and the name of the person who checked it on each answer, with a tooltip that explains what checked means, and keep it in the printed version and in the exported PDF",
  "Fix the typo on the answers landing page and add the missing alt text to the three screenshots that the screen reader skips",
];
const LONG_MILESTONE = "The first batch of one hundred answers, checked, tagged and searchable by product area";

function ago(m: number): string {
  if (m < 1) return "just now";
  const one = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"} ago`;
  if (m < 60) return one(m, "minute");
  if (m < 1440) return one(Math.floor(m / 60), "hour");
  if (m < 10080) return one(Math.floor(m / 1440), "day");
  if (m < 43200) return one(Math.floor(m / 10080), "week");
  return one(Math.floor(m / 43200), "month");
}

function build(long: boolean): { rows: TicketRow[]; mins: Map<TicketRow, number> } {
  const rows: TicketRow[] = [];
  const mins = new Map<TicketRow, number>();
  let pr = 330;
  for (const [pi, [p, specs]] of ([[PROJECTS[0], YS], [PROJECTS[1], ZS], [PROJECTS[2], DOCS]] as [Project, Spec[]][]).entries()) {
    specs.forEach(([title, kind, mi, blocked, m], i) => {
      const n = i + 1;
      const ref = `${p.tag}-${n}`;
      const ms = p.milestones[mi];
      const number = `M${mi + 1}`;
      const minutes = m ?? 1200 + (specs.length - i) * 610 + ((i * 29) % 200) + pi * 90;
      const done = kind === "merged" || kind === "canceled";
      const who = done ? null
        : kind === "review" ? "Reviewer"
        : kind === "todo" || kind === "blocked" ? "Not picked up yet"
        : i === 21 ? "Lead" : `Builder for ${number}`;
      const withPr = kind === "merged" || kind === "changes" || kind === "review" || (kind === "prog" && i % 2 === 0);
      const row: TicketRow = {
        ref,
        title: long && ref === "Z-4" ? LONG_TITLES[0] : long && ref === "DS-26" ? LONG_TITLES[1] : title,
        state: WORD[kind], state_key: KEY[kind],
        project: { id: p.id, name: p.name },
        milestone: { id: `${p.id}-m${mi + 1}`, number, name: long && ref === "Z-4" ? LONG_MILESTONE : ms },
        who,
        pr: withPr ? {
          number: ++pr,
          url: `https://github.com/${REPO}/pull/${pr}`,
          state_text: kind === "merged" ? "merged" : kind === "changes" ? "open, CI failing" : i % 2 ? "open, CI passed" : "open, CI in progress",
        } : null,
        blocked_by: (blocked ?? []).map((b) => `${p.tag}-${b}`),
        changed: ago(minutes),
        changed_at: NOW,
        href: `/projects/${p.id}/tickets/${ref}`,
      };
      mins.set(row, minutes);
      rows.push(row);
    });
  }
  return { rows, mins };
}

const SHORT = build(false);
const LONG = build(true);

interface Dataset { rows: TicketRow[]; mins: Map<TicketRow, number>; projects: Project[] }

const SETS: Record<string, Dataset | "load" | "err"> = {
  many: { ...SHORT, projects: PROJECTS },
  long: { ...LONG, projects: PROJECTS },
  empty: { rows: [], mins: new Map(), projects: [] },
  load: "load",
  err: "err",
};

export const isSample = (name: string | null): name is string => !!name && name in SETS;

const MOVING = new Set(["in_progress", "in_review", "changes_asked"]);

/** What a read of the page answers for a sample: the data, never an answer (`load`), or an error (`err`). */
export function sampleLoad(name: string, q: TicketsQuery): Promise<FactoryTicketsData> {
  const s = SETS[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  const want = q.state ? q.state.replace(/-/g, "_") : "";
  const needle = (q.q ?? "").trim().toLowerCase();
  const inScope = s.rows.filter((r) =>
    (!q.project || r.project.id === q.project)
    && (!q.milestone || r.milestone?.id === q.milestone)
    && (!needle || r.ref.toLowerCase().includes(needle) || r.title.toLowerCase().includes(needle)));
  const counts = {
    total: 0, moving: 0, to_do: 0, in_progress: 0, in_review: 0, changes_asked: 0, blocked: 0, merged: 0, canceled: 0,
  };
  for (const r of inScope) {
    counts[r.state_key] += 1;
    if (r.state_key !== "canceled") counts.total += 1;
    if (MOVING.has(r.state_key)) counts.moving += 1;
  }
  const picked = inScope.filter((r) => (!want ? r.state_key !== "canceled" : want === "moving" ? MOVING.has(r.state_key) : r.state_key === want));
  const order = (r: TicketRow) => s.projects.findIndex((p) => p.id === r.project.id);
  const num = (r: TicketRow) => Number(r.ref.replace(/^\D+/, ""));
  const m = (r: TicketRow) => s.mins.get(r) ?? 0;
  if (q.sort === "old") picked.sort((a, b) => m(b) - m(a));
  else if (q.sort === "ref") picked.sort((a, b) => order(a) - order(b) || num(a) - num(b));
  else picked.sort((a, b) => m(a) - m(b));
  if (q.sort === "open") picked.sort((a, b) => Number(a.state_key === "merged") - Number(b.state_key === "merged"));
  const offset = q.offset ?? 0;
  const limit = q.limit ?? 25;
  const page = picked.slice(offset, offset + limit);
  return Promise.resolve({
    tickets: page, total: picked.length, offset, limit, more: offset + page.length < picked.length, counts,
    projects: s.projects.map((p) => ({ id: p.id, name: p.name, count: s.rows.filter((r) => r.project.id === p.id && r.state_key !== "canceled").length })),
    milestones: s.projects
      .filter((p) => !q.project || p.id === q.project)
      .flatMap((p) => p.milestones.map((name, i) => ({ id: `${p.id}-m${i + 1}`, project: p.id, number: `M${i + 1}`, name }))),
    hidden_canceled: want === "canceled" ? 0 : counts.canceled,
    now: NOW,
  });
}
