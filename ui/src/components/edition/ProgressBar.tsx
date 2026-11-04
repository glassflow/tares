import * as React from "react";

import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

export interface ProgressBarProps {
  /** Done, 0 to 100. */
  value: number;
  /** Blocked, 0 to 100, drawn striped after the done part. */
  blocked?: number;
  /** Required: says what the bar measures. */
  "aria-label": string;
  className?: string;
  "data-testid"?: string;
}

const clamp = (n: number) => Math.min(100, Math.max(0, Number.isFinite(n) ? n : 0));

export function ProgressBar({ value, blocked = 0, className, ...props }: ProgressBarProps) {
  const done = clamp(value);
  const stuck = Math.min(clamp(blocked), 100 - done);
  return (
    <Progress
      value={done}
      className={cn("h-1.5", className)}
      getValueLabel={() => (stuck > 0 ? `${done}% done, ${stuck}% blocked` : `${done}% done`)}
      {...props}
    >
      {stuck > 0 && (
        <div
          aria-hidden
          className="absolute inset-y-0 bg-[repeating-linear-gradient(135deg,var(--err)_0_3px,transparent_3px_6px)]"
          style={{ left: `${done}%`, width: `${stuck}%` }}
        />
      )}
    </Progress>
  );
}
