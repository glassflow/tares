// The body of GET /api/factory/report on a factory cell (tares/report.py `page`). One read answers the
// whole page: every section's figures come under `sections`, and a section fetches nothing of its own.
// Project and team names are a person's own text, and the page shows every string as text.

import { ApiError, request } from "../../api";

export type SectionKey = "delivered" | "time" | "review" | "cost" | "merges" | "architecture";

/** What was delivered in some weeks. Durations are in hours, null with no ticket to say them from. */
export interface DeliveredFigures {
  merged: number;
  milestones_accepted: number;
  projects_merged: number;
  /** `tickets`: how many of `merged` have a start, which p50 and p90 are said from. */
  start_to_merge: { p50: number | null; p90: number | null; tickets: number };
  first_pass: { passed: number; reviewed: number };
  rounds_per_ticket: number | null;
}

/** `sections.delivered` (tares/report.py `delivered`). `previous` holds the figures of as many weeks
 *  just before the range, null when nothing is there. */
export interface DeliveredData extends DeliveredFigures {
  previous: DeliveredFigures | null;
  weeks: { week: string; merged: number; p50: number | null }[];
}

/** How review went in some weeks, counted as in DeliveredFigures. */
export type ReviewFigures = Pick<DeliveredFigures, "first_pass" | "rounds_per_ticket">;

/** The findings of one kind and severity, by what became of them so far. `kind` "other" holds every
 *  kind that has no name of its own. */
export interface FindingRow { kind: string; severity: string; fixed: number; open: number }

/** A kind of finding that came back in one repo: on `count` tickets. `rule_accepted` when a rule the
 *  caller can open was accepted for that repo and kind. */
export interface RecurringRow { repo: string; kind: string | null; count: number; projects: string[]; tickets: string[]; rule_accepted: boolean }

/** `sections.review` (tares/report.py `review`). `projects` come lowest pass rate first; `previous`
 *  is null for a project with no reviewed ticket in as many weeks just before the range. */
export interface ReviewData extends ReviewFigures {
  weeks: ({ week: string } & ReviewFigures)[];
  findings: FindingRow[];
  projects: ({ project: string; name: string; previous: ReviewFigures | null } & ReviewFigures)[];
  /** Over the last `days` days as of the cell's last pass, whatever the range; null before the first
   *  one. */
  recurring: null | { at: string; days: number; rows: RecurringRow[] };
}

/** The ticket stages whose time is counted. */
export type Stage = "doing" | "review" | "changes" | "blocked";

/** Something stuck: its number and label, when it started to be stuck (the question asked, the ticket
 *  moved into its stage) and its project, null for a question of none. */
export interface StuckRow {
  number: number | null;
  label: string;
  at: string;
  project: { id: string; name: string } | null;
}

/** `count` is all of them, `rows` the oldest five. */
export interface StuckList<T extends StuckRow> { count: number; rows: T[] }

/** `sections.time` (tares/report.py `time_spent`). Durations are in hours; percents are whole, null with
 *  nothing to divide. */
export interface TimeData {
  total: number;
  waiting_percent: number | null;
  stages: { stage: Stage; total: number; waiting: number; agent: number;
    share: number | null; waiting_percent: number | null; agent_percent: number | null }[];
  weeks: { week: string; share: Record<Stage, number | null>; waiting: Record<Stage, number | null> }[];
  /** `tickets`: how many of `merged` have a start, which the `buckets` hold. */
  start_to_merge: { merged: number; tickets: number; buckets: number[] };
  /** As of the cell's last pass, whatever the range; null before the first one. */
  stuck: null | {
    at: string;
    questions: StuckList<StuckRow & { text: string }>;
    tickets: StuckList<StuckRow & { title: string; stage: Stage }>;
  };
}

/** An amount of API-equivalent cost in US dollars. `unpriced` replies have no price and are in no
 *  amount, so an amount beside a count above 0 is "at least". */
export interface Money { cost_usd: number; unpriced: number }

export type CostRole = "builder" | "reviewer" | "orchestrator" | "releaser" | "person" | "other";

/** One of the most expensive tickets or milestones. `week` is the Monday of its first merge or its
 *  latest acceptance, `hours` the time up to then from the first start, null with no start. */
export interface CostedRow extends Money {
  project: string; project_name: string; week: string; rounds: number; hours: number | null;
}
export interface CostedTicket extends CostedRow { number: number | null; label: string; title: string }
export interface CostedMilestone extends CostedRow { id: string; name: string; tickets: number }

