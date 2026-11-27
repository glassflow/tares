import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { Banner, Button, EmptyState, Input, ListSection, LoadingBlocks, Select } from "../../components/edition";
import { formatCount } from "../../lib/number";
import { DAY, formatDayUTC } from "../../lib/time";
import { usePagePolling } from "../usePagePolling";
import { factoryReport, isRefused, type ReportData, type SectionKey, type TimeData } from "./FactoryReport.api";
import { Cost } from "./report/Cost";
import { Delivered } from "./report/Delivered";
import { Review } from "./report/Review";
import { TimeSpent } from "./report/TimeSpent";

// Report (TR-1667): what the factory delivered, week by week, for a range and a scope. Both live in
// the address (`?range=4w|8w|quarter|dates&from=&to=&scope=`), so a copied address opens the same view.
// One read (GET /api/factory/report) fills the page; a section draws from it and fetches nothing.
// Project and team names are the person's own text and marked data-user-text.

const RANGES = [
  { value: "4w", label: "Last 4 weeks" },
  { value: "8w", label: "Last 8 weeks" },
  { value: "quarter", label: "This quarter" },
  { value: "dates", label: "Two dates" },
];
const DEFAULT_RANGE = "8w";
const DEFAULT_SCOPE = "factory";

/** The sections in reading order. A section's ticket gives its entry a `body`, drawn from the page's one
 *  read, and keeps to its own entry: one per line with a blank line between them. Sections are drawn also
 *  when no ticket was merged (cost or waiting can exist without a merge), so a `body` says for itself
 *  when it has nothing. */
const SECTIONS: { key: SectionKey; title: string; aside?: string; body?: (data: ReportData) => React.ReactNode }[] = [
  { key: "delivered", title: "Delivered", body: (data) => <Delivered data={data} /> },

  { key: "time", title: "Time", body: (data) => <TimeSpent data={data} /> },

  { key: "review", title: "Review", body: (data) => <Review data={data} /> },

  { key: "cost", title: "Cost", aside: "API-equivalent cost", body: (data) => <Cost data={data} /> },

  { key: "merges", title: "Merges" },

  { key: "architecture", title: "Architecture" },
];

/** The first and last date the read is asked for. A preset is worked out from today (UTC), so its
 *  address moves with the weeks; two dates stay as written. The cell takes whole weeks around them. */
function dates(range: string, params: URLSearchParams, now: number): { from?: string; to?: string } {
  const day = (ms: number) => new Date(ms).toISOString().slice(0, 10);
  if (range === "dates") return { from: params.get("from") || undefined, to: params.get("to") || undefined };
  if (range === "4w") return { from: day(now - 3 * 7 * DAY) };
  if (range === "quarter") {
    const d = new Date(now);
    return { from: day(Date.UTC(d.getUTCFullYear(), Math.floor(d.getUTCMonth() / 3) * 3, 1)) };
  }
  return {};
}

/** A date field that tells the page only a whole date or an emptied field: while a year is being typed
 *  the browser reports years such as 0002, and a half-typed date reads as empty with `badInput` set, and
 *  the page keeps what it has through both. */
function DayInput({ value, onDay, ...props }: { label: string; value: string; onDay: (day: string) => void; "data-testid": string }) {
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => setDraft(value), [value]);
  const emptied = (el: HTMLInputElement) => el.value === "" && !el.validity.badInput;
  return (
    <Input {...props} type="date" value={draft}
           onBlur={(e) => { if (value && emptied(e.target)) onDay(""); else setDraft(value); }}
           onChange={(e) => {
             const day = e.target.value;
             setDraft(day);
             if (/^2\d{3}-\d{2}-\d{2}$/.test(day) || (value && emptied(e.target))) onDay(day);
           }} />
  );
}

/** What one read has to show, under the pickers. */
export function ReportView({ data }: { data: ReportData }) {
  const { from, to, merged, running_week: running } = data.range;
  const waiting = (data.sections.time as TimeData | undefined)?.waiting_percent;
  const summary = [
    merged > 0 && `${formatCount(merged)} ${merged === 1 ? "ticket" : "tickets"} merged.`,
    typeof waiting === "number" && `Waiting on a person: ${formatCount(waiting)}% of stage time.`,
    `${formatDayUTC(from)} to ${formatDayUTC(to)}, in weeks that start on Monday (UTC).`,
    running && "This week is not over yet.",
  ].filter(Boolean).join(" ");
  return (
    <div className="grid grid-cols-1 gap-5 md:gap-6" data-testid="report-read">
      <div className="grid gap-1.5">
        <p className="m-0 text-[15px]" data-testid="report-summary">{summary}</p>
        <p className="m-0 text-[13px] text-muted-foreground" data-testid="report-cannot-see">
          <b className="text-foreground">What this report cannot see:</b> {data.cannot_see.join(" ")}
        </p>
      </div>
      {data.scope.value === "mine" && data.scope.count === 0 ? (
        <EmptyState title="No project here is yours" data-testid="report-none-mine">
          Mine means the projects you lead or hold a ticket in. Pick the whole factory to see everything.
        </EmptyState>
      ) : (
        <>
          {merged === 0 && (
            <EmptyState title="No ticket was merged in these weeks" data-testid="report-empty">Pick a longer range or another scope.</EmptyState>
          )}
          {SECTIONS.map((s) => (
            <ListSection paper key={s.key} title={s.title} aside={s.aside} data-testid={`report-section-${s.key}`}>
              {s.body ? s.body(data) : <EmptyState>This section is not built yet.</EmptyState>}
            </ListSection>
          ))}
        </>
      )}
    </div>
  );
}

