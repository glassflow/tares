// The bodies of GET /api/sre/timeline and GET /api/sre/timeline/raw on an AI SRE cell
// (tares/screens/sre_timeline.py). Every clock the cell words ("15:10", "14:50 to 15:00") is UTC.
// `text` of an item and of a raw line, and `service`, are the cell's own words made from names
// people chose (a service, an alert, a repo); the page shows them as they are.

import { request } from "../../api";

export type TimelineKind = "window" | "quiet" | "deploy" | "alert" | "check" | "incident" | "metric";
export type TimelineTone = "bad" | "warn" | "ok" | "info";

/** The events behind a window: the range to ask the raw route for, and how many there are. */
export interface TimelineRaw {
  from: string;
  to: string;
  count: number;
}

export interface TimelineItem {
  id: string;
  kind: TimelineKind;
  at: string;
  /** The end of a span ("15:00 to 15:10"); null for a moment. */
  end: string | null;
  time_words: string;
  /** The service the item is about, as the page shows it; "" for an alert with no service. */
  service: string;
  text: string;
  tone: TimelineTone;
  /** An incident item points at its incident ("/incidents/<id>"). */
  link: string | null;
  /** A window or a folded quiet stretch has raw lines behind it; the rest have none. */
  raw: TimelineRaw | null;
}

export interface TimelineData {
  /** "Last 6 hours", or "28 Oct 09:00 to 28 Oct 15:00" for a range that does not end now. */
  range_words: string;
  /** Every service the cell knows, for the picker. */
  services: string[];
  items: TimelineItem[];
  /** Pass it as `before` for the next, older page; null when there is none. */
  next_before: string | null;
  /** A sentence for a range with nothing in it; null when there are items. */
  empty_words: string | null;
}

export interface TimelineRawRow {
  at: string;
  feed: string;
  text: string;
  fields: Record<string, unknown>;
}

export interface TimelineRawData {
  rows: TimelineRawRow[];
  /** All the lines in the range, not just the ones returned. */
  count: number;
  more: boolean;
}

/** The ranges a person can pick. A custom range carries its own `from` and `to` in the address. */
export type RangeKey = "1h" | "6h" | "24h" | "7d" | "custom";

/** The kinds a person can turn on and off. */
export type KindGroup = "logs" | "metrics" | "alerts" | "deploys" | "checks";
export const KIND_GROUPS: KindGroup[] = ["logs", "metrics", "alerts", "deploys", "checks"];

export interface TimelineQuery {
  service?: string;
  /** An ISO time; the range ends now unless `to` is given. */
  from?: string;
  to?: string;
  /** A comma list of KindGroup; empty means all of them. */
  kinds?: string;
  before?: string;
}

const qs = (q: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

/** One page of the timeline, newest first. */
export const sreTimeline = (q: TimelineQuery = {}) => request<TimelineData>(`/api/sre/timeline${qs({ ...q })}`);

/** The events in a range, newest first. Only called when a person opens the lines of an item. */
export const sreTimelineRaw = (q: { service?: string; from: string; to: string; limit?: number }) =>
  request<TimelineRawData>(`/api/sre/timeline/raw${qs({ ...q })}`);
