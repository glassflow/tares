// The bodies of the AI SRE Services screens (tares/screens/sre_services.py):
//   GET /api/sre/services              every service as one row (?hidden=1 includes the hidden ones)
//   GET /api/sre/services/{name}       one service's page (?window=1h|24h|7d)
//   PUT /api/sre/services/{name}/settings   any of display_name, hidden, merged_into, repos
// The words (`state_words`, `last_words`, `*_words`) are worded for people and shown as they are.
// `name`, `about`, repo and connection names are the person's own text.

import { request } from "../../api";

export type SreServiceState = "incident" | "not_watched" | "silent" | "no_own_signal" | "unusual" | "normal";
export type SreWindow = "1h" | "24h" | "7d";

/** One line of the Services list. */
export interface SreServiceRow {
  name: string;
  /** The display name when the person renamed the service; null otherwise. */
  about: string | null;
  state: SreServiceState;
  state_words: string;
  signals_words: string;
  feeds_words: string;
  events_24h_words: string;
  watch_words: string;
  /** "checked 4 minutes ago", "not checked yet", "not checked, it is hidden": lower case, shown after "Checked". */
  last_words: string;
  /** Errors in the last day, one count an hour. */
  spark: number[];
  /** null for a service nobody can watch because it has no events of its own. */
  watched: boolean | null;
  hidden: boolean;
}

export interface SreServices {
  services: SreServiceRow[];
  hidden_count: number;
  /** "service-a-receiver is shown as part of service-a", or null when none is. */
  merged_words: string | null;
}

export interface SreChart {
  window: SreWindow;
  /** ISO times. Bucket i starts at since + i * bucket_seconds. */
  since: string;
  until: string;
  bucket_seconds: number;
  errors: number[];
  traffic: number[];
  deploys: { at: string; label: string; repo: string | null }[];
}

export interface SreIncident {
  id: string;
  title: string;
  state: "open" | "on_it" | "resolved";
  state_words: string;
  opened_words: string;
}

export interface SreRaised {
  id: string;
  when_words: string;
  what: string;
  outcome_words: string;
  /** "Worth waking someone", "Worth a look" or "For the record". Text only. */
  worth: string;
}

export interface SreBlindSpot {
  kind: string;
  title: string;
  detail: string;
  action: { label: string; to: string };
}

export interface SreRepo {
  name: string;
  /** "Matched by name" or "Linked in settings". */
  linked_by: string;
  linked_words: string;
}

export interface SreServiceSettings {
  display_name: string;
  hidden: boolean;
  merged_into: string | null;
  merged_names: string[];
  merge_options: string[];
  repo_options: string[];
  updated_by: string | null;
}

/** One service's page. */
export interface SreService {
  name: string;
  about: string | null;
  state: SreServiceState;
  state_words: string;
  hidden: boolean;
  watched: boolean | null;
  watch_words: string;
  signals_words: string;
  feeds_words: string;
  last_words: string;
  events_24h_words: string;
  chart: SreChart;
  /** The newest ten. */
  incidents: SreIncident[];
  incidents_total: number;
  incidents_open: number;
  /** The newest eight. */
  raised: SreRaised[];
  raised_total: number;
  blind_spots_words: string;
  blind_spots: SreBlindSpot[];
  repos: SreRepo[];
  settings: SreServiceSettings;
}

export interface SreServiceSettingsBody {
  display_name?: string | null;
  hidden?: boolean;
  merged_into?: string | null;
  repos?: string[] | null;
}

/** Every service, one read. `hidden` also lists the ones the person hid. */
export const sreServices = (hidden = false) =>
  request<SreServices>(`/api/sre/services${hidden ? "?hidden=1" : ""}`);

/** One service's page. A 404 throws an ApiError: the cell knows no such service. */
export const sreService = (name: string, window: SreWindow = "24h") =>
  request<SreService>(`/api/sre/services/${encodeURIComponent(name)}?window=${window}`);

/** Save any of the settings; answers the service's page (24 hours). A name merged into another
 *  answers with the page of the service it joined. A key that cannot write gets a 403. */
export const saveServiceSettings = (name: string, body: SreServiceSettingsBody) =>
  request<SreService>(`/api/sre/services/${encodeURIComponent(name)}/settings`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