/** One read. `key` on the query starts a fresh one when the address changes. */
function ReportRead({ query, onRead, onStartOver }: { query: string; onRead: (data: ReportData) => void; onStartOver: () => void }) {
  const { data, error, reload } = usePagePolling(() => factoryReport(query), 0);
  const read = data && !isRefused(data) ? data : undefined;
  React.useEffect(() => { if (read) onRead(read); }, [read, onRead]);

  if (data && isRefused(data)) {
    return (
      <Banner tone="error" data-testid="report-refused" action={<Button variant="secondary" onClick={onStartOver}>Start over</Button>}>
        <b>The report did not load.</b>{" "}
        <span className="text-foreground">{data.refused === 404 ? "That project or team does not exist, or you cannot see it." : data.message}</span>
      </Banner>
    );
  }
  if (error) {
    return (
      <Banner tone="error" data-testid="report-error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
        <b>The report did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
      </Banner>
    );
  }
  return read ? <ReportView data={read} /> : <LoadingBlocks data-testid="report-loading" widths={["55%", "35%", "70%"]} />;
}

export default function FactoryReport() {
  const [params, setParams] = useSearchParams();
  const asked = params.get("range");
  const range = RANGES.some((r) => r.value === asked) ? (asked as string) : DEFAULT_RANGE;
  const scope = params.get("scope") || DEFAULT_SCOPE;
  // The last read that answered: its teams and projects fill the scope list, also while the next one
  // is on its way or after the cell refused it.
  const [last, setLast] = React.useState<ReportData>();

  const { from, to } = dates(range, params, Date.now());
  const query = new URLSearchParams({ scope, ...(from ? { from } : {}), ...(to ? { to } : {}) }).toString();

  const patch = (next: Record<string, string | undefined>) => {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) v ? p.set(k, v) : p.delete(k);
    setParams(p);
  };
  const pickRange = (value: string) => {
    if (value !== "dates") return patch({ range: value, from: undefined, to: undefined });
    // two dates start from the weeks in view, and today for a week that is not over
    const today = new Date().toISOString().slice(0, 10);
    const shown = last?.range;
    patch({ range: value, from: shown?.from, to: shown && (shown.to < today ? shown.to : today) });
  };

  const scopes = [
    { value: "mine", label: "My projects" },
    { value: "factory", label: "The whole factory" },
    ...(last?.scope.teams ?? []).map((t) => ({ value: `team:${t.id}`, label: `Team led by ${t.name}` })),
    ...(last?.scope.projects ?? []).map((p) => ({ value: `project:${p.id}`, label: `Project: ${p.name}` })),
  ];

  return (
    <div className="grid grid-cols-1 gap-5 md:gap-6" data-testid="factory-report">
      <div>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Report</h1>
        <p className="m-0 max-w-[640px] text-[15px] text-muted-foreground">What the factory delivered, week by week.</p>
      </div>
      <div className="flex flex-wrap items-end gap-3" data-testid="report-pickers">
        <div className="w-full sm:w-44">
          <Select label="Range" options={RANGES} value={range} onValueChange={pickRange} data-testid="report-range" />
        </div>
        <div className="w-full min-w-0 sm:w-72">
          <Select label="Scope" options={scopes} value={scopes.some((s) => s.value === scope) ? scope : ""} placeholder="Pick a scope"
                  onValueChange={(v) => patch({ scope: v })} data-testid="report-scope" data-user-text />
        </div>
        {range === "dates" && (
          <>
            <div className="w-full sm:w-44">
              <DayInput label="From" value={params.get("from") ?? ""} onDay={(v) => patch({ from: v })} data-testid="report-from" />
            </div>
            <div className="w-full sm:w-44">
              <DayInput label="To" value={params.get("to") ?? ""} onDay={(v) => patch({ to: v })} data-testid="report-to" />
            </div>
          </>
        )}
      </div>
      <ReportRead key={query} query={query} onRead={setLast} onStartOver={() => setParams({})} />
    </div>
  );
}
