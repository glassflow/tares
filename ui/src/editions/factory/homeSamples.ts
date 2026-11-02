import type { HomeCrew, HomeCrewMember, HomeData, HomeItem, HomeProject } from "./home.types";

// Sample bodies of the factory home, in the shape of GET /api/home. They show only behind the
// sample flag (src/editions/sample.ts), never on a live cell. Every name here is invented.

const NOW = "2026-11-02T09:00:00Z";

const crewRef = { crew: true, name: "Your crew" };

function item(id: string, over: Partial<HomeItem> & Pick<HomeItem, "kind" | "label" | "title" | "detail" | "meta">): HomeItem {
  return { id, project: null, repo: null, from: crewRef, since: null, waited: "2 days", actions: [], ...over };
}

const reviewsAction = { label: "Open Reviews", primary: true, href: "/reviews" };

const BUSY_WAITING: HomeItem[] = [
  item("prs_to_review:project-x", {
    kind: "prs_to_review", label: "Review", title: "4 PRs to review in project-x",
    detail: "From the builders. The oldest has waited 10 days.", meta: "project-x", waited: "10 days",
    project: { id: "project-x", name: "project-x" }, actions: [reviewsAction],
  }),
  item("signoff:project-y", {
    kind: "signoff", label: "Ready to close", title: "project-y",
    detail: "All 27 tickets are merged. Close the project when you have checked the result.", meta: "Ready for 3 days",
    waited: "3 days", project: { id: "project-y", name: "project-y" },
    actions: [
      { label: "Open project", primary: false, href: "/projects/project-y" },
      { label: "Close project", primary: true, do: "close" },
    ],
  }),
  item("ready_to_start:project-z", {
    kind: "ready_to_start", label: "Ready to start", title: "project-z is planned and waits for the crew",
    detail: "Its tickets are written. Hand it over from your Claude Code session and the crew starts on it.",
    meta: "project-z", waited: "1 day", project: { id: "project-z", name: "project-z" }, from: { name: "Alex" },
    actions: [
      { label: "Open project", primary: false, href: "/projects/project-z" },
      { label: "Copy the hand-over command", primary: true, do: "copy", text: "/tares:ship project-z" },
    ],
  }),
  item("prs_to_review:docs-site", {
    kind: "prs_to_review", label: "Review", title: "2 PRs to review in docs-site",
    detail: "From the builder. The oldest has waited 2 days.", meta: "docs-site", waited: "2 days",
    project: { id: "docs-site", name: "docs-site" }, actions: [reviewsAction],
  }),
];

function project(id: string, over: Partial<HomeProject>): HomeProject {
  return {
    id, name: id, goal: "", state: "building", state_label: "Building", milestone: null,
    tickets: { merged: 0, blocked: 0, total: 0 }, repos: [], builders: 1, last_activity_at: null,
    last_activity: null, meta: "", can_close: false, ...over,
  };
}

const BUSY_PROJECTS: HomeProject[] = [
  project("project-x", {
    goal: "Publish 1,000 answer articles on the docs site.", meta: "Started 12 days ago",
    milestone: "Now on M4: 1,000 articles live", tickets: { merged: 15, blocked: 4, total: 21 },
  }),
  project("docs-site", {
    goal: "Give the console an edition for each kind of team.", meta: "Started 4 days ago",
    milestone: "Now on M1: Foundations", tickets: { merged: 34, blocked: 23, total: 61 },
  }),
  project("project-y", {
    goal: "Let a team share one crew.", state: "ready_to_close", state_label: "Ready to close",
    meta: "Ready for 3 days", milestone: "All milestones done", tickets: { merged: 27, blocked: 0, total: 27 },
    builders: 0, can_close: true,
  }),
  project("project-z", {
    goal: "Show every pull request in one list.", state: "waiting_on_you", state_label: "Waiting on you",
    meta: "Planned 1 day ago", milestone: "3 milestones", tickets: { merged: 0, blocked: 0, total: 9 }, builders: 0,
  }),
  project("project-w", {
    goal: "Move the settings page to the new layout.", state: "not_started", state_label: "Not started",
    meta: "Planned 5 days ago", milestone: "2 milestones", tickets: { merged: 0, blocked: 0, total: 0 }, builders: 0,
  }),
];

const MEMBERS: HomeCrewMember[] = [
  { station: "crew-lead", role: "orchestrator", name: "Lead", state: "idle", since: "1 minute ago",
    doing: "Waiting for work", memory_pct: 82, memory: "memory 82% full" },
  { station: "crew-reviewer", role: "reviewer", name: "Reviewer", state: "idle", since: "7 minutes ago",
    doing: "Nothing to review right now", memory_pct: 59, memory: "memory 59% full" },
  { station: "builder-project-x", role: "builder", name: "Builder, project-x", state: "working", since: "12 minutes ago",
    doing: "Working on the Projects list", memory_pct: 34, memory: "memory 34% full" },
];

