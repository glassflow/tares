import * as React from "react";

import { Button, EmptyState, ListRow, Stat, WeekChart } from "../../../components/edition";
import { formatCount, formatDecimal } from "../../../lib/number";
import { formatDistance, formatStampUTC } from "../../../lib/time";
import type { FindingRow, RecurringRow, ReportData, ReviewData, ReviewFigures } from "../FactoryReport.api";
import { CAPTION, NoValue, passPercent, percent, roundsWords, tickets } from "./shared";

// Review (TR-1670): the pass rate and review rounds of the range, week by week and by project, its
// findings by kind and severity, and the findings that keep coming back. The figures are the chosen
// range's; the recurring findings are the last 30 days of the cell's last pass, whatever the range.

/** The recurring list covers this many days; the cell's answer says it once it has made a pass. */
const DAYS = 30;

const kindName = (kind: string | null) => {
  const name = kind || "other";
  return name.charAt(0).toUpperCase() + name.slice(1);
};
const rounds = (value: number) => formatDecimal(value, 1);

type Column = "kind" | "severity" | "fixed" | "open";
/** `down`: the order the first press on the column gives. */
const COLUMNS: { key: Column; title: string; down: boolean }[] = [
  { key: "kind", title: "Kind", down: false },
  { key: "severity", title: "Severity", down: false },
  { key: "fixed", title: "Fixed", down: true },
  { key: "open", title: "Still open", down: true },
];

function compare(a: FindingRow, b: FindingRow, key: Column) {
  if (key === "kind") return kindName(a.kind).localeCompare(kindName(b.kind));
  if (key === "severity") return a.severity.localeCompare(b.severity);
  return a[key] - b[key];
}

/** How the header row and every findings row lay out the four columns. */
const COLUMN_ROW = "grid min-h-0 grid-cols-[minmax(0,1.6fr)_minmax(0,1.2fr)_minmax(0,0.8fr)_minmax(0,1fr)] items-baseline gap-x-3 py-2";

/** A column's header sorts by it, and again the other way round. Rows that compare equal stay in the
 *  cell's order: most open first, then severity, then kind. A number is said with its column's name
 *  to a screen reader, which hears no columns. */
function Findings({ rows }: { rows: FindingRow[] }) {
  const [sort, setSort] = React.useState<{ key: Column; down: boolean }>({ key: "open", down: true });
  if (rows.length === 0) {
    return <EmptyState className="py-0" data-testid="review-no-findings">No review finding was first raised in these weeks.</EmptyState>;
  }
  const sorted = rows.map((row, at) => ({ row, at })).sort((a, b) => {
    const by = compare(a.row, b.row, sort.key);
    return (sort.down ? -by : by) || a.at - b.at;
  });
  return (
    <div className="grid min-w-0 content-start gap-1" data-testid="review-findings">
      <div className={CAPTION}>Findings first raised in these weeks</div>
      <div className="min-w-0 max-w-[640px] [--row-x:0px]">
        <ListRow className={`${COLUMN_ROW} hover:bg-transparent`} data-findings-head>
          {COLUMNS.map((c) => {
            const on = sort.key === c.key;
            const direction = sort.down ? "descending" : "ascending";
            return (
              <Button key={c.key} variant="quiet" data-sort={c.key} data-sorted={on ? direction : undefined}
                      onClick={() => setSort({ key: c.key, down: on ? !sort.down : c.down })}
                      className="h-auto min-w-0 justify-start gap-1 whitespace-normal border-0 p-0 text-left font-mono text-[11px] font-normal uppercase tracking-[0.08em]">
                {c.title}
                <span aria-hidden="true" className="w-2">{on ? (sort.down ? "↓" : "↑") : ""}</span>
                {on && <span className="sr-only">, sorted {direction}</span>}
              </Button>
            );
          })}
        </ListRow>
        {sorted.map(({ row }) => (
          <ListRow key={`${row.kind}:${row.severity}`} className={`${COLUMN_ROW} text-sm`} data-finding={`${row.kind}:${row.severity}`}>
            <span>{kindName(row.kind)}</span>
            <span>{row.severity || <NoValue />}</span>
            <span className="tabular-nums"><span className="sr-only">Fixed </span>{formatCount(row.fixed)}</span>
            <span className="tabular-nums"><span className="sr-only">Still open </span>{formatCount(row.open)}</span>
          </ListRow>
        ))}
      </div>
    </div>
  );
}

function figures({ first_pass: first, rounds_per_ticket: each }: ReviewFigures) {
  return [
    `${passPercent(first)}% passed first review`,
    each !== null && roundsWords(each),
    `${formatCount(first.reviewed)} ${tickets(first.reviewed)} first reviewed`,
  ].filter(Boolean).join(" · ");
}

