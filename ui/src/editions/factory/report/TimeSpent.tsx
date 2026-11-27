import * as React from "react";

import { EmptyState, ListRow, WeekChart } from "../../../components/edition";
import { formatCount } from "../../../lib/number";
import { HOUR, ageWords, durationWords, formatDistance, formatStampUTC } from "../../../lib/time";
import type { ReportData, Stage, StuckList, StuckRow, TimeData } from "../FactoryReport.api";
import { CAPTION, CELL, HEAD, NoValue, TABLE, percent } from "./shared";

// Time (TR-1669): where the time of the four stages went, each split into waiting on a person and agent
// time, how long the merged tickets took from start to merge, and what is stuck now. The figures are the
// chosen range's; "stuck now" is the cell's last pass, whatever the range.

const STAGE: Record<Stage, string> = { doing: "In progress", review: "In review", changes: "Changes asked", blocked: "Blocked" };
/** What a stuck ticket's row says before its age. */
const MOVED: Record<Stage, string> = { doing: "Moved to in progress", review: "Moved to review", changes: "Changes asked", blocked: "Moved to blocked" };
const STAGES = Object.keys(STAGE) as Stage[];
/** In the order of `start_to_merge.buckets`; a time on an edge is in the bucket above it. */
const BUCKETS = ["Under 1 h", "1 to 4 h", "4 to 24 h", "1 to 3 days", "Over 3 days"];
const AGENT_NOTE = "Everything not covered by an answered question, including checks and queues. Unfinished stages are not counted.";

function Percent({ value }: { value: number | null }) {
  return value === null ? <NoValue /> : <>{percent(value)}</>;
}

