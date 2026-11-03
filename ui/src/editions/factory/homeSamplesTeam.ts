import type { HomeData, HomeItem } from "./home.types";
import { busy } from "./homeSamples";

// Sample bodies of the factory home on a cell with more than one person (the keys tares/factory_team.py
// adds). They show only behind the sample flag, never on a live cell. Every name here is invented.

const crew = { crew: true, name: "Your crew" };
const kim = { id: "kim", name: "Kim" };
const lee = { id: "lee", name: "Lee" };
const projectX = { id: "project-x", name: "project-x" };
const projectY = { id: "project-y", name: "project-y" };

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

const TEAM_KEYS = {
  summary: "6 things wait for you. You are waiting on Kim for 1 sign-off.",
  you_are_waiting_on: [],
  counts: { mine: 2, team: 3 },
  crews: [],
  crews_quiet: [],
  areas: [],
};

function team(items: HomeItem[], headline: string): HomeData {
  return { ...busy, ...TEAM_KEYS, headline, waiting_for_you: items, waiting_total: items.length };
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
  ...team([], "The crew is working on 5 tickets. Nothing waits for you."), summary: "Nothing waits for you.",
};

/** A question of about 600 characters, which wraps and never pushes the row wider than the card. */
const LONG_QUESTION =
  "Kim's spec asks how seat counts should work across the whole team, and the answer changes three tickets: should the "
  + "number of seats live on the team, so every project draws from one pool, or on each project, so a lead can give a "
  + "project more people without asking anyone? If it lives on the team, the billing page needs one number and the "
  + "projects page needs a way to show who holds a seat. If it lives on each project, the billing page adds the projects "
  + "up, a lead needs a place to change the number, and a project that goes over its seats needs a message that says "
  + "what to do next. Please name the one you want and say why, so the tickets can be written once.";

export const longText: HomeData = team(
  [item("question:long", {
    kind: "question", label: "Question for you", title: "project-with-a-very-long-name-for-the-enterprise-edition-2026",
    from: kim, project: projectX, detail: LONG_QUESTION, meta: "Asked 40 minutes ago · project-x", waited: "40 minutes",
    actions: [{ label: "Answer", primary: true, do: "answer" }],
  })],
  "1 thing waits for you.",
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
);

/** A cell of one person: no team keys, so the page keeps the v2 list. */
export const onePerson: HomeData = busy;

export const HOME_SAMPLES_TEAM: Record<string, HomeData> = {
  onePerson, threePeople, nothingWaitingTeam: nothingWaiting, longText, manyItems,
};
