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

/** A line that fills the width it is given. The stroke follows the text colour (`currentColor`). */
export function Sparkline({ points, marks = [], height = 32, className, ...props }: SparklineProps) {
  const n = points.length;
  const lo = n ? Math.min(...points) : 0;
  const hi = n ? Math.max(...points) : 0;
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * WIDTH : WIDTH / 2);
  // A flat line sits in the middle instead of dividing by zero.
  const y = (v: number) => (hi === lo ? height / 2 : height - PAD - ((v - lo) / (hi - lo)) * (height - 2 * PAD));
  // One point has no direction: draw it across the width so it is still visible.
  const line = n === 1 ? `0,${y(points[0])} ${WIDTH},${y(points[0])}` : points.map((v, i) => `${x(i)},${y(v)}`).join(" ");
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
      {n > 0 && <polyline points={line} fill="none" stroke="currentColor" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />}
    </svg>
  );
}
