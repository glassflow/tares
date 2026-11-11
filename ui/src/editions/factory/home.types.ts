import type { ReactNode } from "react";
import type { StartBlock } from "../StartChecklist.api";

// The body of GET /api/home on a factory cell (tares/factory_home.py). Every string is worded for
// people and shown as it is.

export type HomeItemKind =
  | "prs_to_review"
  | "ready_to_start"
  | "calls_to_confirm"
  | "question"
  | "permission"
  | "milestone_to_check"
  | "signoff"
  | "grant"
  | "desk";

// What a button does on the page when it is not a link.
export type HomeVerb =
  | "copy"
  | "keep_assumptions"
  | "keep_batch"
  | "allow"
  | "answer"
  | "choose"
  | "agree"
  | "decline"
  | "close"
  | "later";

// A link (`href`) or something the page does (`do`), never both.
export interface HomeAction {
  label: string;
  primary: boolean;
  href?: string;
  do?: HomeVerb;
  [extra: string]: unknown;
}

export interface HomeRef {
  id: string;
  name: string;
  /** A project's emoji, when the ref is a project and it has one. */
  icon?: string | null;
}

export interface HomeItem {
  id: string;
  kind: HomeItemKind;
  label: string;
  title: string;
  detail: string;
  meta: string;
  project: HomeRef | null;
  repo: string | null;
  // who it comes from; {crew: true, name: "Your crew"} for the crew
  from: (Partial<HomeRef> & { crew?: boolean; name: string; picture?: string | null }) | null;
  since: string | null; // ISO time it began to wait
  waited: string; // "just now", "5 minutes", "6 days"
  actions: HomeAction[];
  /** The tickets the item is about (the pull requests to review); empty for an item about no ticket. */
  tickets?: string[];
  /** Short lines shown one under the other below the detail (the calls in a "calls to confirm" item). */
  lines?: string[];
  /** Holds crew work up until the person answers; such items come first (TR-1021). */
  blocking?: boolean;
  /** Waited more than a week and holds nothing up: folded under "N older things". */
  older?: boolean;
  /** A desk question's options and the crew's recommendation, answered on the row. */
  question?: { desk: string; options: string[]; recommendation: string | null; blocks: string | null } | null;
}

export type HomeProjectState = "building" | "waiting_on_you" | "not_started" | "ready_to_close";

export interface HomeProject {
  id: string;
  name: string;
  icon?: string | null;
  goal: string;
  state: HomeProjectState;
  state_label: string;
  milestone: string | null; // "Now on M2: search", "3 milestones", "Ready to close"
  tickets: { merged: number; blocked: number; total: number };
  repos: string[];
  builders: number;
  last_activity_at: string | null;
  last_activity: string | null; // "27 minutes ago"
  meta: string; // "1 builder · docs-site · active 2 minutes ago"
  can_close: boolean;
  // A cell with more than one person adds these.
  lead?: HomeRef | null;
  members?: HomeRef[]; // besides the lead
  scope?: "team" | "private";
  yours?: number; // tickets assigned to the person
  yours_text?: string; // "2 tickets yours, 1 Alex's"
  mine?: boolean; // the person leads it, holds a ticket in it, or it waits on them
}

// What stands where a private project the person is not on would be: no name, no goal, only who
// leads it. Shown in the Team view of the projects.
export interface HomeHiddenProject {
  hidden: true;
  lead: { name: string } | null;
}

export type HomeProjectRow = HomeProject | HomeHiddenProject;

// A project the person leads with a question open for someone else, so it is not ready to start.
export interface HomeWaitingOn {
  project: HomeRef;
  title: string;
  detail: string;
  open_questions: { id: string; to: HomeRef | null; asked_at: string | null; waited: string }[];
  actions: HomeAction[];
  no_lead?: boolean; // nobody leads the project; it shows here because it falls to the cell's first person
}

export interface HomeTeamCrew {
  id: string; // the crew, one per person and machine
  name: string; // "Your crew", "Kim's crew"
  machine: string;
  person: HomeRef | null;
  last_seen: string | null;
  heard: string; // "4 minutes ago"
  working: number;
  doing: string; // "Building Atlas."
}

// The limit on how far a crew may go, in words: the person's own and the team's.
export interface HomeCrewLimit {
  mine: string;
  team: string;
  text: string;
}

export interface HomeArea {
  repo: string;
  note: string; // "1 question open", "nothing waiting"
}

// "not heard" is a station silent for a while; "waiting" says why in `doing`.
export type HomeCrewState = "working" | "idle" | "waiting" | "not heard" | "ended";

export interface HomeCrewMember {
  station: string;
  role: string;
  name: string;
  state: HomeCrewState;
  since: string;
  doing: string;
  memory_pct: number | null;
  memory: string | null;
}