const CREW: HomeCrew = {
  running: true, heard_at: "2026-11-02T08:59:00Z", heard: "1 minute ago", where: null,
  summary: "3 of 5 crew members are working or ready", members: MEMBERS,
  how_far: { autonomy: "L3-review", releases: false, text: "Up to merge, no releases" }, desk_open: 0,
};

const QUIET_CREW: HomeCrew = {
  ...CREW, running: true, heard_at: "2026-11-02T08:00:00Z", heard: "1 hour ago", summary: "Not heard from in 1 hour",
  members: MEMBERS.map((m) => ({ ...m, state: "not heard", since: "1 hour ago", doing: "" })),
};

const PLAN: HomeData["plan"] = {
  known: true,
  limits: [
    { kind: "session", label: "Current 5 hours", pct: 1, resets: "21:50", resets_at: "2026-11-02T21:50:00Z", text: "1% used · resets 21:50" },
    { kind: "weekly_all", label: "This week", pct: 11, resets: "Fri", resets_at: "2026-11-06T10:00:00Z", text: "11% used · resets Fri" },
  ],
  share_text: "This week: project-x 20%, project-y 72%",
};

const TOOLS: HomeData["tools"] = [
  { name: "GitHub", connected: true },
  { name: "Linear", connected: true },
  { name: "Claude Code", connected: true },
];

const BASE: HomeData = {
  edition: "factory",
  headline: "4 things wait for you. 2 projects are building and 1 is ready to close.",
  waiting_for_you: BUSY_WAITING, waiting_total: BUSY_WAITING.length,
  projects: BUSY_PROJECTS, finished_count: 4, crew: CREW, plan: PLAN, tools: TOOLS, now: NOW,
};

const LONG_GOAL =
  "Move every answer article off the old docs template, check each against the fact sheet, rewrite the ones that fail, "
  + "and keep the whole library under review so that a new reader finds the same answer in the docs, in search and in an "
  + "AI assistant, with the dates current and nothing that contradicts the product as it ships today, for all 1,000 articles.";
const LONG_NAME = "project-with-a-very-long-name-for-the-enterprise-edition-2026";

const many: HomeItem[] = Array.from({ length: 20 }, (_, i) =>
  i % 3
    ? item(`prs_to_review:project-${i}`, {
        kind: "prs_to_review", label: "Review", title: `${(i % 4) + 1} PRs to review in project-${i}`,
        detail: `From the builders. The oldest has waited ${20 - i} days.`, meta: `project-${i}`, waited: `${20 - i} days`,
        actions: [reviewsAction],
      })
    : item(`signoff:project-${i}`, {
        kind: "signoff", label: "Ready to close", title: `project-${i}`,
        detail: "All tickets are merged. Close the project when you have checked the result.", meta: `Ready for ${20 - i} days`,
        waited: `${20 - i} days`, actions: [{ label: "Open project", primary: true, href: `/projects/project-${i}` }],
      }),
);

export const busy: HomeData = BASE;

export const empty: HomeData = {
  ...BASE,
  headline: "No projects yet. Start one and hand it to the crew.",
  waiting_for_you: [], waiting_total: 0, projects: [], finished_count: 0,
  crew: {
    ...CREW, running: false, heard_at: null, heard: null, summary: "Not running",
    members: [{ station: "crew-lead", role: "orchestrator", name: "Lead", state: "ended", since: "",
                doing: "Starts when you hand over a project", memory_pct: null, memory: null }],
  },
  plan: { known: false, limits: [], share_text: null },
  tools: [{ name: "GitHub", connected: false }],
};

export const crewQuiet: HomeData = {
  ...BASE,
  headline: "The crew has not been heard from in 1 hour. 4 things wait for you.",
  crew: QUIET_CREW,
};

export const nothingWaiting: HomeData = {
  ...BASE,
  headline: "The crew is working on 5 tickets. Nothing waits for you.",
  waiting_for_you: [], waiting_total: 0, projects: BUSY_PROJECTS.slice(0, 2),
};

export const manyWaiting: HomeData = {
  ...BASE,
  headline: "20 things wait for you, the oldest for 20 days.",
  waiting_for_you: many, waiting_total: many.length,
};

export const longGoals: HomeData = {
  ...BASE,
  headline: "1 thing waits for you.",
  waiting_for_you: [
    item("prs_to_review:long", {
      kind: "prs_to_review", label: "Review", title: `4 PRs to review in ${LONG_NAME}`, detail: LONG_GOAL, meta: LONG_NAME,
      actions: [reviewsAction],
    }),
  ],
  waiting_total: 1,
  projects: [
    project(LONG_NAME, {
      name: LONG_NAME, goal: LONG_GOAL, meta: "Started 12 days ago",
      milestone: "Now on M1: A very long milestone name that should wrap and never push the bar out of its card",
      tickets: { merged: 3, blocked: 11, total: 15 },
    }),
  ],
};

/** The samples by the name `?sample=` gives them. Loading and error have no data and live in FactoryHome. */
export const HOME_SAMPLES: Record<string, HomeData> = {
  busy, empty, crewQuiet, nothingWaiting, manyWaiting, longGoals,
};
