// Sample data for the ticket page, shown at /projects/<id>/tickets/<ref>?sample=<name> when the
// browser has the sample flag (see sample.ts). It is invented: project-y, docs-site, Alex, Kim, Lee.
// The words are the cell's own ("Changes asked", "asked for changes", "fixed in round 3"). No sample
// is real cell data.

import { ApiError } from "../../api";
import type {
  FactoryTicketData, TicketCall, TicketCheck, TicketFinding, TicketLink, TicketReview, TicketStateKey,
} from "./FactoryTicket.api";

const NOW = "2026-11-02T10:00:00+00:00";
const PID = "uc_sample";
const PROJECT = { id: PID, name: "project-y", href: `/projects/${PID}` };

const LABEL: Record<TicketStateKey, string> = {
  to_do: "To do", in_progress: "In progress", in_review: "In review", changes_asked: "Changes asked",
  blocked: "Blocked", merged: "Merged", canceled: "Canceled",
};

const link = (ref: string, title: string, state: TicketStateKey): TicketLink => ({
  ref, title, state_label: LABEL[state], href: `/projects/${PID}/tickets/${ref}`,
});

const find = (text: string, file: string, line: number, blocking: boolean, state_text: string, n: number): TicketFinding => ({
  label: `F${n}`, text, file, line, blocking, state_text,
});

const review = (round: number, verdict_text: string, when: string, verified: string, not_verified: string | null,
                findings: TicketFinding[]): TicketReview => ({
  round, verdict_text, by: "the reviewer", when, at: NOW, verified, not_verified, findings,
});

const check = (command: string, result: string, when: string): TicketCheck => ({ command, result, when });

const call = (n: number, question: string, choice: string, why: string, made: string, state_text: string): TicketCall => ({
  ref: `A${n}`, question, choice, why, made, state_text, words: null,
});

const line = (what: string, who: string | null, when: string, text: string | null = null) => ({ what, who, when, at: NOW, text });

const BASE: FactoryTicketData = {
  ref: "T5", title: "Publish one article by hand", state: "merged", state_label: "Merged",
  milestone: { id: "ms1", number: "M1", name: "First answers" }, in: "Linear", url: "https://linear.example.com/project-y/T5",
  who: null, blocked_reason: null, depends_on: [], blocks: [], pr: null, reviews: [], checks: [], calls: [], decisions: [], history: [],
  notes: "", terminal: [], project: PROJECT, now: NOW,
};

const MERGED_NOTES = [
  "# Goal", "Publish one article by hand, from a draft to the live docs site, so the steps are known before anything is automated.",
  "## Progress", "- The draft script is in place.", "- Published one article to docs-site.", "- Review round 3 passed.",
  "## How to try it", "```", "python site/publish.py drafts/first.md", "```",
].join("\n");

const MERGED: FactoryTicketData = {
  ...BASE,
  depends_on: [link("T2", "Script that turns a question into a draft", "merged"), link("T3", "Style rules for answers", "merged")],
  blocks: [link("T9", "Publish ten articles in a batch", "to_do"), link("T10", "Check published links every night", "to_do")],
  pr: { number: 7, url: "https://github.com/example-org/docs-site/pull/7", state_text: "merged" },
  reviews: [
    review(3, "passed", "3 days ago", "the tests on the branch, the new page in a browser", "the live site", [
      find("The page title is missing a space after the colon.", "site/publish.py", 42, false, "fixed in round 3", 3),
      find("The draft reaches the site even when the checklist is empty.", "site/publish.py", 88, true, "fixed in round 3", 2),
    ]),
    review(2, "asked for changes", "4 days ago", "the tests on the branch", "the page in a browser", [
      find("The draft reaches the site even when the checklist is empty.", "site/publish.py", 88, true, "fixed in round 3", 2),
      find("The script prints the whole draft when it fails.", "site/publish.py", 120, false, "fixed in round 3", 4),
    ]),
    review(1, "asked for changes", "5 days ago", "the diff", "the tests", [
      find("There is no test for an empty draft.", "tests/test_publish.py", 1, true, "fixed in round 2", 1),
    ]),
  ],
  checks: [
    check("python tests/test_publish.py", "9 passed, 0 failed", "3 days ago"),
    check("python tests/test_style.py", "14 passed, 0 failed", "3 days ago"),
    check("python tests/test_publish.py", "7 passed, 2 failed", "4 days ago"),
  ],
  calls: [call(1, "Should an article without a checklist be blocked?", "Yes, it is held back and the builder is told.",
               "The plan says every article is checked first.", "by the builder, 5 days ago", "confirmed")],
  decisions: [{ question: "Which site holds the first ten articles?", answer: "docs-site, in its own folder", said: "B", when: "6 days ago" }],
  history: [
    line("Merged", "the reviewer", "3 days ago"), line("Moved to in review", "the builder", "3 days ago"),
    line("Changes asked", "the reviewer", "4 days ago", "The draft reaches the site even when the checklist is empty."),
    line("Moved to in review", "the builder", "4 days ago"), line("Changes asked", "the reviewer", "5 days ago"),
    line("Moved to in review", "the builder", "5 days ago"), line("Started", "the builder", "6 days ago"),
  ],
  notes: MERGED_NOTES,
};

