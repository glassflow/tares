// Sample data for the Crew page, shown at /crew?sample=<name> when the browser has the sample flag
// (see sample.ts). It is invented: project-x, project-y, Alex, Kim.

import type { CrewBuilders, CrewMember, CrewPage, CrewPlan, CrewProposal, MemberDetail, MemberState } from "./FactoryCrew.api";

export const SAMPLES = ["busy", "fold", "l3", "pace", "quiet", "none", "many", "long", "load", "err"] as const;
export const isSample = (s: string | null): s is string => !!s && (SAMPLES as readonly string[]).includes(s);

const NOW = "2026-11-02T10:00:00+00:00";
const TEXT: Record<MemberState, (since: string) => string> = {
  working: () => "Working",
  idle: (s) => `Idle for ${s}`,
  "waiting for you": () => "Waiting for you",
  "not heard": (s) => `Not heard from in ${s}`,
  ended: (s) => `Ended ${s} ago`,
};

let n = 0;
interface Spec {
  role?: string; state?: MemberState; since?: string; doing: string; heard?: string | null; mem?: number | null; helpers?: number;
}
const member = (s: Spec): CrewMember => {
  const state = s.state ?? "idle";
  const mem = s.mem === undefined ? 40 : s.mem;
  return {
    station: `sample-${++n}`, role_label: s.role ?? "Builder", state, state_text: TEXT[state](s.since ?? "8 minutes"),
    doing: s.doing, heard: s.heard === undefined ? "8 minutes ago" : s.heard, memory_pct: mem,
    memory_text: mem === null ? null : `Memory ${mem}% full`, helpers: s.helpers ?? 0,
  };
};

const LEAD = () => member({ role: "Orchestrator", state: "working", doing: "Handing out the tickets of project-x and answering the builders", heard: "12 seconds ago", mem: 82 });
const REVIEWER = () => member({ role: "Reviewer", state: "idle", since: "1 minute", doing: "Reviews every pull request, one project after another", heard: "1 minute ago", mem: 41 });
const RELEASER = () => member({ role: "Releaser", state: "idle", since: "2 hours", doing: "Ships a milestone once you say so", heard: "2 hours ago", mem: 22 });

const X = (): CrewMember[] => [
  member({ doing: "Waiting for the review of Edition routes and Settings", mem: 63 }),
  member({ state: "working", doing: "Drawing the mocks for Reviews, Crew and Settings", heard: "1 minute ago", mem: 58, helpers: 1 }),
  member({ state: "working", doing: "Building the shell for the second edition", heard: "just now", mem: 34 }),
  member({ state: "ended", since: "1 day", doing: "Finished its work.", heard: "1 day ago", mem: null }),
  member({ state: "ended", since: "3 days", doing: "Finished its work.", heard: "3 days ago", mem: null }),
];
const Y = (): CrewMember[] => [
  member({ state: "not heard", since: "1 day", doing: "Wave 6: 100 articles, changes asked", heard: "1 day ago", mem: 88 }),
];
const group = (name: string, members: CrewMember[]): CrewBuilders => ({ project: { id: name, name }, members });

const PLAN: CrewPlan = {
  known: true, share_text: null,
  limits: [
    { kind: "session", label: "Current 5 hours", pct: 1, resets: "21:50", resets_at: null, text: "1% used, resets 21:50" },
    { kind: "weekly_all", label: "This week", pct: 11, resets: "Friday", resets_at: null, text: "11% used, resets Friday" },
  ],
  by_project: [
    { name: "project-y", share_text: "72%", cost_text: "$44 at API prices" },
    { name: "project-x", share_text: "20%", cost_text: "$12 at API prices" },
    { name: "project-z", share_text: "8%", cost_text: "$5 at API prices" },
  ],
};

const PROPOSALS: CrewProposal[] = [
  { id: "p1", text: "Put the milestone check in the ticket before asking for review.", from: "the reviewer", when: "2 days ago" },
  { id: "p2", text: "Never merge on a red check, even when the change looks small.", from: "the reviewer", when: "6 hours ago" },
];
const STANDUP = { text: "In the last day: 4 tickets done, 2 in review. Projects that moved: project-x, project-y.", when: "The last 24 hours" };
const L4 = { text: "The crew merges after its review and green CI.", detail: "No releases. Up to 4 builders at once.", settings_href: "/settings#crew" };

