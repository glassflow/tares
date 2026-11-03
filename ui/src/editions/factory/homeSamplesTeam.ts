import type { HomeArea, HomeData, HomeItem, HomeProject, HomeProjectRow, HomeRef, HomeTeamCrew, HomeWaitingOn } from "./home.types";
import { busy, project } from "./homeSamples";

// Sample bodies of the factory home on a cell with more than one person (the keys tares/factory_team.py
// adds). They show only behind the sample flag, never on a live cell. Every name here is invented.

const crew = { crew: true, name: "Your crew" };
const alex = { id: "alex", name: "Alex" };
const kim = { id: "kim", name: "Kim" };
const lee = { id: "lee", name: "Lee" };
const projectX = { id: "project-x", name: "project-x" };
const projectY = { id: "project-y", name: "project-y" };
const projectZ = { id: "project-z", name: "project-z" };

function item(id: string, over: Partial<HomeItem> & Pick<HomeItem, "kind" | "label" | "title" | "detail" | "meta">): HomeItem {
  return { id, project: null, repo: null, from: crew, since: null, waited: "2 days", actions: [], ...over };
}

const asked: HomeItem[] = [
  item("question:q1", {
    kind: "question", label: "Question for you", title: "project-x, Q2", from: kim, project: projectX,
    detail: "Kim's spec asks about example-org/service-a, which you own: should seat counts live on the team or on each project?",
    meta: "Asked 40 minutes ago · project-x", waited: "40 minutes",
    actions: [{ label: "Answer", primary: true, do: "answer" }],
  }),
  item("signoff:project-x:service-a", {
    kind: "signoff", label: "Sign-off", title: "project-x", from: kim, project: projectX, repo: "example-org/service-a",
    detail: "3 tickets touch example-org/service-a. Kim cannot hand the project to a crew until you sign off.",
    meta: "Waiting 2 hours · your repo: example-org/service-a", waited: "2 hours",
    actions: [
      { label: "Read the 3 tickets", primary: false, href: "/projects/project-x" },
      { label: "Sign off", primary: true, do: "sign_off" },
    ],
  }),
  item("grant:g1", {
    kind: "grant", label: "Permission", title: "Try migrations on staging", from: kim, project: projectX,
    detail: "Kim's builder wants a standing permission that covers example-org/service-a. It starts once you agree.",
    meta: "Waiting 1 day · project grant on project-x, given by Kim", waited: "1 day",
    actions: [
      { label: "Decline", primary: false, do: "decline" },
      { label: "Agree", primary: true, do: "agree" },
    ],
  }),
  item("desk:d1", {
    kind: "desk", label: "Permission", title: "Allow the builder to try `make check`", project: projectY,
    detail: "The builder wants to use `make check` in project-y.", meta: "Waiting 5 minutes · project-y", waited: "5 minutes",
    actions: [{ label: "Allow", primary: true, do: "allow" }],
  }),
  item("milestone:project-y:m1", {
    kind: "milestone_to_check", label: "Milestone to check", title: "project-y, M1: API returns nicknames", project: projectY,
    detail: "Your builder merged the API ticket. Check it before Kim's web ticket starts.",
    meta: "Your crew · example-org/service-a", waited: "3 hours",
    actions: [{ label: "Check it", primary: true, href: "/projects/project-y" }],
  }),
  item("prs_to_review:project-y", {
    kind: "prs_to_review", label: "4 PRs to review", title: "project-y", project: projectY,
    detail: "Each one holds up a milestone of a project you lead.", meta: "Waiting 6 days · example-org/service-a", waited: "6 days",
    actions: [{ label: "Review", primary: true, href: "/reviews" }],
  }),
];

const handOver = { label: "Hand over anyway", primary: false, do: "hand_over_anyway" as const };

/** Two projects the person leads: one waits on Kim, one has a repo nobody owns. */
const waitingOn: HomeWaitingOn[] = [
  {
    project: projectY, title: "project-y",
    detail: "Waiting for Kim to sign off on example-org/service-b. 1 question open for Kim.",
    repos: [
      { repo: "example-org/service-a", owner: null, state: "yours" },
      { repo: "example-org/service-b", owner: kim, state: "waiting" },
      { repo: "example-org/service-d", owner: lee, state: "signed_off" },
    ],
    open_questions: [{ id: "q1", to: kim, asked_at: null, waited: "3 hours" }],
    actions: [handOver],
  },
  {
    project: projectZ, title: "project-z",
    detail: "example-org/service-c has no owner.",
    repos: [
      { repo: "example-org/service-a", owner: null, state: "yours" },
      { repo: "example-org/service-c", owner: null, state: "no_owner" },
    ],
    open_questions: [],
    actions: [handOver, { label: "Set an owner", primary: false, href: "/settings/advanced?tab=areas" }],
  },
];