function Stages({ data }: { data: TimeData }) {
  const id = React.useId();
  return (
    <figure aria-labelledby={id} className="m-0 grid min-w-0 gap-2" data-testid="time-stages">
      <figcaption id={id} className={CAPTION}>Share of time in these four stages</figcaption>
      <div role="region" aria-labelledby={id} tabIndex={0} className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-ring">
        <table aria-labelledby={id} className={TABLE}>
          <thead>
            <tr>
              {["Stage", "Share", "Time", "Waiting on a person", "Agent time"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {data.stages.map((s) => (
              <tr key={s.stage} data-stage={s.stage}>
                <th scope="row" className={CELL}>{STAGE[s.stage]}</th>
                <td className={CELL}><Percent value={s.share} /></td>
                <td className={CELL}>{s.total > 0 ? durationWords(Math.round(s.total * HOUR)) : "none"}</td>
                <td className={CELL}><Percent value={s.waiting_percent} /></td>
                <td className={CELL}><Percent value={s.agent_percent} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="m-0 max-w-[640px] text-xs text-muted-foreground" data-testid="time-agent-note">Agent time: {AGENT_NOTE}</p>
    </figure>
  );
}

/** One histogram for the whole range: a row per bucket with its bar and its count. */
function StartToMerge({ took }: { took: TimeData["start_to_merge"] }) {
  const id = React.useId();
  const most = Math.max(...took.buckets, 1);
  return (
    <figure aria-labelledby={id} className="m-0 grid min-w-0 gap-2" data-testid="time-histogram">
      <figcaption id={id} className={CAPTION}>Start to merge</figcaption>
      <table aria-labelledby={id} className={`${TABLE} max-w-[640px]`}>
        <tbody>
          {BUCKETS.map((label, i) => (
            <tr key={label} data-bucket={label}>
              <th scope="row" className={`${CELL} w-24 whitespace-nowrap`}>{label}</th>
              <td className={`${CELL} align-middle`} aria-hidden="true">
                <svg width="100%" height={10} className="block">
                  <rect width={`${(100 * took.buckets[i]) / most}%`} height={10} className="fill-foreground" />
                </svg>
              </td>
              <td className={`${CELL} w-10 text-right`} data-count>{formatCount(took.buckets[i])}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="m-0 text-xs text-muted-foreground" data-testid="time-histogram-of">
        from {formatCount(took.tickets)} of {formatCount(took.merged)} merged {took.merged === 1 ? "ticket" : "tickets"}
      </p>
    </figure>
  );
}

function Stuck<T extends StuckRow>({ id, title, list, empty, to, children }: {
  id: string; title: string; list: StuckList<T>; empty: string; to: (row: T) => string; children: (row: T) => React.ReactNode;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1" data-testid={`stuck-${id}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <div className={CAPTION}>{title}</div>
        {list.count > 0 && <div className="text-xs text-muted-foreground" data-testid={`stuck-${id}-count`}>{formatCount(list.rows.length)} of {formatCount(list.count)}</div>}
      </div>
      {list.rows.length === 0 ? <EmptyState className="py-1">{empty}</EmptyState> : (
        <div className="min-w-0 [--row-x:0px]">
          {list.rows.map((row) => (
            <ListRow key={`${row.project?.id ?? ""}:${row.label}`} to={to(row)} data-testid={`stuck-${id}-row`}
                     className="flex-col flex-nowrap items-start gap-1 py-3 text-foreground">
              {children(row)}
            </ListRow>
          ))}
        </div>
      )}
    </div>
  );
}

function StuckNow({ stuck }: { stuck: TimeData["stuck"] }) {
  const now = Date.now();
  const age = (at: string) => ageWords(now - Date.parse(at));
  const page = (id: string) => `/projects/${encodeURIComponent(id)}`;
  return (
    <div className="grid min-w-0 gap-3" data-testid="time-stuck">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h3 className="m-0 text-[15px] font-semibold">Stuck now</h3>
        {stuck && (
          <p className="m-0 text-xs text-muted-foreground" data-testid="stuck-as-of" title={formatDistance(Date.parse(stuck.at), now)}>
            As of {formatStampUTC(stuck.at)}
          </p>
        )}
      </div>
      {!stuck ? <EmptyState className="py-1" data-testid="stuck-not-yet">Stuck now is not worked out yet.</EmptyState> : (
        <div className="grid min-w-0 grid-cols-1 gap-5 lg:grid-cols-2 lg:gap-x-8">
          <Stuck id="questions" title="Blocking questions, oldest first" list={stuck.questions} empty="No blocking question is open."
                 to={(q) => `${q.project ? page(q.project.id) : "/crew"}#desk-${q.label}`}>
            {(q) => (
              <>
                <span className="line-clamp-3 text-sm" data-user-text>{q.text}</span>
                <span className="text-xs text-muted-foreground">
                  {q.label} · {q.project ? <span data-user-text>{q.project.name}</span> : "No project"} · Asked {age(q.at)}
                </span>
              </>
            )}
          </Stuck>
          <Stuck id="tickets" title="Tickets longest in one stage" list={stuck.tickets} empty="No ticket is in a stage now."
                 to={(t) => `${page(t.project!.id)}/tickets/${encodeURIComponent(t.label)}`}>
            {(t) => (
              <>
                <span className="text-sm">{t.label} <span data-user-text>{t.title}</span></span>
                <span className="text-xs text-muted-foreground">
                  <span data-user-text>{t.project!.name}</span> · {MOVED[t.stage]} {age(t.at)}
                </span>
              </>
            )}
          </Stuck>
        </div>
      )}
    </div>
  );
}

export function TimeSpent({ data }: { data: ReportData }) {
  const d = data.sections.time as TimeData;
  const { weeks, running_week: running } = data.range;
  const lines = (key: "share" | "waiting") => STAGES.map((s) => ({ label: STAGE[s], values: d.weeks.map((w) => w[key][s]) }));
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 pt-3">
      {d.total > 0 ? (
        <>
          <Stages data={d} />
          <WeekChart caption="Share of stage time per week" kind="lines" weeks={weeks} soFar={running} data-testid="time-share-weeks"
                     series={lines("share")} format={percent} />
          <WeekChart caption="Waiting on a person per week, as a share of each stage" kind="lines" weeks={weeks} soFar={running}
                     data-testid="time-waiting-weeks" series={lines("waiting")} format={percent} />
        </>
      ) : <EmptyState className="py-0" data-testid="time-empty">No stage ended in these weeks.</EmptyState>}
      {d.start_to_merge.merged > 0 && <StartToMerge took={d.start_to_merge} />}
      <StuckNow stuck={d.stuck} />
    </div>
  );
}
