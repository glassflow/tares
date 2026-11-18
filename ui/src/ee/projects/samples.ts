// Tares enterprise licence, see ee/LICENSE
import type { FactoryProjectRow, FactoryProjectsData, HiddenProject } from "@/editions/factory/FactoryProjects.api";
import { body, BUSY, LONG, MANY, row } from "@/editions/factory/projectsSamples";

// Sample bodies of the Projects page on a cell with more than one person (the keys
// tares/screens/factory_projects.py adds through the team seam, TR-1277). They show only behind the
// sample flag, never on a live cell. Every name here is invented; the people are the home samples'.

const alex = { id: "alex", name: "Alex" };
const kim = { id: "kim", name: "Kim" };
const lee = { id: "lee", name: "Lee" };
type Person = typeof alex;

interface Team { lead: Person; members?: Person[]; scope?: "team" | "private"; yours_text?: string; mine: boolean }

const withTeam = (p: FactoryProjectRow, t: Team): FactoryProjectRow => ({
  ...p, lead: t.lead, members: t.members ?? [], scope: t.scope ?? "team", yours_text: t.yours_text ?? "", mine: t.mine,
});

/** A body with the team keys, the counts of each view read from its rows. */
function team(projects: FactoryProjectRow[], hidden: HiddenProject[] = [], hidden_summary?: string): FactoryProjectsData {
  return {
    ...body(projects),
    team: {
      counts: { mine: projects.filter((p) => p.mine).length, team: projects.length },
      hidden, ...(hidden_summary ? { hidden_summary } : {}),
    },
  };
}

// The busy list, each row with who leads it: three are the person's (they lead two, hold tickets in
// the third), one private.
const TEAM: Record<string, Team> = {
  p1: { lead: kim, members: [alex, lee], yours_text: "3 tickets yours, 4 Kim's", mine: true },
  p2: { lead: alex, members: [kim], scope: "private", mine: true },
  p3: { lead: alex, mine: true },
  p4: { lead: kim, members: [lee], mine: false },
  p5: { lead: lee, mine: false },
  p6: { lead: kim, mine: false },
  p7: { lead: alex, mine: true },
  p8: { lead: lee, members: [kim], mine: false },
};
const BUSY_TEAM = BUSY.map(row).map((p) => withTeam(p, TEAM[p.id]));

const LEE_STUB: HiddenProject = { hidden: true, lead: { name: "Lee" } };
const LEE_LINE = "1 private project you are not on, led by Lee";

/** Three people, the busy list with its leads. */
const threePeople = team(BUSY_TEAM);

/** The same, and a private project of Lee's the person is not on: a stub in Team, a line in Mine. */
const privateNotOn = team(BUSY_TEAM, [LEE_STUB], LEE_LINE);

/** Nothing the person can see, only the private projects of others. */
const privateOnly = team([], [LEE_STUB, { hidden: true, lead: { name: "Kim" } }],
  "2 private projects you are not on, led by Lee and Kim");

/** A cell of one: no team keys, the plain rows. */
const onePerson = body(BUSY.map(row));

/** Long names: the lead, the people and the words about the person's tickets wrap, never scroll. */
const LONG_LEAD = { id: "long", name: "Maximiliana Alexandrovna Konstantinopolskaya-Wellington" };
const longText = team(
  LONG.map(row).map((p, i) => withTeam(p, {
    lead: i === 0 ? LONG_LEAD : kim, members: [alex, lee, LONG_LEAD], mine: true,
    yours_text: "12 tickets yours, 9 Maximiliana Alexandrovna Konstantinopolskaya-Wellington's",
  })),
  [{ hidden: true, lead: { name: LONG_LEAD.name } }],
  `1 private project you are not on, led by ${LONG_LEAD.name}`,
);

/** Sixty projects, every other one the person's. */
const manyItems = team(MANY.map(row).map((p, i) => withTeam(p, {
  lead: [alex, kim, lee][i % 3], scope: i % 7 === 0 ? "private" : "team", mine: i % 2 === 0,
})));

/** What ee adds to the Projects page's samples (registered in ee/index.ts). */
export const PROJECTS_SAMPLES_EE: Record<string, FactoryProjectsData> = {
  threePeople, privateNotOn, privateOnly, onePerson, longText, manyItems,
};