// ── projects: the Team list, each row with its lead, members and yours_text ──

function row(id: string, lead: HomeRef, members: HomeRef[], over: Partial<HomeProject>): HomeProject {
  return project(id, { lead, members, scope: "team", yours: 0, yours_text: "", mine: false, ...over });
}

const TEAM_PROJECTS: HomeProject[] = [
  row("project-y", alex, [kim], {
    scope: "private", goal: "A nickname field on the API, shown on the web page.", state: "waiting_on_you", state_label: "Waiting on you",
    milestone: "M1 ready to check", tickets: { merged: 1, blocked: 1, total: 2 }, yours: 1, yours_text: "1 ticket yours, 1 Kim's", mine: true,
  }),
  row("project-x", kim, [alex], {
    goal: "Bill teams per seat, monthly or yearly.", state: "not_started", state_label: "Not started",
    milestone: "Waiting on your answer and sign-off", tickets: { merged: 0, blocked: 0, total: 7 }, yours: 3, yours_text: "3 tickets yours, 4 Kim's", mine: true,
  }),
  row("project-z", alex, [], {
    goal: "About 1,000 answer articles so AI assistants cite the product.", milestone: "Now on M4: 1,000 articles live",
    tickets: { merged: 13, blocked: 6, total: 20 }, yours: 20, yours_text: "20 tickets yours", mine: true,
  }),
  row("project-w", kim, [lee], {
    goal: "A new sign-in page for the web app.", milestone: "Now on M2: sign-in page", tickets: { merged: 4, blocked: 0, total: 9 },
  }),
  row("project-v", lee, [], {
    goal: "Move the build to a faster runner.", state: "not_started", state_label: "Not started", milestone: "3 milestones",
    tickets: { merged: 0, blocked: 0, total: 5 },
  }),
  row("project-u", kim, [], {
    goal: "Fix the date shown on invoices.", state: "ready_to_close", state_label: "Ready to close", milestone: "Ready to close",
    tickets: { merged: 6, blocked: 0, total: 6 },
  }),
];

const LONG_LEAD = { id: "alexandria", name: "Alexandria Kimberley-Lee-Hargreaves-Montgomery" };
const LONG_PROJECT = row("project-long", LONG_LEAD, [kim, lee], {
  name: "project-for-the-enterprise-edition-with-a-name-that-keeps-going-on-and-on-and-on-2026-v2-b",
  scope: "private", mine: true, yours: 2, yours_text: "2 tickets yours, 1 Alexandria Kimberley-Lee-Hargreaves-Montgomery's",
  goal: "A goal written as a long sentence that goes on to describe the whole piece of work in some detail so the row has to wrap onto several lines on a phone.",
  milestone: "Now on M2: a milestone with a long name that also has to wrap", tickets: { merged: 2, blocked: 1, total: 9 },
});

const LEADS = [alex, kim, lee];
const MANY_PROJECTS: HomeProject[] = Array.from({ length: 12 }, (_, i) => {
  const b = TEAM_PROJECTS[i % 6];
  const id = `project-${String.fromCharCode(97 + i)}`;
  return { ...b, id, name: id, lead: LEADS[i % 3], mine: i % 3 === 0, scope: i % 4 === 0 ? "private" : "team", yours_text: i % 3 === 0 ? "2 tickets yours" : "" };
});

