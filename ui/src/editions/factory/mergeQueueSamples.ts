// Sample data for the Merge queue tab, shown at /projects/<any>/github?tab=queue&sample=<name> when the
// browser has the sample flag (see sample.ts). It is invented: example-org, Lee, Dana, ORD-41. Answering
// a row on a sample calls nothing, so the page shows the state and nothing changes.

import type { MergeQueueData, MqAnswer, MqChipKey, MqGroup, MqRow, MqState } from "./FactoryMergeQueue.api";
import type { ProjectGitHubData, TicketTurnFields } from "./FactoryProject.api";

const API = "example-org/api";
const WEB = "example-org/web";
const GOOD = { review: "Approved by the crew", ci: "Passed", guardrail: "Allowed" };

interface Spec {
  n: number; ref: string; title: string; state: MqState; chip: [MqChipKey, string];
  pos?: number | null; status?: string; lead?: string; note?: string; builder?: string; branch?: string;
  review?: string; ci?: string; guardrail?: string; answer?: MqAnswer | null; bounce?: number; needs?: boolean;
}

const LADDER = [
  "Sent back {n} of 3 times. At 3 the orchestrator takes it.",
  "Sent back {n} times. The orchestrator is on it. At 5 it comes to you.",
  "Sent back {n} times. It is now a question for you on the Calls page.",
];

// whose turn, read off the row the way the screen reads it (the queue state, a recorded answer)
const turnOf = (s: Spec): TicketTurnFields => {
  const k = s.chip[0];
  if (k === "approval") return { turn: "ready_for_you", turn_label: "Ready for you", whose: "person" };
  if (s.state === "merging") return { turn: "merging", turn_label: "Merging", whose: "crew" };
  if (k === "approved") return { turn: "next_to_merge", turn_label: "Next to merge", whose: "crew" };
  if (k === "moved") return { turn: "reviewing", turn_label: "Reviewing", whose: "crew" };
  if (k === "hold" || s.state === "blocked") return { turn: "stuck", turn_label: "Stuck", whose: "person" };
  if (s.state === "sent_back") return { turn: "changes_asked", turn_label: "Changes asked", whose: "crew" };
  if (s.state === "waiting_ci") return { turn: "checks_running", turn_label: "Checks running", whose: "github" };
  const after = /^Waiting for (.+) to merge/.exec(s.status ?? "");
  return after ? { turn: "queued", turn_label: `Queued after ${after[1]}`, whose: "nobody" }
    : { turn: "merging", turn_label: "Merging", whose: "crew" };
};

const row = (s: Spec): MqRow => ({
  id: `row-${s.n}`, position: s.pos === undefined ? 1 : s.pos, state: s.state, chip: { key: s.chip[0], text: s.chip[1] },
  ticket: { ref: s.ref, title: s.title }, pr: { number: s.n, url: `https://github.com/${API}/pull/${s.n}` },
  branch: s.branch ?? `${s.ref.toLowerCase()}-change`,
  builder: s.builder ? { id: null, name: s.builder, text: `Built by ${s.builder}` } : null,
  review: s.review ?? GOOD.review, ci: s.ci ?? GOOD.ci, guardrail: s.guardrail ?? GOOD.guardrail,
  status: { lead: s.lead ?? "", text: s.status ?? "", note: s.note ?? "" },
  answer: s.answer ?? null,
  bounce: s.bounce ? { count: s.bounce, text: LADDER[s.bounce < 3 ? 0 : s.bounce < 5 ? 1 : 2].replace("{n}", String(s.bounce)) } : null,
  needs_you: !!s.needs,
  ...turnOf(s),
});

