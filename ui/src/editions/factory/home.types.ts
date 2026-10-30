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
  | "allow"
  | "answer"
  | "sign_off"
  | "agree"
  | "decline"
  | "hand_over_anyway"
  | "close";

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
  from: (Partial<HomeRef> & { crew?: boolean; name: string }) | null;
  since: string | null; // ISO time it began to wait
  waited: string; // "just now", "5 minutes", "6 days"
  actions: HomeAction[];
}

export type HomeProjectState = "building" | "waiting_on_you" | "not_started" | "ready_to_close";

export interface HomeProject {
  id: string;
  name: string;
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
}

export interface HomeTool {
  name: string;
  connected: boolean;
}

export interface HomeData {
  edition: "factory";
  headline: string;
  waiting_for_you: HomeItem[];
  waiting_total: number;
  projects: HomeProject[];
  finished_count: number;
  crew: HomeCrew;
  plan: HomePlan;
  tools: HomeTool[];
  now: string;
}
