import { EmptyState, Stat, WeekChart } from "../../../components/edition";
import { formatCount, formatDecimal } from "../../../lib/number";
import { HOUR, MIN, durationWords } from "../../../lib/time";
import type { DeliveredData, ReportData } from "../FactoryReport.api";

// Delivered (TR-1668): four headline figures for the range, each with its change on as many weeks just
// before as a plain number, and merged tickets and start-to-merge week by week.

const tickets = (n: number) => (n === 1 ? "ticket" : "tickets");

/** "+12 on the 4 weeks before". `say` words the size of the difference. */
function change(now: number, then: number | null | undefined, weeks: number, say: (size: number) => string = formatCount) {
  const before = weeks === 1 ? "the week before" : `the ${formatCount(weeks)} weeks before`;
  if (then === null || then === undefined) return `No figures for ${before}`;
  if (now === then) return `Same as ${before}`;
  return `${now > then ? "+" : "-"}${say(Math.abs(now - then))} on ${before}`;
}

/** Hours as milliseconds, rounded: 19.4 hours must not read a minute short. */
const ms = (hours: number) => Math.round(hours * HOUR);
/** Hours as the console's duration words. */
const length = (hours: number) => durationWords(ms(hours));
/** The same words in a narrow column: a second unit takes its own line. */
const column = (hours: number) => length(hours).replace(/ (?=\d)/, "\n");
/** A length in whole minutes, so two lengths that read the same are the same. */
const minutes = (hours: number) => Math.floor(ms(hours) / MIN);

function Tile({ id, label, value, under = [], changed, pair }: {
  id: string; label: string; value: string; under?: (string | false)[]; changed?: string; pair?: string | false;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1.5 [overflow-wrap:anywhere]" data-testid={`delivered-${id}`}>
      <Stat label={label} value={value} />
      {under.map((line) => line && <p key={line} className="m-0 text-xs text-muted-foreground">{line}</p>)}
      {changed && <p className="m-0 text-xs text-muted-foreground" data-change>{changed}</p>}
      {pair && <p className="m-0 text-[13px]" data-pair>{pair}</p>}
    </div>
  );
}

export function Delivered({ data }: { data: ReportData }) {
  const d = data.sections.delivered as DeliveredData;
  const { weeks, running_week: running } = data.range;
  const { previous, first_pass: first, rounds_per_ticket: rounds, start_to_merge: took } = d;
  if (!d.merged && !d.milestones_accepted && !first.reviewed) {
    return <EmptyState data-testid="delivered-empty">Nothing was delivered in these weeks.</EmptyState>;
  }
  const count = (key: "merged" | "milestones_accepted" | "projects_merged") => change(d[key], previous?.[key], weeks.length);
  const earlier = previous?.start_to_merge.p50;
  // with earlier figures and none now there is no difference to say
  const startChange = typeof earlier !== "number" ? change(0, null, weeks.length)
    : took.p50 === null ? undefined
    : change(minutes(took.p50), minutes(earlier), weeks.length, (m) => durationWords(m * MIN));
  const round = rounds === null ? "" : formatDecimal(rounds, 1);
  return (
    <div className="grid min-w-0 grid-cols-1 gap-5 pt-3">
      <div className="grid grid-cols-2 gap-x-6 gap-y-5 md:grid-cols-4">
        <Tile id="merged" label="Merged" value={formatCount(d.merged)} changed={count("merged")}
              pair={first.reviewed > 0 && `${Math.round((100 * first.passed) / first.reviewed)}% passed first review, of ${formatCount(first.reviewed)} ${tickets(first.reviewed)} first reviewed`} />
        <Tile id="milestones" label="Milestones accepted" value={formatCount(d.milestones_accepted)} changed={count("milestones_accepted")} />
        <Tile id="projects" label="Projects with a merge" value={formatCount(d.projects_merged)} changed={count("projects_merged")} />
        <Tile id="start-to-merge" label="Start to merge, p50" value={took.p50 === null ? "no value" : length(took.p50)}
              under={[
                took.p90 !== null && `p90 ${length(took.p90)}`,
                took.tickets < d.merged && `from ${formatCount(took.tickets)} of ${formatCount(d.merged)} merged ${tickets(d.merged)}`,
              ]}
              changed={startChange}
              pair={round !== "" && `${round} review ${round === "1" ? "round" : "rounds"} per ticket reviewed`} />
      </div>
      <WeekChart caption="Merged per week" weeks={weeks} soFar={running} data-testid="delivered-merged-weeks"
                 series={[{ label: "Merged", values: d.weeks.map((w) => w.merged) }]} format={formatCount} />
      <WeekChart caption="Start to merge, p50 per week" kind="lines" weeks={weeks} soFar={running} data-testid="delivered-p50-weeks"
                 series={[{ label: "p50", values: d.weeks.map((w) => w.p50) }]} format={column} />
    </div>
  );
}
