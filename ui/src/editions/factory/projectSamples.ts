// Sample data for the project page, shown at /projects/<id>?sample=<name> when the browser has the
// sample flag (see sample.ts). It is invented: project-x, project-y, project-z, example-org/docs-site,
// Alex, Kim, Lee. No sample is real cell data.

import type { HomeAction, HomeItem } from "./home.types";
import type { FactoryProjectData, MovingTicket, ProjectCall, ProjectMilestone, TicketState } from "./FactoryProject.api";

const NOW = "2026-11-02T10:00:00+00:00";
const PID = "uc_sample";
const TICKETS = `/tickets?project=${PID}`;

const COUNT_KEY: Record<TicketState, keyof FactoryProjectData["counts"]> = {
  "to do": "to_do", "in progress": "in_progress", "in review": "in_review", "changes asked": "changes_asked",
  blocked: "blocked", merged: "merged", canceled: "canceled",
};

const ticket = (ref: string, title: string, state: TicketState, extra: Partial<MovingTicket> = {}): MovingTicket => ({
  ref, title, state, who: null, pr: null, review: null, blocked_by: [], href: `/projects/${PID}/tickets/${ref}`, ...extra,
});

const ms = (n: number, name: string, goal: string, total: number, merged: number,
            checks: { check: string; expect: string }[] = []): ProjectMilestone => ({
  id: `ms${n}`, number: `M${n}`, name, goal, done: total > 0 && merged === total,
  counts: { total, merged }, checks, tickets_href: `${TICKETS}&milestone=ms${n}`,
});

const act = (label: string, primary: boolean, extra: Partial<HomeAction>): HomeAction => ({ label, primary, ...extra });

const item = (kind: HomeItem["kind"], label: string, title: string, detail: string, meta: string,
              actions: HomeAction[]): HomeItem => ({
  id: `${kind}:${label}`, kind, label, title, detail, meta, project: { id: PID, name: "project-y" }, repo: null,
  from: null, since: NOW, waited: "3 hours", actions,
});

const REVIEW = item("prs_to_review", "2 PRs to review", "project-y",
  "Show a last checked date on each answer, Add the answers to the docs sidebar. Each one holds up a milestone.",
  "Waiting 3 hours · example-org/docs-site", [act("Review", true, { href: `/reviews?project=${PID}` })]);
const CALLS = item("calls_to_confirm", "2 calls to confirm", "project-y",
  "The crew decided without you: ISO dates, the standard logger. Keep both?", "Made by the builder and the reviewer",
  [act("Read them", false, { href: `/projects/${PID}#calls` }), act("Keep both", true, { do: "keep_assumptions" })]);
const PERMISSION = item("permission", "Permission", "project-y",
  "The builder wants to use: `gh pr merge 17 --squash`", "Asked by the crew, 3 hours ago",
  [act("Open request", false, { href: `/projects/${PID}#desk-D2` }), act("Allow", true, { do: "allow" })]);
const QUESTION = item("question", "Question for you", "project-y",
  "Which languages first, German or French?", "Asked by the crew, 5 hours ago",
  [act("Answer", true, { href: `/projects/${PID}#desk-D1` })]);
const CHECK = item("milestone_to_check", "Milestone to check", "project-y, M1: Foundations",
  "Every ticket of this milestone merged. Try its 2 checks.", "Merged 1 day ago",
  [act("Check it", true, { href: `/projects/${PID}#milestone-ms1` })]);

const CALL_LIST: ProjectCall[] = [
  { ref: "A1", question: "Which date format?", choice: "ISO dates", why: "They sort the same as the text.",
    made: "by the builder, 2 days ago", tickets: ["T6", "T7"] },
  { ref: "A2", question: "Which logger?", choice: "The standard one", why: "It adds no new package.",
    made: "by the reviewer, 1 day ago", tickets: [] },
];

const WORDS = [
  "Support owns them. Docs only edits.", "No. Show the date, not the person.",
  "Say what was wrong and what is right. No blame.", "Yes, one short sample, tested.",
  "Only when the screen is hard to describe.", "Lee reads them before they go live.",
];
const QS = ["Which team owns answers?", "Do we show the author?", "How do we word a wrong answer?",
  "Is a code sample allowed?", "Do answers need a screenshot?", "Who reads the German ones?"];
const decisions = (n: number) => Array.from({ length: n }, (_, i) => ({
  question: QS[i % QS.length] + (i >= QS.length ? ` (round ${Math.floor(i / QS.length) + 1})` : ""),
  answer: WORDS[i % WORDS.length], said: i % 3 === 0 ? `D${40 + i} ${"AB"[i % 2]}` : null,
  when: `${i + 1} day${i ? "s" : ""} ago`, at: NOW,
}));

