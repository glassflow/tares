// The body of GET /api/factory/settings on a factory cell (tares/screens/factory_settings.py).
// Every string is worded for people and shown as it is. The words of a grant are the person's own.
// A change goes through the routes that already exist, one field at a time.

import { request } from "../../api";

export interface SettingsTool {
  key: "github" | "linear" | "slack" | "claude_code";
  name: string;
  connected: boolean;
  detail: string;
  /** What it is used for here: "The open projects build in glassflow/tares and glassflow/docs"; null when not connected. */
  covers: string | null;
  href: string;
}

/** One thing the crew was allowed. `question` is set when the words only answer a desk question. */
export interface SettingsGrant {
  id: string;
  text: string;
  project: string | null;
  scope_text: string;
  given: string;
  state_text: string;
  question?: string;
}

export type Autonomy = "L3-review" | "L4-ship";

export interface SettingsCrew {
  how_far: { value: Autonomy; releases: boolean; text: string };
  release_profile: boolean;
  builders_max: number;
  builders_text: string;
  challenger: boolean;
  pace: boolean;
  pace_text: string;
  grants: SettingsGrant[];
  grants_total: number;
}

export interface SettingsEdition {
  value: string;
  label: string;
  can_switch: boolean;
  options: { value: string; label: string }[];
  other: { value: string; label: string };
  note: string;
}

export interface SettingsPage {
  can_change: boolean;
  tools: SettingsTool[];
  crew: SettingsCrew;
  models: { default_provider: string; chosen: boolean; budget_text: string };
  people: { keys: { count: number; text: string }; cloud_team_href: string | null };
  edition: SettingsEdition;
  advanced: { label: string; text: string; href: string }[];
}

export const factorySettings = () => request<SettingsPage>("/api/factory/settings");

/** The fields the crew's settings route changes on this page; each call sends only the one given. */
export type CrewChange = Partial<{ autonomy: Autonomy; releaser: boolean; challenger: boolean; builders_max: number; pace: boolean }>;

export const changeCrew = (change: CrewChange) =>
  request<unknown>("/api/crew/settings", { method: "PUT", body: JSON.stringify(change) });

export const addGrant = (words: string) =>
  request<unknown>("/api/grants", { method: "POST", body: JSON.stringify({ words, scope: "all" }) });

export const switchEdition = (edition: string) =>
  request<unknown>("/api/settings/edition", { method: "PUT", body: JSON.stringify({ edition }) });