const mine = (commit: string, id: string): MqAnswer => ({
  kind: "mine", lead: "Waiting for your yes.", text: "Waiting for your yes.",
  note: `Asked 14 minutes ago for commit ${commit}. Your answer counts for this commit only; a new commit asks again. It is the same question as on Overview and the project page, so answering it here closes it there too.`,
  item: { id, label: null }, can_answer: true, for: null, asked: commit, now: commit, decision: null, by: null, where: null, when: "", commit,
});
const withdrawn = (commit: string, id: string): MqAnswer => ({
  ...mine(commit, id), kind: "withdrawn", lead: "The question was withdrawn, so this pull request is on hold.",
  text: "The question was withdrawn, so this pull request is on hold.", note: "Approve it here, or a new commit asks again.",
});
const other = (commit: string): MqAnswer => ({
  ...mine(commit, "D2"), kind: "other", can_answer: false, lead: "Waiting for Dana’s yes.",
  text: "Waiting for Dana’s yes. Only the person this question is for can answer it.",
  note: `Asked 2 minutes ago for commit ${commit}. You see it read only; the answer shows here once it is given.`,
});

const group = (repo: string, base: string, rows: MqRow[], extra: Partial<MqGroup> = {}): MqGroup => {
  const line = rows.filter((r) => r.state !== "sent_back").length;
  return {
    repo, base, title: `${repo} into ${base}`, count: `${line} in line`, auto: "", sub: "Merges one at a time, in order.",
    line, you: rows.filter((r) => r.needs_you && r.state !== "blocked").length, blocked: rows.filter((r) => r.state === "blocked").length,
    sent: rows.filter((r) => r.state === "sent_back").length, rows, merged: null, ...extra,
  };
};

const INTRO = "One pull request merges at a time for each repo and base branch, in milestone and ticket order. No agent and no tokens are used.";
const EMPTY = "Nothing is waiting to merge. A pull request shows here once its review is approved.";
const FOOTER = [
  "Milestone pull requests from factory-modular/* are merged by you.",
  "That is the whole queue. Merged and closed pull requests are on the Pull requests tab.",
];

const data = (groups: MqGroup[]): MergeQueueData => {
  const line = groups.reduce((n, g) => n + g.line, 0);
  const need = groups.reduce((n, g) => n + g.you + g.blocked, 0);
  const needText = `${need} need${need === 1 ? "s" : ""} you`;
  return {
    project: { id: "sample", name: "Orders platform rebuild" },
    aside: groups.length ? `${line} in line${need ? `, ${needText}` : ""}` : "",
    intro: groups.length ? INTRO : "", empty: groups.length ? "" : EMPTY,
    tab: { line, need, need_text: need ? needText : "", note: groups.length
      ? `${line} pull request${line === 1 ? " is" : "s are"} in the merge queue${need ? `, ${needText}` : ""}.` : "Nothing is in the merge queue." },
    footer: FOOTER, groups,
  };
};

const MIXED_API = [
  row({ n: 301, ref: "ASK-1", title: "Backport price rounding to 2.4", state: "waiting_approval", chip: ["approval", "Needs your yes"], pos: 1,
        lead: "Waiting for your yes.", status: "Waiting for your yes.", note: mine("301a000", "D1").note, builder: "Lee",
        answer: mine("301a000", "D1"), needs: true }),
  row({ n: 302, ref: "ASK-2", title: "Backport tax rounding to 2.4", state: "waiting_approval", chip: ["other", "Waiting for Dana"], pos: 2,
        lead: other("302a000").lead, status: other("302a000").text, note: other("302a000").note, builder: "Dana", answer: other("302a000") }),
  row({ n: 217, ref: "ORD-41", title: "Round prices to the order currency", state: "waiting_ci", chip: ["ci", "Waiting for CI"], pos: 3,
        status: "Branch updated to main 3 minutes ago, waiting for CI on the new commit.", ci: "Pending, 1 of 3 checks", review: "Approved by the crew" }),
  row({ n: 219, ref: "ORD-43", title: "Add currency to the order export", state: "queued", chip: ["next", "Next"], pos: 4, status: "Next to merge.", builder: "Dana" }),
  row({ n: 221, ref: "ORD-44", title: "Add a column to the export", state: "queued", chip: ["queued", "Queued"], pos: 5, status: "In line behind #219.", builder: "Lee" }),
  row({ n: 311, ref: "BLK-1", title: "Use one rounding rule everywhere", state: "blocked", chip: ["blocked", "Blocked"], pos: 6,
        lead: "Blocked by repo rules:", status: "Blocked by repo rules: GitHub says the base branch requires two approving reviews.",
        note: "Fix the rule in GitHub, or merge it by hand.", needs: true }),
  row({ n: 312, ref: "BLK-3", title: "Move the price rules into one file", state: "sent_back", chip: ["sent_back", "Sent back"], pos: null,
        lead: "Sent back to the builder:", status: "Sent back to the builder: conflicts with PR 218 in app/pricing.py.", bounce: 3 }),
];