const DOCS = [
  { id: "d1", title: "Spec", kind_label: "Spec", updated: "5 days ago", href: "/docs?doc=d1" },
  { id: "d2", title: "Plan", kind_label: "Plan", updated: "5 days ago", href: "/docs?doc=d2" },
  { id: "d3", title: "Rules", kind_label: "Rules", updated: "4 days ago", href: "/docs?doc=d3" },
  { id: "d4", title: "Memory", kind_label: "Memory", updated: "1 day ago", href: "/docs?doc=d4" },
];

const COMMAND = "tares factory crew attach project-y-build-m2";
const TERMINAL = [{ label: "Open the builder's session", command: COMMAND }];

interface Parts {
  name: string; goal: string; stage?: FactoryProjectData["stage"]; state?: FactoryProjectData["state"];
  state_label?: string; waiting?: HomeItem[]; moving?: MovingTicket[]; moving_total?: number;
  merged?: number; canceled?: number; total?: number; next?: FactoryProjectData["next"];
  milestones?: ProjectMilestone[]; decisions?: FactoryProjectData["decisions"]; calls?: ProjectCall[];
  docs?: typeof DOCS; terminal?: typeof TERMINAL; tracker?: "Linear" | "GitHub" | null; can_close?: boolean;
}

function make(p: Parts): FactoryProjectData {
  const moving = p.moving ?? [];
  const counts = { total: p.total ?? 0, to_do: 0, in_progress: 0, in_review: 0, changes_asked: 0, blocked: 0,
                   merged: p.merged ?? 0, canceled: p.canceled ?? 0 };
  for (const t of moving) counts[COUNT_KEY[t.state]] += 1;
  const state = p.state ?? "building";
  return {
    id: PID, name: p.name, goal: p.goal, stage: p.stage ?? "build",
    stage_label: { spec: "Spec", build: "Build", review: "Review", done: "Done" }[p.stage ?? "build"],
    state, state_label: p.state_label ?? { building: "Building", waiting_on_you: "Waiting on you", not_started: "Not started",
      ready_to_close: "Ready to close", finished: "Finished" }[state],
    waiting: p.waiting ?? [], counts, moving, moving_total: p.moving_total ?? moving.length, tickets_href: TICKETS,
    next: p.next ?? null, milestones: p.milestones ?? [], decisions: p.decisions ?? [], calls: p.calls ?? [],
    docs: p.docs ?? DOCS, terminal: p.terminal ?? TERMINAL,
    linked: { tickets_in: p.tracker ?? null, url: p.tracker ? "https://example.com/tickets" : null },
    can_close: p.can_close ?? false, now: NOW,
  };
}

const CHECKS_M1 = [
  { check: "Open the docs site and search for a question.", expect: "An answer page with a last checked date." },
  { check: "Publish one article by hand.", expect: "It shows in the sidebar within a minute." },
];

const BUILDING = make({
  name: "project-y", goal: "Publish 1,000 answers to common questions on the docs site.", state: "waiting_on_you",
  waiting: [REVIEW, CALLS, PERMISSION, QUESTION],
  moving: [
    ticket("T14", "Show a last checked date on each answer", "in review", { who: "Reviewer", review: "round 1: waiting for the reviewer",
      pr: { number: 17, url: "https://example.com/pull/17", state_text: "PR #17 open, CI green" } }),
    ticket("T15", "Add the answers to the docs sidebar", "changes asked", { who: "Builder for M2", review: "round 2: changes asked",
      pr: { number: 18, url: "https://example.com/pull/18", state_text: "PR #18 open, CI failing" } }),
    ticket("T16", "Tag answers by product area", "in progress", { who: "Builder for M2" }),
    ticket("T17", "Search page lists the answers", "in progress", { who: "Builder for M2" }),
    ticket("T18", "Translate the first 50 answers", "blocked", { blocked_by: ["T16"] }),
  ],
  moving_total: 7, total: 21, merged: 13, canceled: 1,
  next: { ticket: "T19", title: "Show the answer count on the docs home page", why: "ready, nothing it depends on is open" },
  milestones: [
    ms(1, "M1: Foundations", "The template and the publishing script work.", 7, 7, CHECKS_M1),
    ms(2, "M2: The first 100 answers", "A first batch of 100 answers is checked and searchable.", 8, 6,
       [{ check: "Search for a question from the batch.", expect: "The answer is the first result." }]),
    ms(3, "M3: 1,000 answers live", "All answers are live and the home page says so.", 5, 0),
  ],
  decisions: decisions(5), calls: CALL_LIST, tracker: "GitHub",
});

const SPEC = make({
  name: "project-x", goal: "Let customers connect their own tools, with a setup page for each.", stage: "spec",
  state: "not_started", total: 0, docs: DOCS.slice(0, 1), terminal: [], decisions: [],
});

const READY = make({
  name: "project-z", goal: "Prepare the workshop for the whole team.", stage: "done", state: "ready_to_close",
  total: 12, merged: 12, can_close: true, tracker: "GitHub",
  milestones: [ms(1, "M1: The export", "A customer can ask for an export and get a zip file.", 6, 6, CHECKS_M1),
               ms(2, "M2: The schedule", "An export can be set to repeat.", 6, 6)],
  decisions: decisions(2), terminal: [],
});

