import * as React from "react";

import { cn } from "@/lib/utils";

export type Tone = "working" | "waiting" | "ready" | "done" | "bad" | "quiet";

// Colour per tone, from the semantic tokens only.
const TONE_TEXT: Record<Tone, string> = {
  working: "text-[color:var(--slate)]",
  waiting: "text-[color:var(--warn)]",
  ready: "text-[color:var(--ok)]",
  done: "text-[color:var(--ink-soft)]",
  bad: "text-[color:var(--err)]",
  quiet: "text-[color:var(--ink-faint)]",
};
const TONE_FILL: Record<Tone, string> = {
  working: "bg-[var(--slate)]",
  waiting: "bg-[var(--warn)]",
  ready: "bg-[var(--ok)]",
  done: "bg-[var(--ink-soft)]",
  bad: "bg-[var(--err)]",
  quiet: "bg-[var(--ink-faint)]",
};

export interface StatusLabelProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone: Tone;
}

export function StatusLabel({ tone, className, ...props }: StatusLabelProps) {
  return (
    <span
      className={cn("font-mono text-[11px] uppercase tracking-[0.08em]", TONE_TEXT[tone], className)}
      {...props}
    />
  );
}

export interface StatusDotProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone: Tone;
  /** Says what the dot means; without it the dot is decoration and hidden from screen readers. */
  label?: string;
}

export function StatusDot({ tone, label, className, ...props }: StatusDotProps) {
  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("inline-block size-2 shrink-0", TONE_FILL[tone], className)}
      {...props}
    />
  );
}
