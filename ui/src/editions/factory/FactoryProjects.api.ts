// The body of GET /api/factory/projects on a factory cell (tares/screens/factory_projects.py). The
// words (`state_label`, `milestone`, `meta`, `last_activity`) are worded for people and shown as
// they are. `name`, `goal`, `repo_names` and `milestone` carry a person's own text, and so do the
// names of the people (`lead`, `members`) and `yours_text`.

import { request } from "../../api";

export type ProjectState = "building" | "waiting_on_you" | "not_started" | "ready_to_close" | "later" | "done";

export interface FactoryProjectRow {
  id: string;
  name: string;
  icon?: string | null;
  goal: string;
  state: ProjectState;
  state_label: string;
  /** The line under the bar: "Now on M2: ...", "6 milestones", "All milestones done" or "Done 20 Oct";
   *  null for a project with no milestones. */
  milestone: string | null;
  /** Tickets by count group (tares/factory.py `progress`): merged only for a merge, closed for done
   *  without one, queued waits on another ticket, stuck a station stopped. `label` is the line
   *  ("5 merged, 2 queued, 1 stuck of 8", status_words.count_line). */
  tickets: { merged: number; closed: number; queued: number; stuck: number; total: number; label: string };
  /** The repos in short, for a place that is tight. The list shows `repo_names`. */
  repos: string[];
  builders: number;
  last_activity_at: string | null;
  last_activity: string | null;
  /** Builders, how long ago and pull requests waiting, as one line of words. */
  meta: string;
  /** True for a project whose tickets are all merged and that is not closed yet. */
  can_close: boolean;
  repo_names: string[];
  // A cell with more than one person adds these (TR-1277).
  lead?: { id: string; name: string } | null;
  /** The people on the project besides the lead. */
  members?: { id: string; name: string; picture?: string | null }[];
  scope?: "team" | "private";
  /** "2 tickets yours, 1 Alex's". */
  yours_text?: string;
  /** The person leads it, holds a ticket in it, or something in it waits for them. */
  mine?: boolean;
}

/** Where an open private project the person is not on would be: no name, no goal, only who leads it. */
export interface HiddenProject {
  hidden: true;
  lead: { name: string } | null;
}

/** Which projects a cell with more than one person lists: the person's, or the team's. */
export type ProjectsView = "mine" | "team";

/** What a cell with more than one person adds to the body. */
export interface ProjectsTeam {
  counts: Record<ProjectsView, number>;
  hidden: HiddenProject[];
  /** "1 private project you are not on, led by Lee". */
  hidden_summary?: string;
}

export interface FactoryProjectsData {
  projects: FactoryProjectRow[];
  counts: { all: number } & Record<ProjectState, number>;
  now: string;
  team?: ProjectsTeam;
}

/** Every factory project, in one read. */
export const factoryProjects = () => request<FactoryProjectsData>("/api/factory/projects");