const page = (p: Partial<CrewPage>): CrewPage => ({
  summary: "Working, last heard 12 seconds ago", talk_to: LEAD(), reviewer: REVIEWER(), releaser: null,
  builders: [group("project-x", X()), group("project-y", Y())], how_far: L4, pacing: null, plan: PLAN,
  rule_proposals: PROPOSALS, standup: STANDUP, now: NOW, ...p,
});

const LONG_DO = "Moving every answer article off the old docs template, checking each against the fact sheet and rewriting the ones that fail so a reader finds the same answer in the docs, in search and in an assistant";
const quiet = (m: CrewMember): CrewMember => ({ ...m, state: "not heard", state_text: "Not heard from in 2 hours", heard: "2 hours ago" });

const MANY = ["project-a", "project-b", "project-c", "project-d"].map((p, i) => group(p, [
  member({ state: i % 2 ? "idle" : "working", since: `${i * 5 + 3} minutes`, doing: `Ticket title number ${i * 2 + 1}`, heard: `${i + 1} minutes ago`, mem: 30 + i * 15 }),
  member({ state: "working", doing: `Ticket title number ${i * 2 + 2}`, heard: "1 minute ago", mem: 50 + i * 10 }),
]));

const NONE = page({
  summary: "Not started", talk_to: null, reviewer: null, builders: [], plan: { known: false, limits: [], share_text: null, by_project: [] },
  rule_proposals: [], standup: null,
});

export function sampleLoad(name: string): Promise<CrewPage> {
  n = 0;
  switch (name) {
    case "load": return new Promise(() => undefined);
    case "err": return Promise.reject(new Error("sample failure"));
    case "none": return Promise.resolve(NONE);
    case "l3":
      return Promise.resolve(page({
        reviewer: member({ role: "Reviewer", state: "waiting for you", doing: "4 reviewed pull requests wait for you to merge", heard: "3 minutes ago", mem: 41 }),
        releaser: RELEASER(), builders: [group("project-x", X().slice(0, 2))],
        how_far: { text: "You approve each merge.", detail: "Releases wait for you. Up to 4 builders at once.", settings_href: "/settings#crew" },
      }));
    case "pace":
      return Promise.resolve(page({
        pacing: "Pacing: the 5-hour window is at 86%, so no new builder starts until 21:50.",
        builders: [group("project-x", X().slice(0, 3))], rule_proposals: [],
        plan: { ...PLAN, limits: [
          { kind: "session", label: "Current 5 hours", pct: 86, resets: "21:50", resets_at: null, text: "86% used, resets 21:50" },
          { kind: "weekly_all", label: "This week", pct: 61, resets: "Friday", resets_at: null, text: "61% used, resets Friday" },
        ] },
      }));
    case "quiet":
      return Promise.resolve(page({
        summary: "Not heard from in 2 hours", talk_to: quiet(LEAD()), reviewer: quiet(REVIEWER()),
        builders: [group("project-x", X().slice(0, 2).map(quiet))], rule_proposals: [], standup: null,
      }));
    case "many": return Promise.resolve(page({ builders: MANY }));
    case "long":
      return Promise.resolve(page({
        builders: [group("customer-connectors-for-the-enterprise-edition-2026",
          [member({ state: "working", doing: LONG_DO, heard: "2 minutes ago", mem: 91 })])],
        rule_proposals: [{ id: "p1", from: "the reviewer", when: "2 days ago",
          text: "When a change touches more than one repository, open the pull requests together and say in each one which other pull request has to merge first, so nobody merges half of a change." }],
        standup: { text: `${LONG_DO}. ${LONG_DO}.`, when: "The last 24 hours" },
      }));
    default: return Promise.resolve(page({})); // busy, fold
  }
}

/** The member's lines in a sample: what the fold shows. */
export function sampleDetail(): Promise<MemberDetail> {
  const at = (hm: string) => `2026-11-02T${hm}:00+00:00`;
  return Promise.resolve({
    attach: "tares factory crew attach project-x-build-m2",
    recent: [
      { at: at("11:42"), type: "tool_use", text: "Read ui/src/editions/WORDS.md" },
      { at: at("11:43"), type: "tool_use", text: "Edit ui/src/editions/factory/FactoryHome.tsx" },
      { at: at("11:44"), type: "tool_use", text: "Bash: cd ui && npx tsc --noEmit" },
      { at: at("11:44"), type: "tool_result", text: "error TS2322: Type 'string | null' is not assignable to type 'string'." },
      { at: at("11:45"), type: "assistant", text: "Fixing the type, then checking again." },
      { at: at("11:47"), type: "assistant", text: "Message to the reviewer: pull request ready <b>not markup</b>" },
    ],
  });
}
