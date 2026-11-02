// The AI SRE Settings page: GET /api/sre/settings and PUT /api/sre/settings/{group}
// (tares/screens/sre_settings.py). The cell words the values and the sentences; the page keeps only
// the labels of its own fields. `value` is what is stored, `value_words` the same in a sentence.

import { request } from "../../api";

export interface Option {
  value: string;
  label: string;
}

export interface Link {
  label: string;
  value: string;
}

export type FieldValue = string | number | string[] | null;

export interface Field {
  key: string;
  label: string;
  value: FieldValue;
  value_words: string | null;
  /** The choices, or null when the value is typed. */
  options: Option[] | null;
  help: string;
  /** model: the cell's usual model */
  default?: string;
  /** budget_usd: "Spent $6.40 of $20 this month" */
  spent_words?: string;
  /** budget_period: when a monthly budget starts again */
  resets_on?: string | null;
  resets_words?: string | null;
  /** rule_text and hit_rate cannot be changed here */
  read_only?: boolean;
  /** rule_text: the wording of the rule, written by a person */
  text?: string;
  link?: Link;
}

export interface FieldGroup {
  title: string;
  editable: boolean;
  fields: Field[];
  /** Said when there is no watcher or investigator yet. */
  empty_words: string | null;
  agent?: string;
}

export interface NotesGroup extends FieldGroup {
  slack: { connected: boolean; problem: string | null; words: string; link: Link };
}

export interface ToolRow {
  name: string;
  kind: string;
  ok: boolean;
  state: string;
  state_words: string | null;
  problem_words: string | null;
  link: Link;
}

export interface ToolsGroup {
  title: string;
  editable: boolean;
  rows: ToolRow[];
  add: Link[];
  empty_words: string | null;
}

export interface KeyRow {
  name: string;
  scopes_words: string;
  last_used_words: string;
  /** Only an admin sees these two. */
  id?: string;
  can_revoke?: boolean;
}

export interface PeopleGroup {
  title: string;
  editable: boolean;
  keys: KeyRow[];
  scope_options: Option[];
  empty_words: string | null;
  workspace: { words: string; link: Link } | null;
}

export interface EditionGroup {
  title: string;
  editable: boolean;
  value: string;
  label: string;
  can_switch: boolean;
  options: Option[];
  warning: string;
}

export interface AdvancedGroup {
  title: string;
  editable: boolean;
  links: (Link & { help: string })[];
  note: string;
}

export interface SreSettings {
  watching: FieldGroup;
  investigating: FieldGroup;
  notes: NotesGroup;
  tools: ToolsGroup;
  people: PeopleGroup;
  edition: EditionGroup;
  advanced: AdvancedGroup;
  can_edit: boolean;
}

/** The groups a person saves, each on its own. */
export type FieldGroupName = "watching" | "investigating" | "notes";
export type GroupName = FieldGroupName | "people" | "edition";

/** The key a person just made. The secret is shown once and the cell never says it again. */
export interface CreatedKey {
  id: string;
  name: string;
  scopes_words: string;
  secret: string;
}

export type PeopleSaved = SreSettings & { created_key?: CreatedKey };

export const sreSettings = () => request<SreSettings>("/api/sre/settings");

/** Saves one group. A field group takes {key: value} for the parts that changed and answers with the
 *  new page; people takes {create} or {revoke}; edition takes {value, confirm}. A refusal throws an
 *  ApiError whose message is the cell's sentence (for a field group: which part was saved and which was
 *  not). */
export function saveSreSettings(group: "people", body: { create: { name: string; scopes: string[] } } | { revoke: string }): Promise<PeopleSaved>;
export function saveSreSettings(group: "edition", body: { value: string; confirm: boolean }): Promise<unknown>;
export function saveSreSettings(group: FieldGroupName, body: Record<string, FieldValue>): Promise<SreSettings>;
export function saveSreSettings(group: GroupName, body: unknown): Promise<unknown> {
  return request(`/api/sre/settings/${group}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