const and = (xs: string[]) => (xs.length === 1 ? xs[0] : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/** The three keys a team cell adds for its projects: the list as the Team view has it (a stub for each private
 *  project the person is not on), the counts and the one line about the hidden ones. */
function projectKeys(rows: HomeProjectRow[]): Pick<HomeData, "projects" | "counts" | "hidden_summary"> {
  const shown = rows.filter((r): r is HomeProject => !("hidden" in r));
  const stubs = rows.filter((r) => "hidden" in r);
  const leads = [...new Set(stubs.flatMap((s) => ("hidden" in s && s.lead ? [s.lead.name] : [])))];
  return {
    projects: rows, counts: { mine: shown.filter((p) => p.mine).length, team: shown.length },
    hidden_summary: stubs.length ? `${stubs.length} private project${stubs.length > 1 ? "s" : ""} you are not on${leads.length ? `, led by ${and(leads)}` : ""}` : undefined,
  };
}

/** What the cell sends for `?projects=mine|team`: Mine drops the stubs and the projects that are not the person's. */
export function sampleView(data: HomeData, view: "mine" | "team"): HomeData {
  if (view === "team" || data.summary === undefined) return data;   // a cell of one has one list
  return { ...data, projects: data.projects.filter((r) => !("hidden" in r) && r.mine) };
}

// ── the side column: the crews as the cell lists them (the person's own first), the limit, the repos owned ──

function crewRow(who: HomeRef | null, name: string, machine: string, heard: string, doing: string, working = 0): HomeTeamCrew {
  return { id: `crew-${machine}`, name, machine, person: who, last_seen: "2026-11-02T08:58:00Z", heard, working, doing };
}

const OWN_CREW = crewRow(alex, "Your crew", "alex-mbp", "17 seconds ago", "Building project-z.", 1);
const KIM_CREW = crewRow(kim, "Kim's crew", "kim-mbp", "1 minute ago", "Building project-x and project-w.", 2);
const CREW_LIMIT = {
  mine: "you approve each merge", team: "you approve each merge",
  text: "The team allows up to merge. Yours can be stricter, not looser.",
};
const AREAS: HomeArea[] = [
  { repo: "example-org/service-a", note: "1 sign-off asked" },
  { repo: "example-org/service-d", note: "nothing waiting" },
];
const MORE_CREWS: HomeTeamCrew[] = [
  ["Sam", "sam-mbp", "4 minutes ago", "Building project-w."], ["Robin", "robin-mbp", "9 minutes ago", "Idle."],
  ["Jo", "jo-mbp", "20 minutes ago", "Building project-c."], ["Max", "max-mbp", "3 hours ago", "Idle."],
  ["Ana", "ana-mbp", "5 hours ago", "Waiting for an answer."],
].map(([n, m, h, d]) => crewRow({ id: n.toLowerCase(), name: n }, `${n}'s crew`, m, h, d));

const TEAM_KEYS = {
  summary: "6 things wait for you. You are waiting on Kim for 1 sign-off.",
  you_are_waiting_on: waitingOn,
  ...projectKeys(TEAM_PROJECTS),
  crews: [OWN_CREW, KIM_CREW],
  crews_quiet: ["Lee's crew"],
  crew_limit: CREW_LIMIT,
  areas: AREAS,
};

function team(items: HomeItem[], headline: string, over: Partial<HomeData> = {}): HomeData {
  return { ...busy, ...TEAM_KEYS, ...over, headline, waiting_for_you: items, waiting_total: items.length };
}

/** Three people: items from Kim, from the crew and one from Lee, each with its actions. */
export const threePeople: HomeData = team(
  [...asked, item("question:q2", {
    kind: "question", label: "Question for you", title: "project-y, Q1", from: lee, project: projectY,
    detail: "Which of the two sign-in pages should the web ticket start from?", meta: "Asked 3 days ago · project-y", waited: "3 days",
    actions: [{ label: "Answer", primary: true, do: "answer" }],
  })],
  "7 things wait for you, the oldest for 6 days.",
);

/** Nothing addressed to the person. */
export const nothingWaiting: HomeData = {
  ...team([], "The crew is working on 5 tickets. Nothing waits for you.", { you_are_waiting_on: [] }),
  summary: "Nothing waits for you.",
  areas: AREAS.map((a) => ({ ...a, note: "nothing waiting" })),
};

/** A question of about 600 characters, which wraps and never pushes the row wider than the card. */
const LONG_QUESTION =
  "Kim's spec asks how seat counts should work across the whole team, and the answer changes three tickets: should the "
  + "number of seats live on the team, so every project draws from one pool, or on each project, so a lead can give a "
  + "project more people without asking anyone? If it lives on the team, the billing page needs one number and the "
  + "projects page needs a way to show who holds a seat. If it lives on each project, the billing page adds the projects "
  + "up, a lead needs a place to change the number, and a project that goes over its seats needs a message that says "
  + "what to do next. Please name the one you want and say why, so the tickets can be written once.";

/** A project name of 90 characters, which wraps and never pushes the row wider than the card. */
const LONG_NAME = "project-for-the-enterprise-edition-with-a-name-that-keeps-going-on-and-on-and-on-2026-v2-b";

export const longText: HomeData = team(
  [item("question:long", {
    kind: "question", label: "Question for you", title: "project-with-a-very-long-name-for-the-enterprise-edition-2026",
    from: kim, project: projectX, detail: LONG_QUESTION, meta: "Asked 40 minutes ago · project-x", waited: "40 minutes",
    actions: [{ label: "Answer", primary: true, do: "answer" }],
  })],
  "1 thing waits for you.",
  {
    ...projectKeys([LONG_PROJECT, TEAM_PROJECTS[3]]),
    summary: "1 thing waits for you. You are waiting on Kim for 1 sign-off.",
    crews: [OWN_CREW, crewRow({ ...kim, name: "Alexandria Kimberley-Lee-Hargreaves-Montgomery" }, "Alexandria Kimberley-Lee-Hargreaves-Montgomery's crew",
                              "a-laptop-with-a-long-machine-name-2026", "1 minute ago", `Building ${LONG_NAME}.`, 1)],
    crews_quiet: ["Lee's crew with a name that is long enough to wrap onto a second line on a phone"],
    areas: [{ repo: "example-org/a-repository-with-a-very-long-name-service-b", note: "1 sign-off asked, 2 questions open, nothing else waits there" }],
    you_are_waiting_on: [{ ...waitingOn[0], project: { ...projectY, name: LONG_NAME }, title: LONG_NAME }],
  },
);

/** Thirty items: eight rows, then Show all 30. */
export const manyItems: HomeData = team(
  Array.from({ length: 30 }, (_, i) => item(`question:n${i}`, {
    kind: "question", label: "Question for you", title: `project-x, Q${i + 1}`, from: i % 2 ? kim : lee, project: projectX,
    detail: `Should ticket ${i + 1} wait for the sign-off on example-org/service-a?`,
    meta: `Asked ${i + 2} hours ago · project-x`, waited: `${i + 2} hours`,
    actions: [{ label: "Answer", primary: true, do: "answer" }],
  })),
  "30 things wait for you, the oldest for 31 hours.",
  { summary: "30 things wait for you.", you_are_waiting_on: [], ...projectKeys(MANY_PROJECTS),
    crews: [OWN_CREW, KIM_CREW, ...MORE_CREWS] },
);

/** A private project the person is not on: a quiet row in Team, one line under the list in Mine. */
export const privateNotOn: HomeData = team(
  asked.slice(0, 2), "2 things wait for you.",
  { summary: "2 things wait for you.", ...projectKeys([...TEAM_PROJECTS, { hidden: true, lead: { name: "Lee" } }]) },
);

/** The person owns no repo: the side column has no "Repos you own" block at all. */
export const noOwnedRepos: HomeData = team(asked.slice(0, 2), "2 things wait for you.", { summary: "2 things wait for you.", areas: [] });

/** The person's own crew went quiet too: the cell names it, the page never lists it among the team's. */
export const ownCrewQuiet: HomeData = team(
  asked.slice(0, 2), "2 things wait for you.",
  { summary: "2 things wait for you.", crews: [KIM_CREW], crews_quiet: ["Your crew", "Lee's crew"] },
);

/** A team cell whose Team list is only private projects the person is not on: quiet rows, never "No projects yet." */
export const privateOnly: HomeData = team(
  asked.slice(0, 2), "2 things wait for you.",
  { summary: "2 things wait for you.", ...projectKeys([{ hidden: true, lead: { name: "Lee" } }]) },
);

/** A cell of one person: no team keys, so the page keeps the v2 list. */
export const onePerson: HomeData = busy;

export const HOME_SAMPLES_TEAM: Record<string, HomeData> = {
  onePerson, threePeople, nothingWaitingTeam: nothingWaiting, longText, manyItems, privateNotOn, privateOnly, noOwnedRepos, ownCrewQuiet,
};