function Projects({ projects, weeks }: { projects: ReviewData["projects"]; weeks: number }) {
  const before = weeks === 1 ? "The week before" : `The ${formatCount(weeks)} weeks before`;
  return (
    <div className="grid min-w-0 content-start gap-1" data-testid="review-projects">
      <div className={CAPTION}>Projects, lowest pass rate first</div>
      <div className="min-w-0 [--row-x:0px]">
        {projects.map((p) => (
          <ListRow key={p.project} data-testid="review-project-row" className="flex-col flex-nowrap items-start gap-1 py-3">
            <span className="text-sm" data-user-text>{p.name}</span>
            <span className="text-xs">{figures(p)}</span>
            <span className="text-xs text-muted-foreground" data-before>
              {before}: {p.previous ? figures(p.previous) : "no ticket first reviewed"}
            </span>
          </ListRow>
        ))}
      </div>
    </div>
  );
}

/** A row is marked when a rule the reader can open was accepted for its repo and kind. */
function RecurringRows({ rows }: { rows: RecurringRow[] }) {
  return (
    <div className="min-w-0 [--row-x:0px]">
      {rows.map((row) => (
        <ListRow key={`${row.repo}:${row.kind}`} data-testid="recurring-row" className="flex-col flex-nowrap items-start gap-1 py-3">
          <span className="text-sm" data-user-text>{kindName(row.kind)}</span>
          <span className="text-xs text-muted-foreground">
            <span data-user-text>{row.repo}</span> · {formatCount(row.count)} {tickets(row.count)}
            {row.rule_accepted && <> · <span className="text-foreground" data-rule>Rule accepted</span></>}
          </span>
        </ListRow>
      ))}
    </div>
  );
}

function Recurring({ recurring }: { recurring: ReviewData["recurring"] }) {
  const now = Date.now();
  const days = formatCount(recurring?.days ?? DAYS);
  return (
    <div className="grid min-w-0 content-start gap-1" data-testid="review-recurring">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h3 className="m-0 text-[15px] font-semibold">Recurring findings, last {days} days</h3>
        {recurring && (
          <p className="m-0 text-xs text-muted-foreground" data-testid="recurring-as-of" title={formatDistance(Date.parse(recurring.at), now)}>
            As of {formatStampUTC(recurring.at)}
          </p>
        )}
      </div>
      {!recurring ? <EmptyState className="py-1" data-testid="recurring-not-yet">Recurring findings are not worked out yet.</EmptyState>
        : recurring.rows.length === 0 ? <EmptyState className="py-1" data-testid="recurring-none">No finding kind came back on 3 or more tickets in the last {days} days.</EmptyState>
        : <RecurringRows rows={recurring.rows} />}
    </div>
  );
}

export function Review({ data }: { data: ReportData }) {
  const d = data.sections.review as ReviewData;
  const { weeks, running_week: running } = data.range;
  const { first_pass: first, rounds_per_ticket: each } = d;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 pt-3">
      {first.reviewed > 0 ? (
        <>
          <div className="grid grid-cols-2 gap-x-6 gap-y-5 md:grid-cols-4">
            <div className="grid min-w-0 content-start gap-1.5 [overflow-wrap:anywhere]" data-testid="review-first-pass">
              <Stat label="Passed first review" value={percent(passPercent(first))} />
              <p className="m-0 text-xs text-muted-foreground">of {formatCount(first.reviewed)} {tickets(first.reviewed)} first reviewed</p>
            </div>
            <div className="grid min-w-0 content-start gap-1.5 [overflow-wrap:anywhere]" data-testid="review-rounds">
              <Stat label="Review rounds per ticket" value={each === null ? "no value" : rounds(each)} />
            </div>
          </div>
          <WeekChart caption="Passed first review per week" kind="lines" weeks={weeks} soFar={running} data-testid="review-pass-weeks"
                     series={[{ label: "Passed", values: d.weeks.map((w) => (w.first_pass.reviewed ? passPercent(w.first_pass) : null)) }]} format={percent} />
          <WeekChart caption="Review rounds per ticket per week" kind="lines" weeks={weeks} soFar={running} data-testid="review-rounds-weeks"
                     series={[{ label: "Rounds", values: d.weeks.map((w) => w.rounds_per_ticket) }]} format={rounds} />
        </>
      ) : <EmptyState className="py-0" data-testid="review-empty">No ticket had its first review in these weeks.</EmptyState>}
      <Findings rows={d.findings} />
      {d.projects.length > 0 && <Projects projects={d.projects} weeks={weeks.length} />}
      <Recurring recurring={d.recurring} />
    </div>
  );
}
