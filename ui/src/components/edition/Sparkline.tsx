import * as React from "react";

import { cn } from "@/lib/utils";

export interface SparklineProps extends Omit<React.SVGAttributes<SVGSVGElement>, "points"> {
  points: number[];
  /** Indexes into `points` that get a vertical tick, such as the moment of a deploy. */
  marks?: number[];
  /** Pixels tall. Default 32. */
  height?: number;
  /** What the line shows. Required: the chart has no text of its own. */
  "aria-label": string;
}

const WIDTH = 100;
const PAD = 2;
// A lone point has no direction: a segment this wide, centred on its x, so it is still visible.
const LONE = 6;

/** A line that fills the width it is given. The stroke follows the text colour (`currentColor`). */
export function Sparkline({ points, marks = [], height = 32, className, ...props }: SparklineProps) {
  const n = points.length;
  // Values are not trusted: null, undefined, NaN and Infinity are dropped, not drawn as 0. A dropped value
  // still takes its slot, so every other point keeps its x and `marks` (indexes into `points`) still land on the right one.
  // The min and max come from a loop: a spread of a long series throws a RangeError.
  const kept: number[] = [];
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    const v = points[i];
    if (!Number.isFinite(v)) continue;
    kept.push(i);
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * WIDTH : WIDTH / 2);
  // A flat line sits in the middle instead of dividing by zero.
  const y = (v: number) => (hi === lo ? height / 2 : height - PAD - ((v - lo) / (hi - lo)) * (height - 2 * PAD));
  let line = "";
  if (kept.length === 1) {
    const from = Math.min(Math.max(x(kept[0]) - LONE / 2, 0), WIDTH - LONE);
    line = `${from},${y(points[kept[0]])} ${from + LONE},${y(points[kept[0]])}`;
  } else line = kept.map((i) => `${x(i)},${y(points[i])}`).join(" ");
  return (
    <svg
      role="img"
      viewBox={`0 0 ${WIDTH} ${height}`}
      preserveAspectRatio="none"
      width="100%"
      height={height}
      className={cn("block min-w-0 overflow-visible", className)}
      {...props}
    >
      {marks
        .filter((m) => Number.isInteger(m) && m >= 0 && m < n)
        .map((m) => (
          <line key={m} x1={x(m)} x2={x(m)} y1={0} y2={height} strokeWidth={1} vectorEffect="non-scaling-stroke" className="stroke-[color:var(--ink-faint)]" />
        ))}
      {kept.length > 0 && <polyline points={line} fill="none" stroke="currentColor" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />}
    </svg>
  );
}