const OPEN: FactoryTicketData = {
  ...BASE, ref: "T7", title: "Sample set of 10 questions", state: "changes_asked", state_label: "Changes asked", in: "GitHub",
  who: "Builder for M1",
  depends_on: [link("T1", "Answer template for the docs site", "merged")], blocks: [link("T8", "Score the first answers", "blocked")],
  pr: { number: 9, url: "https://github.com/example-org/docs-site/pull/9", state_text: "open, CI green" },
  reviews: [
    review(2, "asked for changes", "1 hour ago", "the tests on the branch, the diff", "the sample set in a browser", [
      find("Question 4 and question 7 are the same question.", "samples/questions.md", 31, true, "open", 5),
      find("The loader skips a question that has no answer line, without saying so.", "site/load.py", 57, false, "open", 6),
    ]),
    review(1, "asked for changes", "2 days ago", "the diff", "the tests", [
      find("The file has no header row.", "samples/questions.md", 1, true, "fixed in round 2", 4),
    ]),
  ],
  checks: [check("python tests/test_samples.py", "6 passed, 0 failed", "2 hours ago")],
  calls: [call(2, "Should the sample set include questions in other languages?", "No, English only for now.",
               "The plan names English readers.", "by the builder, 2 days ago", "waiting for you")],
  history: [
    line("Changes asked", "the reviewer", "1 hour ago"), line("Moved to in review", "the builder", "2 hours ago"),
    line("Changes asked", "the reviewer", "2 days ago"), line("Moved to in review", "the builder", "2 days ago"),
    line("Started", "the builder", "3 days ago"),
  ],
  notes: "# Goal\nTen questions people really ask, with the answer each should give.\n## Progress\n- Wrote 10 questions.\n- Waiting on the duplicate in 4 and 7.\n## Next\n- Replace question 7.\n- Make the loader say when it skips one.",
  terminal: [{ label: "Open the builder's session", command: "tares factory crew attach project-y-build-m1" }],
};

const BLOCKED: FactoryTicketData = {
  ...BASE, ref: "T9", title: "Publish ten articles in a batch", state: "blocked", state_label: "Blocked", in: "Tares", url: null,
  blocked_reason: "Waiting for T8, Score the first answers.",
  milestone: { id: "ms2", number: "M2", name: "Publishing" },
  depends_on: [link("T5", "Publish one article by hand", "merged"), link("T8", "Score the first answers", "blocked")],
  history: [
    line("Moved to blocked", "the lead", "1 day ago", "Waiting for T8, Score the first answers."),
    line("Changed the title", "the lead", "6 days ago"),
  ],
};

const EMPTY: FactoryTicketData = {
  ...BASE, ref: "T12", title: "Archive old drafts", state: "to_do", state_label: "To do", in: "Tares", url: null,
  milestone: { id: "ms2", number: "M2", name: "Publishing" }, history: [line("Changed the title", "the lead", "just now")],
};

const LONG_NOTES = (() => {
  const verbs = ["Checked", "Rewrote", "Moved", "Linked", "Dropped"];
  let s = "# Goal\nTurn the whole docs site into answers, section by section.\n## Progress log\n";
  for (let i = 1; i <= 1994; i++) s += i % 97 === 0 ? `## Day ${i / 97}\n` : `- ${verbs[i % 5]} section ${i} of the docs site, then tried the page again.\n`;
  return s + "## Next\n- Check what is left.\n";
})();

const LONG: FactoryTicketData = {
  ...OPEN, ref: "T14",
  title: "Rewrite every page of the docs site as answers, keeping the links, the code samples and the order of the sections, and say which pages were dropped",
  notes: LONG_NOTES,
};

const MANY: FactoryTicketData = (() => {
  const reviews: TicketReview[] = [];
  for (let i = 12; i >= 1; i--) {
    const last = i === 12;
    reviews.push(review(i, last ? "passed" : i % 5 === 0 ? "blocked" : "asked for changes", `${13 - i} days ago`,
      "the tests on the branch, the diff", last ? null : "the live site",
      last ? [] : [find(`Finding ${i}: a case the page does not handle.`, "site/pages.py", 10 + i, i % 2 === 0, i < 12 ? `fixed in round ${i + 1}` : "open", i)]));
  }
  const words = ["Moved to in review", "Changes asked", "Moved to in progress"];
  const history = Array.from({ length: 40 }, (_, i) => line(words[i % 3], i % 3 === 1 ? "the reviewer" : "the builder", `${i + 1} days ago`));
  const checks = Array.from({ length: 12 }, (_, i) =>
    check("python tests/test_pages.py", `1${i % 3} passed, ${i % 3 === 2 ? "0" : "1"} failed`, `${i + 1} days ago`));
  return { ...MERGED, ref: "T15", title: "Move every page to the new template", reviews, history, checks };
})();

const SAMPLES: Record<string, FactoryTicketData | "load" | "err" | "missing"> = {
  merged: MERGED, changes: OPEN, blocked: BLOCKED, empty: EMPTY, long: LONG, many: MANY,
  load: "load", err: "err", missing: "missing",
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** What a read of the page answers for a sample: the data, never an answer (`load`), the cell's
 *  404 (`missing`), or an error (`err`). */
export function sampleLoad(name: string): Promise<FactoryTicketData> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "missing") return Promise.reject(new ApiError("no ticket T99 in this project", 404));
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(s);
}