export interface HomeCrew {
  running: boolean;
  heard_at: string | null;
  heard: string | null;
  where: string | null;
  summary: string;
  members: HomeCrewMember[];
  how_far: { autonomy: string; releases: boolean; text: string };
  desk_open: number;
}

export interface HomePlanLimit {
  kind: "session" | "weekly_all";
  label: string;
  pct: number;
  resets: string | null;
  resets_at: string | null;
  text: string; // "27% used · resets 14:00"
}

export interface HomePlan {
  known: boolean;
  limits: HomePlanLimit[];
  share_text: string | null; // "This week: project-x 75%, project-y 25%"
  title: string; // "Your Claude plan", "Kim's Claude plan", or "Claude plan" when the cell cannot tell whose it is
}

export interface HomeTool {
  key: "github" | "linear" | "slack" | "claude_code";
  name: string;
  connected: boolean;
  /** What it is connected to, after the name on the chip: "glassflow", "last session 5 minutes ago". */
  short: string | null;
}

export interface HomeData {
  edition: "factory";
  headline: string;
  waiting_for_you: HomeItem[];
  waiting_total: number;
  // One sentence on what waits for the person; only a cell with more than one person sends it.
  summary?: string;
  // Only a cell with more than one person sends the keys below. `projects=mine|team` picks the view.
  you_are_waiting_on?: HomeWaitingOn[];
  // In the Team view of a team cell the list also holds HomeHiddenProject stubs (see
  // HomeProjectRow); narrow on `"hidden" in row` before reading a project field.
  projects: HomeProjectRow[];
  counts?: { mine: number; team: number };
  hidden_summary?: string; // "1 private project you are not on, led by Lee"
  crews?: HomeTeamCrew[];
  crews_quiet?: string[]; // crews not heard from in a day
  crew_limit?: HomeCrewLimit;
  areas?: HomeArea[];
  finished_count: number;
  later_count?: number; // projects kept for later, off the home
  crew: HomeCrew;
  plan: HomePlan;
  tools: HomeTool[];
  // The Start checklist (tares/start_steps.py); absent in older answers and in samples that skip it.
  start?: StartBlock;
  now: string;
}

/** What the team's part of the home gives the page: the agree and decline buttons, and sending the
 *  answer to a question a project's spec asked. `act` says whether it took the action. */
export interface TeamHomeHandler {
  act(action: HomeAction, item: HomeItem): boolean;
  answer(project: string, question: string, words: string): Promise<unknown>;
}

/** Which projects a cell with more than one person lists on the home. */
export type ProjectsView = "mine" | "team";

/** A sample of the home: the data, or the data for the view the person picked (a team sample). */
export type HomeSample = HomeData | ((view: ProjectsView) => HomeData);

// What the core home gives each slot of the team home (ee/home). The map of slot names to these is
// SlotProps in src/slotProps.ts; the ee components take the same types through ee/home/home.types.ts.

export interface HomeProjectsSwitchProps {
  view: ProjectsView; counts: { mine: number; team: number }; onView?: (v: ProjectsView) => void;
}
export interface HomeProjectLeadProps { p: HomeProject; me?: string }
export interface HomeHiddenRowsProps { stubs: HomeHiddenProject[]; hidden?: string; view: ProjectsView }
export interface HomeCrewLimitProps { limit: HomeCrewLimit }
export interface HomeTeamCrewsProps { crews: HomeTeamCrew[]; quiet: string[] }
export interface HomeReposProps { areas: HomeArea[] }
export interface HomeWaitingProps {
  items: HomeItem[];
  /** What a button of a row is; the page decides what a click does. */
  renderAction: (a: HomeAction, item: HomeItem) => ReactNode;
  /** A refusal's sentence by row id, shown under that row. */
  errors?: Record<string, string | undefined>;
  /** A panel open under a row, by row id (the answer box). */
  panels?: Record<string, ReactNode>;
}
export interface HomeWaitingOnProps { entries: HomeWaitingOn[] }
/** The page's one call for a row: work shown, a refusal, the toast. */
export type HomeRun = (
  item: HomeItem, verb: "agree" | "decline", call: () => Promise<unknown>, message: string, finish: () => void,
) => Promise<void>;
export interface HomeTeamActionsProps {
  run: HomeRun;
  clearError: (id: string) => void;
  rows: Record<string, { busy?: HomeVerb; error?: string; copied?: boolean }>;
  onHandler: (h: TeamHomeHandler | null) => void;
  /** The row whose dialog is open, so the page shows its refusal in the dialog and not under the row too. */
  onDialog: (id: string | null) => void;
}