/** `sections.cost` (tares/report.py `spend` and `_costs_now`). `projects` come highest spend first;
 *  `project` "" is spend of no project. */
export interface CostData {
  total: Money;
  roles: (Money & { role: CostRole })[];
  projects: (Money & { project: string; name: string })[];
  weeks: (Money & { week: string; projects: Record<string, Money> })[];
  /** Ticket and milestone costs as of the cell's last pass, over the chosen range; null before the
   *  first one. */
  by_ticket: null | {
    at: string;
    weeks: { week: string; merged: number; mean_cost_usd: number | null; unpriced_tickets: number }[];
    milestones: { accepted: number; mean_cost_usd: number | null; unpriced_milestones: number };
    top_tickets: CostedTicket[];
    top_milestones: CostedMilestone[];
    /** What is tied to no ticket; `no_project` is the part of no project, on the whole factory only. */
    untied: Money & { no_project: Money | null };
  };
}

/** The autonomy level pull requests are merged at. */
export type MergeLevel = "L3-review" | "L4-ship";

/** One merge through the merge queue. Null is what nothing recorded; the `_words` fields say the
 *  level, the gate and the approver for people. */
export interface MergeRow {
  project: string; name: string; ticket: string | null; title: string | null;
  repo: string; pr: number; url: string | null; merged_at: string;
  level: MergeLevel | null; reviewed_sha: string | null; merged_sha: string | null; rounds: number | null;
  ci_state: string | null; outside: boolean | null;
  approver: { id: string; name: string } | null; approved_at: string | null;
  level_words: string; gate_words: string; approver_words: string;
}

/** `sections.merges` (tares/report.py `merges`). `rows` are the newest of the `total` merges and `csv`
 *  holds every one. `levels` is each project's level as it is now. */
export interface MergesData {
  total: number;
  rows: MergeRow[];
  csv: string;
  levels: { project: string; name: string; level: MergeLevel; level_words: string }[];
  weeks: ({ week: string; unrecorded: number } & Record<MergeLevel, number>)[];
}

/** A decision a person made. `name` is its project's, `answer` the choice picked, else their words,
 *  `tickets` the labels it was recorded with. */
export interface ReportDecision {
  project: string; name: string; question: string; answer: string; at: string;
  person: { id: string; name: string } | null; tickets: string[];
}

/** `decisions` are the newest of the `decisions_total` decisions. */
export interface DecisionList { decisions_total: number; decisions: ReportDecision[] }

/** One repo's architecture findings and decisions. `repo` null holds the findings of tickets with no
 *  pull request. `weeks` and `total` count findings first raised in the range. */
export interface ArchitectureRepo extends DecisionList {
  repo: string | null; total: number; weeks: { week: string; count: number }[]; rising: boolean;
}

/** `sections.architecture` (tares/report.py `architecture`). `repos` come rising first, then most
 *  findings; `unplaced` holds the decisions whose tickets have no pull request. */
export interface ArchitectureData { repos: ArchitectureRepo[]; unplaced: DecisionList }

export interface ReportData {
  /** The weeks the read covers: `from` is a Monday, `to` a Sunday, `weeks` the Mondays, oldest first.
   *  `running_week` is the Monday of the week that is not over, when the range holds it. */
  range: { from: string; to: string; weeks: string[]; running_week: string | null; merged: number };
  /** `value` is the scope asked for, `count` its projects; `teams` and `projects` are what the caller
   *  may pick. */
  scope: { value: string; name: string | null; count: number; teams: { id: string; name: string }[]; projects: { id: string; name: string }[] };
  cannot_see: string[];
  /** A section is here once the cell builds its figures. */
  sections: Partial<Record<SectionKey, unknown>>;
}

/** The cell would not read what the address asks for: 400 a bad date or scope, 404 a project or team
 *  the caller cannot see. `message` is the cell's own sentence. */
export type ReportRefused = { refused: 400 | 404; message: string };

export const isRefused = (r: ReportData | ReportRefused): r is ReportRefused => "refused" in r;

/** The whole page in one read. A refusal is an answer, so the page can say what is wrong with the
 *  address; any other failure is thrown. */
export const factoryReport = (query: string): Promise<ReportData | ReportRefused> =>
  request<ReportData>(`/api/factory/report?${query}`).catch((e) => {
    if (e instanceof ApiError && (e.status === 400 || e.status === 404)) return { refused: e.status, message: e.message };
    throw e;
  });