const FINISHED = make({
  name: "project-z", goal: "Prepare the workshop for the whole team.", stage: "done", state: "finished",
  total: 12, merged: 12, tracker: "GitHub", decisions: decisions(2), terminal: [],
  milestones: [ms(1, "M1: The export", "A customer can ask for an export and get a zip file.", 6, 6),
               ms(2, "M2: The schedule", "An export can be set to repeat.", 6, 6)],
});

const EMPTY = make({ name: "project-w", goal: "A project the crew has not planned yet.", stage: "build", state: "not_started",
  docs: [], terminal: [] });

const LONG_TEXT = "Move every answer article off the old docs template, check each against the fact sheet, rewrite the " +
  "ones that fail, and keep the whole library under review so that a new reader finds the same answer in the docs, in " +
  "search and in an assistant, with the dates current and nothing that contradicts the product as it ships today.";
const LONG = make({
  name: "project-with-a-very-long-name-that-goes-on-and-on-without-a-break-1", goal: LONG_TEXT,
  waiting: [{ ...QUESTION, title: "A question with a long title that goes on and on without any break at all to see it wrap",
              detail: LONG_TEXT }],
  moving: [ticket("T1", "A ticket title that is far too long to fit on one line of the list and has to wrap across two lines at most, then be cut",
                  "in progress", { who: "Builder for M1", review: "round 1: passed" })],
  total: 3, merged: 1, next: { ticket: "T2", title: "Another long title that goes on and on without a break to test the next up row",
                               why: "ready, nothing it depends on is open" },
  milestones: [ms(1, "M1: A very long milestone name that should wrap and never push the bar out of its card", LONG_TEXT, 3, 1,
                  [{ check: LONG_TEXT, expect: LONG_TEXT }])],
  decisions: [{ question: LONG_TEXT, answer: LONG_TEXT, said: null, when: "1 day ago", at: NOW }],
  calls: [{ ref: "A1", question: LONG_TEXT, choice: LONG_TEXT, why: LONG_TEXT, made: "by the builder, 2 days ago", tickets: ["T1"] }],
  docs: [{ id: "d1", title: "A document title that is much too long to fit on one line of the list and wraps", kind_label: "Spec",
           updated: "5 days ago", href: "/docs?doc=d1" }],
  terminal: [{ label: "Open the builder's session", command: "tares factory crew attach project-with-a-very-long-name-build-m1-and-more" }],
});

const MANY = make({
  name: "project-y", goal: "Publish 1,000 answers to common questions on the docs site.", state: "waiting_on_you",
  waiting: [REVIEW, CALLS, PERMISSION, QUESTION, CHECK],
  moving: Array.from({ length: 5 }, (_, i) => ticket(`T${30 + i}`, `Ticket number ${30 + i} in motion`, i < 2 ? "in review" : "in progress",
    { who: "Builder for M2" })),
  moving_total: 18, total: 60, merged: 30, canceled: 2,
  next: { ticket: "T48", title: "Show the answer count on the docs home page", why: "ready, nothing it depends on is open" },
  milestones: Array.from({ length: 6 }, (_, i) => ms(i + 1, `M${i + 1}: Step number ${i + 1}`, `The goal of step ${i + 1}.`, 10, i < 3 ? 10 : i === 3 ? 4 : 0,
    i < 2 ? CHECKS_M1 : [])),
  decisions: decisions(40), calls: [CALL_LIST[0], CALL_LIST[1], { ...CALL_LIST[0], ref: "A3" }, { ...CALL_LIST[1], ref: "A4" }],
  tracker: "Linear",
  terminal: [TERMINAL[0], { label: "Open the builder's session", command: "tares factory crew attach project-y-build-m3" },
             { label: "Open the builder's session", command: "tares factory crew attach project-y-build-m4" }],
});

const SAMPLES: Record<string, FactoryProjectData | "load" | "err"> = {
  building: BUILDING, spec: SPEC, ready: READY, dialog: READY, fail: READY, finished: FINISHED, empty: EMPTY,
  long: LONG, many: MANY, load: "load", err: "err",
};

/** The Close a sample has part way: `dialog` has the confirm open, `fail` has the cell's answer to a
 *  Close already shown under the heading. */
export function sampleClose(name: string): { open: boolean; error: string } {
  if (name === "dialog") return { open: true, error: "" };
  if (name === "fail") return { open: false, error: "Two tickets are still open, so the project cannot close yet." };
  return { open: false, error: "" };
}

/** The sample after its project was closed here: finished, nothing left to close. */
export const sampleClosed = (d: FactoryProjectData): FactoryProjectData => ({
  ...d, stage: "done", stage_label: "Done", state: "finished", state_label: "Finished", can_close: false,
});

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** What a read of the page answers for a sample: the data, never an answer (`load`), or an error (`err`). */
export function sampleLoad(name: string): Promise<FactoryProjectData> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(s);
}