const MIXED_WEB = [
  row({ n: 88, ref: "BLK-2", title: "Show the shipping estimate", state: "blocked", chip: ["blocked", "Blocked"], pos: 1,
        lead: `No GitHub connection can write to ${WEB}.`, status: `No GitHub connection can write to ${WEB}.`,
        note: "One question on the Calls page covers every blocked pull request in this repo.", needs: true }),
];

const LONG = row({
  n: 222, ref: "ORD-45", state: "waiting_approval", chip: ["approval", "Needs your yes"], pos: 1, needs: true,
  title: "Make the order export keep the currency of every line when a customer pays in two currencies and the shop rounds each line on its own",
  branch: `ord-45-${"a-very-long-branch-name-".repeat(4)}end`, builder: "Lee",
  lead: "Waiting for your yes.", status: "Waiting for your yes.", note: mine("222a000", "D9").note, answer: mine("222a000", "D9"),
});

const MANY = Array.from({ length: 8 }, (_, i) => row({
  n: 331 + i, ref: `MNY-${i + 1}`, title: `Queued pull request number ${i + 1}`, state: "queued",
  chip: [i === 0 ? "next" : "queued", i === 0 ? "Next" : "Queued"], pos: i + 1,
  status: i === 0 ? "Next to merge." : `In line behind #${330 + i}.`,
}));

const MERGED = { title: "Merged just now, 1", items: [{ n: 214, tk: "ORD-38", title: "Fix tax total on refunds", when: "9 minutes ago", by: "the merge queue" }] };

const SAMPLES: Record<string, MergeQueueData | "load" | "err"> = {
  "queue-mixed": data([group(API, "main", MIXED_API, { merged: MERGED }), group(WEB, "main", MIXED_WEB)]),
  "queue-withdrawn": data([group(API, "main", [row({
    n: 303, ref: "ASK-3", title: "Backport the export column", state: "blocked", chip: ["hold", "On hold"], pos: 1, needs: true,
    lead: withdrawn("303a000", "D3").lead, status: withdrawn("303a000", "D3").text, note: withdrawn("303a000", "D3").note, answer: withdrawn("303a000", "D3"),
  })])]),
  "queue-long": data([group(API, "main", [LONG, MIXED_API[3]])]),
  "queue-many": data([group(API, "main", MANY)]),
  "queue-empty": data([]),
  "queue-load": "load", "queue-err": "err",
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** What a read of the queue answers for a sample: the data, never an answer (`queue-load`), or an error (`queue-err`). */
export function sampleLoad(name: string): Promise<MergeQueueData> {
  const s = SAMPLES[name];
  if (s === "load") return new Promise(() => {});
  if (s === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(s);
}

/** The pull requests the Pull requests tab shows next to a queue sample. */
export const samplePrs = (): Promise<ProjectGitHubData> => Promise.resolve({
  id: "sample", name: "Orders platform rebuild", counts: { open: 4, merged: 12, closed: 1 }, checked: "40 seconds ago", github_error: null,
  now: "2026-11-02T10:00:00+00:00",
  repos: [{
    name: API, url: `https://github.com/${API}`, counts: { open: 4, merged: 12, closed: 1 }, added: 340, removed: 120,
    prs: [{
      number: 217, url: `https://github.com/${API}/pull/217`, repo: API, title: "Round prices to the order currency", ticket: "ORD-41",
      ticket_title: "Round prices to the order currency", ticket_href: "tickets/ORD-41", state: "open", state_label: "Open", ci: "CI running",
      review: "round 1: passed", branch: "ord-41-price-rounding", base: "main", head: null, when: "opened 3 hours ago by Lee", size: null, error: null,
    }],
  }],
});
