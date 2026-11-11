// Sample data for the Crew page, shown at /crew?sample=<name> when the browser has the sample flag
// (see sample.ts). It is invented: project-x, project-y, Alex, Kim.

import type { CrewBuilders, CrewMember, CrewPage, CrewPlan, CrewProposal, MemberDetail, MemberState } from "./FactoryCrew.api";

export const SAMPLES = ["busy", "fold", "finished", "l3", "pace", "quiet", "none", "many", "long", "team", "load", "err"] as const;
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
  { id: "p1", text: "Put the milestone check in the ticket before asking for review.", from: "the reviewer", when: "2 days ago",
    for: "For project-x", tickets: ["project-x T3", "project-x T5", "project-x T9"] },
  { id: "p2", text: "Never merge on a red check, even when the change looks small.", from: "the reviewer", when: "6 hours ago",
    for: "For every project", tickets: ["project-x T2", "project-y T4", "project-y T6", "project-z T1", "project-z T8", "project-x T11"] },
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

const done = (hours: number): CrewMember => member({ state: "ended", since: `${hours} hours`, doing: "Finished its work.", heard: `${hours} hours ago`, mem: null });
// A group that has had many builders, the live ones in the middle; one with only finished ones; one with none finished.
const FINISHED = () => [
  group("project-x", Array.from({ length: 19 }, (_, i) => {
    if (i === 6) return member({ state: "working", doing: "Building the shell for the second edition", heard: "just now", mem: 34 });
    if (i === 11) return member({ state: "not heard", since: "14 hours", doing: "Waiting for the review of Edition routes and Settings", heard: "14 hours ago", mem: 77 });
    return done(i + 1);
  })),
  group("project-y", [done(3), done(5)]),
  group("project-z", [member({ state: "working", doing: "Drawing the mocks for Reviews", heard: "1 minute ago", mem: 50 })]),
];

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
    case "finished": return Promise.resolve(page({ builders: FINISHED() }));
    case "many": return Promise.resolve(page({ builders: MANY }));
    case "team": {
      // two people's crews with the same station names (TR-1223): the caller's runs, Adam's ended
      const ended = (m: CrewMember): CrewMember => ({ ...m, state: "ended", state_text: "Ended 1 hour ago", doing: "Finished its work.",
        heard: "1 hour ago", memory_pct: null, memory_text: null });
      const mine = { id: "c1", person: { id: "p1", name: "Ashish" }, machine: "ashish-mbp", machine_shown: false, mine: true,
        summary: "Working, last heard 12 seconds ago", talk_to: LEAD(), reviewer: REVIEWER(), releaser: null,
        builders: [group("factory-is-modular", X())] };
      const theirs = { id: "c2", person: { id: "p2", name: "Adam" }, machine: "adam-mbp", machine_shown: false, mine: false,
        summary: "Not running", talk_to: ended(LEAD()), reviewer: ended(REVIEWER()), releaser: null, builders: [] };
      return Promise.resolve(page({ crews: [mine, theirs], summary: mine.summary, talk_to: mine.talk_to, reviewer: mine.reviewer,
        builders: mine.builders }));
    }
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

const SAMPLE_LOG = Array.from({ length: 120 }, (_, i) => `ok ${i + 1} - claude_update: frozen kit case ${i + 1}`).join("\n");

/** The member's session in a sample, as the fold shows it: a conversation (TR-1177). */
export function sampleDetail(): Promise<MemberDetail> {
  const at = (hm: string) => `2026-11-02T${hm}:00+00:00`;
  const tool = (id: string, hm: string, name: string, summary: string, input: unknown, output: string | null,
                ms: number | null, failed = false, more = false) => ({
    kind: "tool" as const, at: at(hm), id, name, summary, input: JSON.stringify(input, null, 2), output, failed, ms, more,
  });
  return Promise.resolve({
    attach: "tares factory crew attach project-x-build-m2",
    recent: [],
    conversation_from: 0,
    conversation: [
      { kind: "divider", at: at("09:40"), text: "Session started" },
      { kind: "asked", at: at("09:41"), from: "crew-orchestrator", quiet: false,
        text: "Take TR-1174: make cc_frozen_midround set its known difference aside. The working doc is in the ticket." },
      { kind: "said", at: at("09:43"),
        text: "The difference is the one skill file the frozen kit holds back on purpose. I'll teach the check to read the known list instead of counting it as a failure." },
      tool("t1", "09:44", "Edit", "tests/factory/claude_update_test.sh",
        { file_path: "tests/factory/claude_update_test.sh", old_string: "check cc_frozen_midround \\", new_string: "check cc_frozen_midround --known \"$KNOWN_DIFFS\" \\" },
        "- check cc_frozen_midround \\\n+ check cc_frozen_midround --known \"$KNOWN_DIFFS\" \\", 31),
      tool("t2", "09:46", "Bash", "bash tests/factory/claude_update_test.sh",
        { command: "bash tests/factory/claude_update_test.sh", timeout: 600000 },
        "FAIL cc_frozen_midround: unknown flag --known\n111 passed, 1 failed", 92000, true),
      { kind: "said", at: at("09:46"), text: "The `check` helper doesn't take flags yet. Adding `--known` to it in `lib.sh`." },
      tool("t3", "09:47", "Edit", "tests/factory/lib.sh", { file_path: "tests/factory/lib.sh" }, "Updated tests/factory/lib.sh", 28),
      tool("t4", "09:48", "Bash", "bash tests/factory/claude_update_test.sh", { command: "bash tests/factory/claude_update_test.sh" },
        SAMPLE_LOG.slice(0, 400), 88000, false, true),
      tool("t5", "09:49", "Bash", "git push -u origin tr-1174", { command: "git push -u origin tr-1174" }, "branch 'tr-1174' set up to track 'origin/tr-1174'.", 2400),
      tool("t6", "09:50", "SendMessage", "crew-reviewer", { to: "crew-reviewer", message: "PR #595 is ready." }, "Sent.", 120),
      { kind: "said", at: at("09:50"), text: "Opened #595 and asked the reviewer. 112/112 on the frozen kit, with the known difference listed in the output." },
      { kind: "asked", at: at("09:58"), from: "crew-reviewer", quiet: false,
        text: "Approved #595. One nit: name the variable KNOWN_DIFFERENCES so it matches the docs." },
      { kind: "divider", at: at("10:02"), text: "Memory compacted" },
      { kind: "asked", at: at("10:05"), from: "The factory", quiet: true,
        text: "Builder tick: follow the Tick section of your skill." },
      { kind: "said", at: at("10:06"), text: "Renaming it now." },
      { kind: "said", at: at("10:07"), text: "Told the reviewer: pull request ready <b>not markup</b>" },
    ],
  });
}

/** One sample call's whole output. */
export function sampleToolOutput(id: string): Promise<string> {
  return Promise.resolve(id === "t4" ? SAMPLE_LOG : "");
}
