import * as React from "react";

import { EmptyState } from "@/components/edition/EmptyState";
import { formatDecimal } from "@/lib/number";
import { formatDayShortUTC, formatDayUTC } from "@/lib/time";
import { cn } from "@/lib/utils";

export interface WeekSeries {
  label: string;
  /** One value per week, in the order of `weeks`. null (or a missing one) is a week with no value, not 0. */
  values: (number | null)[];
}

export interface WeekChartProps extends Omit<React.HTMLAttributes<HTMLElement>, "children"> {
  /** What the figure shows: its caption, in view above it. */
  caption: string;
  /** The Mondays of the weeks, oldest first, as year-month-day. */
  weeks: string[];
  /** The first three are drawn; every one is listed with its values. */
  series: WeekSeries[];
  /** "bars" (default) for a count or a sum; "lines" for a figure that moves, such as a median. */
  kind?: "bars" | "lines";
  /** A value with its unit, short enough for a column. Default: grouped, at most one decimal. */
  format?: (value: number) => string;
  /** The Monday of the week still running: its column says "so far". */
  soFar?: string | null;
  /** Pixels tall for the plot. Default 120. */
  height?: number;
}

// Ink first, then the two quieter inks; a line also differs by its dashes.
const DRAWN = [
  { fill: "fill-foreground", stroke: "stroke-foreground", dash: undefined },
  { fill: "fill-muted-foreground", stroke: "stroke-muted-foreground", dash: "5 3" },
  { fill: "fill-faint", stroke: "stroke-faint", dash: "1.5 3" },
];
const COL = 10;       // a week's width in the plot's own units
const GROUP = 6;      // how much of it the bars take
const LABEL = "sticky left-0 z-10 bg-background";
// The platform pages style every table, th and td by element; each of those is set here.
const HEAD = "border-0 p-0 pb-1 align-bottom font-mono text-[11px] font-normal uppercase tracking-[0.08em] text-muted-foreground";

function Swatch({ index, kind }: { index: number; kind: "bars" | "lines" }) {
  const look = DRAWN[index];
  return (
    <svg aria-hidden="true" width={16} height={8} className="shrink-0">
      {look && (kind === "bars"
        ? <rect x={4} width={8} height={8} className={look.fill} />
        : <line x1={0} x2={16} y1={4} y2={4} strokeWidth={2} strokeDasharray={look.dash} className={look.stroke} />)}
    </svg>
  );
}

/** Weekly figures as one figure: a bar or line plot over a table with a column per week, so every value
 *  is read without hovering. y starts at 0. It scrolls sideways when the weeks do not fit, with the
 *  series names held in place. It sits on the page canvas. */
export function WeekChart({ caption, weeks, series, kind = "bars", format = (v) => formatDecimal(v, 1), soFar, height = 120, className, ...props }: WeekChartProps) {
  const id = React.useId();
  // Values are not trusted: anything but a finite number is a week with no value.
  const rows = series.map((s) => ({ label: s.label, values: weeks.map((_, w) => (Number.isFinite(s.values[w]) ? (s.values[w] as number) : null)) }));
  const drawn = rows.slice(0, DRAWN.length);
  let max = 0;
  for (const s of drawn) for (const v of s.values) if (v !== null && v > max) max = v;
  const top = max || 1;
  const tall = (v: number) => (Math.max(v, 0) / top) * (height - 4);
  const middle = (w: number) => w * COL + COL / 2;
  const wide = GROUP / Math.max(drawn.length, 1);

  return (
    <figure aria-labelledby={id} className={cn("m-0 grid min-w-0 gap-2", className)} data-week-chart={kind} {...props}>
      <figcaption id={id} className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{caption}</figcaption>
      {!rows.some((s) => s.values.some((v) => v !== null)) ? (
        <EmptyState>Nothing to chart: no data in these weeks.</EmptyState>
      ) : (
        <div role="region" aria-labelledby={id} tabIndex={0} className="relative min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-ring">
          {/* overflow-visible: a table that clips its own content stops the names from staying in place */}
          <table aria-labelledby={id} className="w-full table-fixed border-collapse overflow-visible border-0 bg-transparent text-[13px]">
            <thead>
              <tr>
                <th scope="col" className={cn(HEAD, LABEL, "w-28 pr-2 text-left")}>Week of</th>
                {weeks.map((week) => (
                  <th key={week} scope="col" title={formatDayUTC(week)} data-week={week} className={cn(HEAD, "w-[4.5rem] bg-transparent text-center")}>
                    {formatDayShortUTC(week)}
                    {week === soFar && <span className="block">so far</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr aria-hidden="true">
                <td className={cn(LABEL, "border-0 p-0")} />
                <td colSpan={weeks.length} className="border-0 border-b border-rule-strong p-0">
                  <svg viewBox={`0 0 ${weeks.length * COL} ${height}`} preserveAspectRatio="none" width="100%" height={height} className="block" data-plot>
                    {kind === "bars" && drawn.map((s, i) => s.values.map((v, w) => v !== null && v > 0 && (
                      <rect
                        key={`${s.label}-${weeks[w]}`}
                        x={w * COL + (COL - GROUP) / 2 + i * wide}
                        y={height - Math.max(tall(v), 1)}
                        width={wide}
                        height={Math.max(tall(v), 1)}
                        className={cn(DRAWN[i].fill, weeks[w] === soFar && "opacity-50")}
                        data-bar={s.label}
                      />
                    )))}
                    {kind === "lines" && drawn.map((s, i) => (
                      <g key={s.label} className={DRAWN[i].stroke} data-line={s.label}>
                        {/* One path, lifted at a week with no value, so a gap shows as a gap. */}
                        <path
                          d={s.values.map((v, w) => (v === null ? "" : `${w && s.values[w - 1] !== null ? "L" : "M"}${middle(w)} ${height - 2 - tall(v)}`)).join("")}
                          fill="none" strokeWidth={1.5} strokeDasharray={DRAWN[i].dash} strokeLinejoin="round" vectorEffect="non-scaling-stroke"
                        />
                        {/* A line of no length with round ends is a dot that the sideways stretch leaves round. */}
                        {s.values.map((v, w) => v !== null && (
                          <line key={weeks[w]} x1={middle(w)} x2={middle(w)} y1={height - 2 - tall(v)} y2={height - 2 - tall(v)} strokeWidth={4} strokeLinecap="round" vectorEffect="non-scaling-stroke" data-dot />
                        ))}
                      </g>
                    ))}
                  </svg>
                </td>
              </tr>
              {rows.map((s, i) => (
                <tr key={s.label} data-series={s.label}>
                  <th scope="row" className={cn(LABEL, "py-1 pl-0 pr-2 text-left align-top font-normal")}>
                    <span className="flex min-w-0 items-center gap-1.5">
                      <Swatch index={i} kind={kind} />
                      <span className="min-w-0 [overflow-wrap:anywhere]">{s.label}</span>
                    </span>
                  </th>
                  {s.values.map((v, w) => (
                    <td key={weeks[w]} className="whitespace-nowrap border-0 px-0 py-1 text-center align-top tabular-nums">
                      {v === null ? <><span aria-hidden="true">-</span><span className="sr-only">no value</span></> : format(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </figure>
  );
}
