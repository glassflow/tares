// The body of GET /api/factory/report on a factory cell (tares/report.py `page`). One read answers the
// whole page: every section's figures come under `sections`, and a section fetches nothing of its own.
// Project and team names are a person's own text, and the page shows every string as text.

import { ApiError, request } from "../../api";

export type SectionKey = "delivered" | "time" | "review" | "cost" | "merges" | "architecture";

/** What was delivered in some weeks. Durations are in hours, null with no ticket to say them from. */
export interface DeliveredFigures {
  merged: number;
  milestones_accepted: number;
  projects_merged: number;
  /** `tickets`: how many of `merged` have a start, which p50 and p90 are said from. */
  start_to_merge: { p50: number | null; p90: number | null; tickets: number };
  first_pass: { passed: number; reviewed: number };
  rounds_per_ticket: number | null;
}

/** `sections.delivered` (tares/report.py `delivered`). `previous` holds the figures of as many weeks
 *  just before the range, null when nothing is there. */
export interface DeliveredData extends DeliveredFigures {
  previous: DeliveredFigures | null;
  weeks: { week: string; merged: number; p50: number | null }[];
}

export interface ReportData {
  /** The weeks the read covers: `from` is a Monday, `to` a Sunday, `weeks` the Mondays, oldest first.
   *  `running_week` is the Monday of the week that is not over, when the range holds it. */
  range: { from: string; to: string; weeks: string[]; running_week: string | null; merged: number };
  /** `value` is the scope asked for, `count` its projects; `teams` and `projects` are what the caller
   *  may pick. */
  scope: { value: string; name: string | null; count: number; teams: { id: string; name: string }[]; projects: { id: string; name: string }[] };
  cannot_see: string[];
  /** A section is here once the cell builds its figures. */
  sections: Partial<Record<SectionKey, unknown>>;
}

/** The cell would not read what the address asks for: 400 a bad date or scope, 404 a project or team
 *  the caller cannot see. `message` is the cell's own sentence. */
export type ReportRefused = { refused: 400 | 404; message: string };

export const isRefused = (r: ReportData | ReportRefused): r is ReportRefused => "refused" in r;

/** The whole page in one read. A refusal is an answer, so the page can say what is wrong with the
 *  address; any other failure is thrown. */
export const factoryReport = (query: string): Promise<ReportData | ReportRefused> =>
  request<ReportData>(`/api/factory/report?${query}`).catch((e) => {
    if (e instanceof ApiError && (e.status === 400 || e.status === 404)) return { refused: e.status, message: e.message };
    throw e;
  });
