import type { HomeItem } from "./home.types";
import type { FeedData } from "./feed.types";

// Samples of the feed (P-TR-273), in the shape GET /api/home sends (factory_waiting.py builds each kind
// with these same actions), plus what the home does not send yet (FeedExtra). They show only behind the
// sample flag (src/editions/sample.ts), and their buttons go through Home's handler, which sends nothing
// for a sample. Every name here is invented.

const NOW = "2026-11-18T09:00:00Z";
const crew = { crew: true, name: "Your crew" };

function item(id: string, over: Partial<HomeItem> & Pick<HomeItem, "kind" | "label" | "title" | "detail">): HomeItem {
  return { id, meta: "", project: null, repo: null, from: crew, since: null, waited: "just now", actions: [], ...over };
}

const atlas = { id: "atlas", name: "atlas" };
const linkExpiry = { id: "link-expiry", name: "link-expiry" };
const billing = { id: "billing", name: "billing" };

const ITEMS: HomeItem[] = [
  item("question:D7", {
    kind: "question", label: "Question for you", project: atlas, blocking: true,
    title: "atlas", detail: "Which store should sessions use?",
    since: "2026-11-18T08:41:00Z", waited: "19 minutes",
    from: { name: "the builder" },
    question: { desk: "D7", options: ["Redis", "Postgres", "Ask me on a call"], recommendation: "Postgres", blocks: "AT-4 and the 2 tickets after it" },
    actions: [{ label: "Answer", primary: true, href: "/projects/atlas#desk-D7" }],
  }),
  item("prs_to_review:atlas", {
    kind: "prs_to_review", label: "1 PR to review", project: atlas, blocking: true,
    title: "atlas", detail: "The reviewer passed PR 336 on its second round. It adds the expiry column and its migration.",
    since: "2026-11-17T11:02:00Z", waited: "22 hours", tickets: ["AT-3"],
    from: { name: "the reviewer" },
    actions: [{ label: "Review", primary: true, href: "/reviews?project=atlas" }],
  }),
  item("permission:D8", {
    kind: "permission", label: "Permission", project: linkExpiry, blocking: true,
    title: "link-expiry", detail: "The builder wants to use: `git push --force-with-lease`",
    since: "2026-11-18T07:55:00Z", waited: "1 hour",
    from: { name: "the builder" },
    actions: [
      { label: "Answer", primary: false, href: "/projects/link-expiry#desk-D8" },
      { label: "Allow", primary: true, do: "allow", desk: "D8", station: "builder-1", may_limit: true },
    ],
  }),
  item("milestone_to_check:atlas:M1", {
    kind: "milestone_to_check", label: "Milestone to check", project: atlas,
    title: "M1: sign in works end to end",
    detail: "All 4 tickets of M1 are merged. Check it on staging before M2 starts.",
    lines: ["Open staging and sign in with a new account", "Sign out, sign in again: the session is kept"],
    since: "2026-11-17T16:20:00Z", waited: "17 hours",
    actions: [{ label: "Check it", primary: true, href: "/projects/atlas" }],
  }),
  item("calls_to_confirm:link-expiry", {
    kind: "calls_to_confirm", label: "2 calls to confirm", project: linkExpiry,
    title: "link-expiry", detail: "The crew decided without you:",
    lines: ["Expired links answer 410, not 404", "Expiry is checked on read, no sweeper"],
    since: "2026-11-16T10:00:00Z", waited: "2 days",
    actions: [
      { label: "Read them", primary: false, href: "/projects/link-expiry/calls" },
      { label: "Keep both", primary: true, do: "keep_assumptions", project: "link-expiry", refs: ["A3", "A4"] },
    ],
  }),
  item("ready_to_start:billing", {
    kind: "ready_to_start", label: "Ready to start", project: billing,
    title: "billing", detail: "Spec and plan written, 11 tickets. The crew has not picked it up.",
    since: "2026-11-15T09:30:00Z", waited: "3 days",
    actions: [
      { label: "Read the spec", primary: false, href: "/projects/billing" },
      { label: "Later", primary: false, do: "later", project: "billing" },
      { label: "Copy the hand-over command", primary: true, do: "copy", text: "/tares:ship billing" },
    ],
  }),
];

const EXTRAS: FeedData["extras"] = {
  "question:D7": { why: "Sessions are rows we already back up; Redis would be the only thing holding them." },
  "prs_to_review:atlas": {
    ask: "Merge PR 336", holds_up: "AT-4, the next ticket",
    checks: "Checks green. Main moved 2 commits since the review.",
  },
  "permission:D8": { holds_up: "PR 12" },
  "milestone_to_check:atlas:M1": { ask: "Check M1 of atlas", holds_up: "M2: billing hooks" },
  "calls_to_confirm:link-expiry": { ask: "Keep 2 calls on link-expiry" },
  "ready_to_start:billing": { ask: "Hand billing to the crew" },
};

const ANSWERED: FeedData["answered"] = [
  { id: "grant:G2", ask: "Let the crew merge on green", choice: "yes", said: "Agree", by: "you", at: "2026-11-18T07:12:00Z", via: "feed" },
  { id: "question:D5", ask: "Keep the old /login page?", choice: "no", said: "Keep it, redirect", by: "you", at: "2026-11-18T06:40:00Z", via: "console" },
];

const running = { running: true, since: null, summary: "Building atlas and link-expiry." };

export const FEED_SAMPLES: Record<string, FeedData> = {
  busy: { now: NOW, crew: running, items: ITEMS, extras: EXTRAS, answered: ANSWERED },
  empty: { now: NOW, crew: running, items: [], extras: {}, answered: ANSWERED },
  down: {
    now: NOW, crew: { running: false, since: "2026-11-18T06:12:00Z", summary: "Nothing is being built." },
    items: [], extras: {}, answered: [],
  },
};
