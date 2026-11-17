// The body of GET /api/factory/setup (tares/screens/factory_setup.py, TR-871): every string the
// "Set up your factory" screen shows, in one read. The console only draws it. A save goes through the
// routes that already exist (tares/integrations_api.py), one connection at a time.

import { request } from "../../api";
import type { StartStep } from "../StartChecklist.api";
import type { HomeOnboarding } from "./home.types";

export type SetupSlot = "tickets" | "review" | "ci" | "memory" | "monitoring" | "guardrails" | "runtime";

export type FieldKind = "text" | "secret" | "url" | "select" | "bool" | "mapping";

export interface SetupRow {
  key: string;
  label: string;
  aria_label: string;
  value: string;
}

/** One setting an integration asks for. A secret never has a `value`, only `saved`. */
export interface SetupField {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  /** "Required" or "Optional", as the screen shows it. */
  requirement: string;
  placeholder: string;
  help: string;
  /** The sentence shown when this field is left empty or wrong. */
  error: string;
  options: string[];
  rows: SetupRow[];
  value?: string | boolean | null;
  saved: boolean;
  saved_note: string;
  /** "github_repo": the value is picked from the repos GitHub lets the cell see (setupGithubRepos). */
  picker?: string;
  /** The help, placeholder and empty-field line while the list is shown instead of the text box. */
  picker_help?: string;
  picker_placeholder?: string;
  picker_loading?: string;
  /** "{n} more. ..." under a list cut short. */
  picker_more?: string;
}

export interface SetupForm {
  mode: "connect" | "edit";
  title: string;
  lead: string;
  save_label: string;
  /** True when the saved connection becomes the step's default. */
  save_as_default: boolean;
  test_hint: string;
  fields: SetupField[];
}

export interface SetupApp {
  url: string;
  service: string;
  app_name: string;
  button: string;
  repo_step: boolean;
  steps: string[];
  note: string;
  use_key_label: string;
  use_app_label: string;
  stage: "start" | "done";
  done: string | null;
}

export interface SetupTag {
  key: "default" | "built_in" | "licence_ended" | "not_licensed" | "app" | "key";
  text: string;
}

export interface SetupAction {
  key: "edit" | "make_default" | "keep" | "remove";
  label: string;
}

export interface SetupConfirm {
  title: string;
  text: string;
  yes: string;
  no: string;
  moves_projects: boolean;
}

export interface SetupConnected {
  id: string;
  title: string;
  description: string;
  tags: SetupTag[];
  default: boolean;
  built_in: boolean;
  via: "app" | "key" | null;
  who: string;
  picked_by: { uid: string; name: string }[];
  follows: number;
  use: string;
  actions: SetupAction[];
  blocked: { key: string; text: string } | null;
  confirm: SetupConfirm | null;
  form: SetupForm | null;
  last_test: null;
}

export interface SetupAvailable {
  id: string;
  title: string;
  description: string;
  enterprise: boolean;
  allowed: boolean;
  button: string;
  reason: string | null;
  app: SetupApp | null;
  form: SetupForm | null;
}

export interface SetupStep {
  slot: SetupSlot;
  number: number;
  name: string;
  why: string;
  state: "set" | "not_set";
  set: boolean;
  line: string;
  note: string | null;
  banner: { lead: string; text: string } | null;
  connected_heading: string;
  connected: SetupConnected[];
  available_heading: string;
  available: SetupAvailable[];
  footer: { hint: string; next: string };
}

export interface SetupData {
  title: string;
  exit_label: string;
  edition: { key: string; name: string; cloud: boolean };
  licence: { state: "valid" | "ended" | "none"; ended_on: string | null; text: string };
  /** All ten steps: the seven below, the crew's choices, GitHub, and the terminal commands. */
  progress: SetupProgress;
  /** The home's onboarding block and its four terminal steps, for the steps around the integrations. */
  onboarding?: HomeOnboarding;
  terminal?: StartStep[];
  /** GitHub and Slack, for the Integrations page's Code and Notifications. */
  tools?: Partial<Record<"github" | "slack", { connected: boolean; detail: string }>>;
  steps: SetupStep[];
}

export interface SetupProgress { set: number; total: number; text: string }

/** What a form sends for one field: text, a secret ("" keeps the saved one), a real bool, or a mapping. */
export type SettingValue = string | boolean | Record<string, string>;

export const setupRead = () => request<SetupData>("/api/factory/setup");

/** The count the Setup menu entry shows. Admins only: any other key gets a refusal, and no entry. */
/** The repos the GitHub Issues form offers, of `credential` or else of the GitHub App connections. */
export const setupGithubRepos = (via: "app" | "key", credential = "") =>
  request<{ repos: string[]; error: string; note: string }>(
    `/api/factory/setup/github/repos?via=${via}&credential=${encodeURIComponent(credential)}`);
export const setupProgress = () => request<SetupProgress & { projects: number }>("/api/factory/setup/progress");

const put = (path: string, body: unknown) => request<unknown>(path, { method: "PUT", body: JSON.stringify(body) });
const slotPath = (slot: SetupSlot) => `/api/factory/integrations/${encodeURIComponent(slot)}`;

/** Connect an integration or change its settings. `asDefault` is the form's `save_as_default`. */
export const saveIntegration = (slot: SetupSlot, name: string, settings: Record<string, SettingValue>, asDefault: boolean) =>
  put(slotPath(slot), { name, settings, default: asDefault });

/** What a connection test answers: the integration's own sentence, never saved. */
export interface TestResult { ok: boolean; message: string }

/** Try the settings in the form against the service. Nothing is saved; a blank secret tests the saved one. */
export const testIntegration = (slot: SetupSlot, name: string, settings: Record<string, SettingValue>) =>
  request<TestResult>(`${slotPath(slot)}/test`, { method: "POST", body: JSON.stringify({ name, settings }) });

export const makeDefault = (slot: SetupSlot, name: string) => put(`${slotPath(slot)}/default`, { name });

/** Keep the step's default: the step reads as set, and nothing about the integrations changes. */
export const keepDefault = (slot: SetupSlot, name: string) =>
  request<unknown>(`/api/factory/setup/${encodeURIComponent(slot)}/keep`, { method: "PUT", body: JSON.stringify({ name }) });

/** Move a project off its own pick of this step, back to the default. */
export const clearProjectPick = (uid: string, slot: SetupSlot) =>
  request<unknown>(`/api/projects/${encodeURIComponent(uid)}/integrations/${encodeURIComponent(slot)}`, { method: "DELETE" });

export const removeConnection = (slot: SetupSlot, name: string) =>
  request<unknown>(`${slotPath(slot)}/connections/${encodeURIComponent(name)}`, { method: "DELETE" });
