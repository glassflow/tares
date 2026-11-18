import type { StartBlock, StartStep } from "../StartChecklist.api";
import type { HomeCrew, HomeCrewMember, HomeData, HomeItem, HomeOnboarding, HomeProject, OnboardingMember } from "./home.types";

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

export function project(id: string, over: Partial<HomeProject>): HomeProject {
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
  title: "Your Claude plan",
};

const BASE: HomeData = {
  edition: "factory",
  headline: "4 things wait for you. 2 projects are building and 1 is ready to close.",
  waiting_for_you: BUSY_WAITING, waiting_total: BUSY_WAITING.length,
  projects: BUSY_PROJECTS, finished_count: 4, crew: CREW, plan: PLAN, now: NOW,
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
        waited: `${20 - i} days`,
        // The first row lists its primary action first, so the page has to put it last.
        actions: i === 0
          ? [{ label: "Close project", primary: true, do: "close" }, { label: "Open project", primary: false, href: `/projects/project-${i}` }]
          : [{ label: "Open project", primary: true, href: `/projects/project-${i}` }],
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
  plan: { known: false, limits: [], share_text: null, title: "Your Claude plan" },
  plan_command: "tares factory spec",
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

// The first-run steps, as the daemon words them (tares/start_steps.py): four steps, done ones in place
// without buttons.
const STEP_KEYS = ["setup", "install", "login", "crew"];
const STEPS: Record<string, Omit<StartStep, "done">> = {
  setup: { key: "setup", title: "Set up your factory", where: "workspace",
    gets: "How your crew works, your GitHub, and where tickets, reviews and checks live.",
    actions: [{ label: "Set up your factory", to: "/setup" }] },
  install: { key: "install", title: "Install Tares", where: "terminal",
    gets: "The command that starts and watches the crew.",
    actions: [{ label: "Copy the command", copy: "uv tool install tares" }] },
  login: { key: "login", title: "Sign your machine in", where: "terminal",
    gets: "So the crew on your machine reports here.",
    actions: [{ label: "Copy the command", copy: "tares login" }] },
  crew: { key: "crew", title: "Start the crew", where: "terminal",
    gets: "It reads your choices from this workspace and asks only what belongs to your machine.",
    actions: [{ label: "Copy the command", copy: "tares factory crew up" }] },
};

/** The block with the steps named in `done` ticked, and `details` added to the steps they name. */
function startBlock(done: string[], details: Record<string, string> = {}, hidden = false): StartBlock {
  const steps: StartStep[] = STEP_KEYS.map((k) => {
    const isDone = done.includes(k);
    return { ...STEPS[k], done: isDone, actions: isDone ? [] : STEPS[k].actions, ...(details[k] ? { detail: details[k] } : {}) };
  });
  const n = steps.filter((s) => s.done).length;
  const pending = n < steps.length;
  return { steps, done: n, total: steps.length, show: pending && !hidden, hidden: pending && hidden };
}

const NO_MEMBER: OnboardingMember = { state: "none", last_at: null };
const RUNNING_MEMBER: OnboardingMember = { state: "running", last_at: "2026-11-02T09:40:00Z" };
const FRESH: HomeOnboarding = {
  choices: { chosen: false, autonomy: null, releaser: null, builders_max: null,
             defaults: { autonomy: "L3-review", releaser: false, builders_max: 2 } },
  github: { connected: false, text: "" },
  machine: { name: "", version: "", at: null },
  crew: { machine: "", orchestrator: NO_MEMBER, reviewer: NO_MEMBER, builders: { live: 0, max: 2 }, up: false },
};
const CHOSEN: HomeOnboarding = {
  ...FRESH,
  choices: { ...FRESH.choices, chosen: true, autonomy: "L3-review", releaser: false, builders_max: 2 },
  github: { connected: true, text: "GitHub connected as example-org" },
};
const SIGNED_IN: HomeOnboarding = {
  ...CHOSEN,
  machine: { name: "example-laptop", version: "1.60.0", at: "2026-11-02T09:30:00Z" },
  crew: { ...CHOSEN.crew, machine: "example-laptop" },
};
const COMING_UP: HomeOnboarding = {
  ...SIGNED_IN, crew: { ...SIGNED_IN.crew, orchestrator: RUNNING_MEMBER, reviewer: { state: "starting", last_at: null } },
};
const CREW_UP: HomeOnboarding = {
  ...SIGNED_IN, crew: { ...SIGNED_IN.crew, orchestrator: RUNNING_MEMBER, reviewer: RUNNING_MEMBER, up: true },
};

const LONG_DETAIL =
  "GitHub connected as example-org. The connection covers example-org/service-a, example-org/edge-proxy, example-org/docs-site "
  + "and 40 more repositories, and was last used 3 minutes ago to read a review on a pull request in example-org/service-a. "
  + "Open Settings, GitHub to change which repositories the crew can reach, or to disconnect it for everyone. "
  + "Nothing changes for a pull request that is already open, and the crew keeps reading its reviews and its checks "
  + "until the connection is removed.";

const DONE_TEXT = {
  setup: "You approve each merge, 2 builders, no releases. GitHub connected as example-org",
  install: "Installed: tares 1.60.0.",
  login: "Signed in from example-laptop.",
};
const FIRST_THREE = ["setup", "install", "login"];

/** A new cell: nothing done, so the list shows all five steps and the form starts on the defaults. */
export const startNothing: HomeData = { ...empty, start: startBlock([]), onboarding: FRESH };

/** Choices saved, GitHub connected and tickets placed, Tares installed and signed in: the crew command is next. */
export const startThree: HomeData = { ...empty, start: startBlock(FIRST_THREE, DONE_TEXT), onboarding: SIGNED_IN };

/** The crew command ran: the orchestrator is up and the reviewer is starting. */
export const startComingUp: HomeData = { ...empty, start: startBlock(FIRST_THREE, DONE_TEXT), onboarding: COMING_UP };

/** The crew is up and waits for a plan: the page says so and links to Settings and the Crew page. */
export const startCrewUp: HomeData = {
  ...empty,
  start: startBlock([...FIRST_THREE, "crew"], { ...DONE_TEXT, crew: "The orchestrator and the reviewer are running." }),
  onboarding: CREW_UP,
};

/** A step with a long detail: it clamps to three lines and the full text is in its title. */
export const startLong: HomeData = {
  ...empty, start: startBlock(["choices", "connections"], { connections: LONG_DETAIL }), onboarding: CHOSEN,
};

/** Everything done: the block says not to show, so the page has no list. */
export const startDone: HomeData = { ...nothingWaiting, start: startBlock(STEP_KEYS), onboarding: CREW_UP };

/** The person hid the list: the page shows one line with Show it again where the list was. */
export const startHidden: HomeData = { ...empty, start: startBlock(["setup"], {}, true), onboarding: CHOSEN };

/** The samples by the name `?sample=` gives them. Loading and error have no data and live in FactoryHome. */
export const HOME_SAMPLES: Record<string, HomeData> = {
  busy, empty, crewQuiet, nothingWaiting, manyWaiting, longGoals,
  startNothing, startThree, startComingUp, startCrewUp, startLong, startDone, startHidden,
};
