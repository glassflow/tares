// The body of GET /api/factory/projects on a factory cell (tares/screens/factory_projects.py). The
// words (`state_label`, `milestone`, `meta`, `last_activity`) are worded for people and shown as
// they are. `name`, `goal`, `repo_names` and `milestone` carry a person's own text.

import { request } from "../../api";

export type ProjectState = "building" | "waiting_on_you" | "not_started" | "ready_to_close" | "done";

export interface FactoryProjectRow {
  id: string;
  name: string;
  goal: string;
  state: ProjectState;
  state_label: string;
  /** The line under the bar: "Now on M2: ...", "6 milestones", "All milestones done" or "Finished 20 Oct";
   *  null for a project with no milestones. */
  milestone: string | null;
  tickets: { merged: number; blocked: number; total: number };
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
}

export interface FactoryProjectsData {
  projects: FactoryProjectRow[];
  counts: { all: number } & Record<ProjectState, number>;
  now: string;
}

/** Every factory project, in one read. */
export const factoryProjects = () => request<FactoryProjectsData>("/api/factory/projects");
