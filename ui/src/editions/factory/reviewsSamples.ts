// Sample data for the Reviews page, shown at /reviews?sample=<name> when the browser has the sample
// flag (see sample.ts). It is invented: project-x, project-y, example-org/service-a, Alex, Kim.

import type { CiState, FactoryReviewsData, ReviewRow } from "./FactoryReviews.api";

const NOW = "2026-11-02T10:00:00+00:00";
const MERGE = "You merge every pull request after the crew's review.";
const CREW_MERGES = "The crew merges after its review and green CI.";
const A = "example-org/service-a";
const B = "example-org/docs";

const CI_WORDS: Record<CiState, string> = { ok: "CI passed", bad: "CI failing", go: "CI in progress", none: "No CI" };

interface Spec {
  project: string; ref: string; title: string; number: number; waited: string;
  review?: string; ci?: CiState; holds?: string | null; repo?: string; why?: string | null;
}

const row = (s: Spec): ReviewRow => {
  const id = s.project.replace(/[^a-z0-9]+/g, "-");
  const ci = s.ci ?? "ok";
  return {
    project: { id, name: s.project },
    ticket: { ref: s.ref, title: s.title, href: `/projects/${id}/tickets/${s.ref}` },
    pr: { number: s.number, url: `https://github.com/${s.repo ?? A}/pull/${s.number}`, title: s.title, repo: s.repo ?? A },
    waited: s.waited, since: NOW, review: s.review ?? "Passed, round 1", ci: CI_WORDS[ci], ci_state: ci,
    holds_up: s.holds ?? null, codex_open: false, why: s.why ?? null,
  };
};

const WAITING: ReviewRow[] = ([
  { project: "project-x", ref: "T14", title: "Add a bench script for the list endpoints", number: 250, waited: "10 days", holds: "Holds up M2: Faster lists" },
  { project: "project-x", ref: "T17", title: "Batch the ticket titles and last reviews in the tickets list", number: 257, waited: "10 days", holds: "Holds up M2: Faster lists" },
  { project: "project-x", ref: "T8", title: "Batch the review queue", number: 260, waited: "10 days", holds: "Holds up M2: Faster lists" },
  { project: "project-x", ref: "T19", title: "Show project progress from one query", number: 262, waited: "10 days", holds: "Holds up M2: Faster lists" },
] as Spec[]).map(row);

const WITH_CREW: ReviewRow[] = ([
  { project: "project-y", ref: "T68", title: "Add the edition routes and the Advanced tab in Settings", number: 355, waited: "8 minutes",
    review: "Not reviewed yet", ci: "go", holds: "Holds up 1. Foundations", why: "The reviewer reviews it" },
  { project: "project-z", ref: "T92", title: "Write the sixth wave of answer articles", number: 279, waited: "2 days",
    review: "Changes asked, round 1", repo: B, why: "The builder works on the changes the reviewer asked for" },
  { project: "project-z", ref: "T73", title: "Fix up 10 older articles flagged in two reviews", number: 287, waited: "2 days",
    review: "Passed on an older commit", repo: B, why: "The reviewer reviews it" },
] as Spec[]).map(row);

const NOBODY: ReviewRow[] = ([
  { project: "project-w", ref: "T5", title: "Add a bench script for the cell's endpoints", number: 250, waited: "1 week",
    review: "Not reviewed yet", why: "The project is finished, so the crew no longer works on it" },
  { project: "project-z", ref: "T61", title: "Split the long answer pages", number: 270, waited: "4 days",
    review: "Not reviewed yet", repo: B, why: "The pull request was closed without merging" },
] as Spec[]).map(row);

const LONG_TITLE = "Move every answer article off the old docs template, check each against the fact sheet, and rewrite the ones that fail so that a reader finds the same answer in the docs, in search and in an assistant";
const LONG_ROW = row({
  project: "customer-connectors-for-the-enterprise-edition-2026", ref: "T1234", title: LONG_TITLE, number: 3141, waited: "6 days",
  holds: "Holds up M1: A very long milestone name that has to wrap inside its card and never push the waiting time out", repo: B,
});

const MANY: ReviewRow[] = Array.from({ length: 40 }, (_, i) => row({
  project: `project-${1 + (i % 5)}`, ref: `T${900 + i}`, title: `Ticket title number ${i + 1}`, number: 300 + i, waited: `${i + 1} days`,
  holds: i % 4 === 0 ? `Holds up M${1 + (i % 3)}: Milestone name` : null,
}));

const data = (d: Partial<FactoryReviewsData>): FactoryReviewsData => ({
  waiting_for_you: [], with_the_crew: [], nobody_on_it: [], autonomy_text: MERGE, project: null, now: NOW, ...d,
});

const SAMPLES: Record<string, FactoryReviewsData | "load" | "err"> = {
  // you merge: four wait for you, three are with the crew
  l3: data({ waiting_for_you: WAITING, with_the_crew: WITH_CREW, nobody_on_it: NOBODY }),
  // the crew merges: nothing waits for you, so the crew's list opens
  l4: data({ autonomy_text: CREW_MERGES, with_the_crew: [...WAITING.map((r) => ({ ...r, why: "The crew merges it" })), ...WITH_CREW] }),
  empty: data({}),
  long: data({ waiting_for_you: [LONG_ROW, WAITING[0]], with_the_crew: WITH_CREW.slice(0, 1) }),
  many: data({ waiting_for_you: MANY, with_the_crew: WITH_CREW }),
  filt: data({ waiting_for_you: WAITING, project: { id: "project-x", name: "project-x" } }),
  load: "load", err: "err",
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** What a read of the page answers for a sample: the data, never an answer (`load`), or an error (`err`). */
export function sampleLoad(name: string): Promise<FactoryReviewsData> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(s);
}
