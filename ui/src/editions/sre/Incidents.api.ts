import { request } from "../../api";

// The incidents screens (tares/screens/sre_incidents.py): the list is one read, an incident is one
// read, and a change (I'm on it, resolve, reopen, worth) answers the incident as it is now.

export type IncidentState = "open" | "on_it" | "resolved";
export type GroupState = IncidentState | "raised";

export interface IncidentItem {
  id: string;
  /** False for a finding that was raised but did not become an incident: it has no page. */
  incident: boolean;
  title: string;
  service: string;
  state: GroupState;
  state_words: string;
  /** ISO time. */
  opened_at: string;
  when_words: string;
  /** The person it is with: the one on it, or the one who resolved it. */
  who: string | null;
}

export interface IncidentGroup {
  state: GroupState;
  label: string;
  items: IncidentItem[];
}

export interface IncidentsData {
  groups: IncidentGroup[];
  counts: { open: number; on_it: number; resolved: number };
  services: string[];
  /** The `before` of the next page of resolved incidents, or null when there is none. */
  next_before: string | null;
  quiet_checks_words: string | null;
}

export type IncidentAction = "on_it" | "resolve" | "reopen";
export type WorthAction = "worth_yes" | "worth_no";

export interface IncidentData {
  id: string;
  title: string;
  service: string;
  state: IncidentState;
  state_words: string;
  opened_at: string;
  who: { name: string; since: string } | null;
  resolved: { by: string | null; at: string; words: string } | null;
  failing: string | null;
  likely_cause: string | null;
  next_step: string | null;
  note: string;
  evidence: { at: string; text: string }[];
  chart: {
    label: string;
    points: [string, number][];
    /** Every event of the service over the same window; older cells do not send it. */
    traffic?: [string, number][];
    deploys: { at: string; label: string }[];
  };
  slack: { where: string; url: string | null } | null;
  joined: { at: string; title: string }[];
  worth: { answer: boolean | null; by: string | null; can_answer: boolean };
  actions: IncidentAction[];
}

export interface IncidentsQuery {
  service?: string;
  before?: string;
  show?: "all";
}

export function sreIncidents(q: IncidentsQuery = {}): Promise<IncidentsData> {
  const p = new URLSearchParams();
  if (q.service) p.set("service", q.service);
  if (q.before) p.set("before", q.before);
  if (q.show) p.set("show", q.show);
  const s = p.toString();
  return request<IncidentsData>(`/api/sre/incidents${s ? `?${s}` : ""}`);
}

export const sreIncident = (id: string) =>
  request<IncidentData>(`/api/sre/incidents/${encodeURIComponent(id)}`);

export const incidentAction = (id: string, action: IncidentAction | WorthAction) =>
  request<IncidentData>(`/api/sre/incidents/${encodeURIComponent(id)}/${action}`, { method: "POST" });
