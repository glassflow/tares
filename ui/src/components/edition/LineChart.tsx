import * as React from "react";

import { EmptyState } from "@/components/edition/EmptyState";
import { cn } from "@/lib/utils";

export interface LineSeries {
  label: string;
  /** ISO time and value, oldest first. */
  points: [string, number][];
}

export interface LineMark {
  /** ISO time. */
  at: string;
  /** Shown on hover. */
  label: string;
}

export interface LineChartProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "children"> {
  /** At most two: a third is not drawn. */
  series: LineSeries[];
  /** Vertical dashed lines, such as deploys. */
  marks?: LineMark[];
  /** Pixels tall for the plot. Default 160. */
  height?: number;
  /** What the chart shows. Required. */
  "aria-label": string;
}

// Series colours come from the tokens: the first is the error colour, the second the soft ink.
const STROKE = ["stroke-[color:var(--err)]", "stroke-[color:var(--ink-soft)]"];
const SWATCH = ["bg-[var(--err)]", "bg-[var(--ink-soft)]"];

const WIDTH = 100;

/** The smallest of 1, 2, 2.5, 5, 10 times a power of ten that is at least `max`. */
function niceMax(max: number) {
  if (!(max > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(max));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * mag >= max) ?? 10;
  return step * mag;
}

const NUM = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const TIME = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const timeLabel = (ms: number, raw: string) => (Number.isNaN(ms) ? raw : TIME.format(ms));

/** A line chart in inline SVG: y from 0 to a rounded maximum with two gridlines, the first and last time under it, a legend. */
export function LineChart({ series, marks = [], height = 160, className, "aria-label": ariaLabel, ...props }: LineChartProps) {
  const shown = series.slice(0, 2).map((s) => ({ ...s, pts: s.points.map(([t, v]) => [Date.parse(t), v] as const).filter(([t]) => !Number.isNaN(t)) }));
  const all = shown.flatMap((s) => s.pts);
  if (all.length === 0) {
    return (
      <div className={cn("min-w-0", className)} {...props}>
        <EmptyState title="Nothing to chart">No data in this window.</EmptyState>
      </div>
    );
  }

  const t0 = Math.min(...all.map(([t]) => t));
  const t1 = Math.max(...all.map(([t]) => t));
  const top = niceMax(Math.max(...all.map(([, v]) => v)));
  const x = (t: number) => (t1 === t0 ? WIDTH / 2 : ((t - t0) / (t1 - t0)) * WIDTH);
  const y = (v: number) => height - (Math.min(Math.max(v, 0), top) / top) * height;
  const path = (pts: readonly (readonly [number, number])[]) =>
    pts.length === 1 ? `M0 ${y(pts[0][1])}H${WIDTH}` : pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t)} ${y(v)}`).join("");
  const first = shown.flatMap((s) => s.points).find(([t]) => Date.parse(t) === t0)?.[0] ?? "";
  const last = shown.flatMap((s) => s.points).find(([t]) => Date.parse(t) === t1)?.[0] ?? "";
  const lines = [0.5, 1].map((f) => ({ f, v: top * f }));
  const labelCh = Math.max(...lines.map(({ v }) => NUM.format(v).length), 1);
  const inRange = marks.map((m) => ({ ...m, ms: Date.parse(m.at) })).filter((m) => m.ms >= t0 && m.ms <= t1);

  return (
    <div className={cn("grid min-w-0 gap-2", className)} {...props}>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 font-mono text-[11px] text-muted-foreground">
        <div className="py-2">
          {/* The labels are positioned out of the flow, so the column gets its width from the longest one. */}
          <div className="relative" style={{ height, width: `${labelCh}ch` }}>
            {[{ f: 0, v: 0 }, ...lines].map(({ f, v }) => (
              <span key={f} className="absolute right-0 -translate-y-1/2 whitespace-nowrap" style={{ top: `${(1 - f) * 100}%` }}>
                {NUM.format(v)}
              </span>
            ))}
          </div>
        </div>
        <div className="py-2">
          <svg role="img" viewBox={`0 0 ${WIDTH} ${height}`} preserveAspectRatio="none" width="100%" height={height} className="block overflow-visible" aria-label={ariaLabel}>
            {lines.map(({ f }) => (
              <line key={f} x1={0} x2={WIDTH} y1={y(top * f)} y2={y(top * f)} strokeWidth={1} vectorEffect="non-scaling-stroke" className="stroke-[color:var(--line)]" />
            ))}
            <line x1={0} x2={WIDTH} y1={height} y2={height} strokeWidth={1} vectorEffect="non-scaling-stroke" className="stroke-[color:var(--ink-faint)]" />
            {inRange.map((m) => (
              <g key={`${m.at}-${m.label}`} data-mark>
                <line x1={x(m.ms)} x2={x(m.ms)} y1={0} y2={height} strokeWidth={1} strokeDasharray="4 3" vectorEffect="non-scaling-stroke" className="stroke-[color:var(--ink-soft)]" />
                {/* A wide, invisible line gives the hover a target a pixel could not. */}
                <line x1={x(m.ms)} x2={x(m.ms)} y1={0} y2={height} strokeWidth={12} stroke="transparent" vectorEffect="non-scaling-stroke">
                  <title>{m.label}</title>
                </line>
              </g>
            ))}
            {shown.map((s, i) => (
              <path key={s.label} d={path(s.pts)} fill="none" strokeWidth={1.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" className={STROKE[i]} />
            ))}
          </svg>
          <div className="mt-2 flex justify-between gap-2">
            <span>{timeLabel(t0, first)}</span>
            <span>{timeLabel(t1, last)}</span>
          </div>
        </div>
      </div>
      <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-xs">
        {shown.map((s, i) => (
          <li key={s.label} className="flex items-center gap-2">
            <span aria-hidden className={cn("inline-block h-0.5 w-4", SWATCH[i])} />
            {s.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
