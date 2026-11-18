import type { HomeItem } from "./home.types";
import type { FeedAnswered, FeedData } from "./feed.types";
import { toFeedCard } from "./feedCard";

// Samples of the feed (P-TR-273, M2 mock), in the shape of GET /api/home's items plus what the home does
// not send yet (FeedExtra). They show only behind the sample flag (src/editions/sample.ts). Every name
// here is invented; the asks are the ones the two M1 runs met.

const NOW = "2026-11-18T09:00:00Z";
const crew = { crew: true, name: "Your crew" };

function item(id: string, over: Partial<HomeItem> & Pick<HomeItem, "kind" | "label" | "title" | "detail">): HomeItem {
  return { id, meta: "", project: null, repo: null, from: crew, since: null, waited: "just now", actions: [], ...over };
}

const atlas = { id: "atlas", name: "atlas" };
const linkExpiry = { id: "link-expiry", name: "link-expiry" };

const ITEMS: HomeItem[] = [
  item("desk:D7", {
    kind: "desk", label: "Question", project: atlas, blocking: true,
    title: "Which store should sessions use?",
    detail: "The builder on AT-4 needs a place to keep sessions before it can write the API. Redis is already in the stack; Postgres keeps one fewer moving part.",
    since: "2026-11-18T08:41:00Z", waited: "19 minutes",
    from: { name: "the builder" },
    question: { desk: "D7", options: ["Redis", "Postgres", "Ask me on a call"], recommendation: "Postgres", blocks: "AT-4 and the 2 tickets after it" },
  }),
  item("prs_to_review:atlas:336", {
    kind: "prs_to_review", label: "Merge", project: atlas, blocking: true,
    title: "1 PR to review in atlas",
    detail: "The reviewer passed it on its second round. It adds the expiry column and its migration.",
    since: "2026-11-17T11:02:00Z", waited: "22 hours", tickets: ["AT-3"],
    from: { name: "the reviewer" },
    actions: [{ label: "Open Reviews", primary: true, href: "/reviews?project=atlas" }],
  }),
  item("permission:D8", {
    kind: "permission", label: "Permission", project: linkExpiry,
    title: "Let the builder rebase its own branch?",
    detail: "Main moved 3 commits under PR 12. Today the builder may not rebase without asking.",
    lines: ["git rebase origin/main", "git push --force-with-lease"],
    since: "2026-11-18T07:55:00Z", waited: "1 hour",
    from: { name: "the builder" },
  }),
  item("milestone_to_check:atlas:M1", {
    kind: "milestone_to_check", label: "Check", project: atlas,
    title: "M1: sign in works end to end",
    detail: "All 4 tickets of M1 are merged. Check it on staging before M2 starts.",
    lines: ["Open staging and sign in with a new account", "Sign out, sign in again: the session is kept"],
    since: "2026-11-17T16:20:00Z", waited: "17 hours",
    actions: [{ label: "Open project", primary: false, href: "/projects/atlas" }],
  }),
  item("calls_to_confirm:link-expiry", {
    kind: "calls_to_confirm", label: "Calls", project: linkExpiry,
    title: "2 calls the crew made without you",
    detail: "The crew chose these to keep going. Keep them, or overturn one and it rebuilds from there.",
    lines: ["Expired links answer 410, not 404", "Expiry is checked on read, no sweeper"],
    since: "2026-11-16T10:00:00Z", waited: "2 days",
  }),
  item("ready_to_start:billing", {
    kind: "ready_to_start", label: "Start", project: { id: "billing", name: "billing" },
    title: "billing is planned and ready",
    detail: "The spec is written: 3 milestones, 11 tickets. Nothing has started.",
    since: "2026-11-15T09:30:00Z", waited: "3 days",
  }),
];

const EXTRAS: FeedData["extras"] = {
  "desk:D7": { why: "Sessions are rows we already back up; Redis would be the only thing holding them." },
  "prs_to_review:atlas:336": {
    ask: "Merge PR 336", holds_up: "AT-4, the next ticket",
    checks: "Checks green. Main moved 2 commits since the review.",
  },
  "permission:D8": { ask: "Let the builder rebase PR 12", holds_up: "PR 12" },
  "milestone_to_check:atlas:M1": { ask: "Check M1 of atlas", holds_up: "M2: billing hooks" },
  "calls_to_confirm:link-expiry": { ask: "Keep 2 calls on link-expiry", holds_up: null },
  "ready_to_start:billing": { ask: "Start billing", holds_up: null },
};

const ANSWERED: FeedAnswered[] = [
  {
    card: toFeedCard(item("grant:G2", {
      kind: "grant", label: "Grant", project: linkExpiry,
      title: "Let the crew merge on green", detail: "Asked by the crew lead after the third merge.",
    }), { ask: "Let the crew merge on green" }),
    choice: "yes", words: null, by: "you", at: "2026-11-18T07:12:00Z", via: "feed",
  },
  {
    card: toFeedCard(item("desk:D5", {
      kind: "desk", label: "Question", project: atlas,
      title: "Keep the old /login page?", detail: "The builder asked before removing it.",
      question: { desk: "D5", options: ["Remove it", "Keep it, redirect"], recommendation: "Remove it", blocks: null },
    })),
    choice: "no", words: "Keep it, redirect", by: "you", at: "2026-11-18T06:40:00Z", via: "console",
  },
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
