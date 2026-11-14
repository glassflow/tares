import * as React from "react";

import { cn } from "@/lib/utils";

/** How a status reads. A page never picks a tone: it looks its status up in a table in
 *  statusTones.ts, so the same state reads the same way everywhere. */
export type Tone = "working" | "waiting" | "ready" | "done" | "bad" | "quiet";

// States are typographic (.impeccable.md): a mono word with a 1px underline. Amber is for live work
// only; waiting and quiet take a dashed neutral line, so a ticket that waits does not look like a button.
const TONE_TEXT: Record<Tone, string> = {
  working: "text-[color:var(--accent-deep)] decoration-[color:var(--accent)]",
  waiting: "text-foreground decoration-[color:var(--ink-soft)] decoration-dashed",
  ready: "text-[color:var(--ok)] decoration-[color:var(--ok)]",
  done: "text-[color:var(--ink-soft)] no-underline",
  bad: "text-[color:var(--err)] decoration-[color:var(--err)]",
  quiet: "text-[color:var(--ink-faint)] decoration-[color:var(--ink-faint)] decoration-dashed",
};
// The dot repeats the tone in a list where the word sits apart: solid for a state with colour,
// a 1px ring for the neutral ones.
const TONE_DOT: Record<Tone, string> = {
  working: "bg-[var(--accent)]",
  waiting: "border border-[color:var(--ink-soft)]",
  ready: "bg-[var(--ok)]",
  done: "bg-[var(--ink-soft)]",
  bad: "bg-[var(--err)]",
  quiet: "border border-[color:var(--ink-faint)]",
};

export interface StatusLabelProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone: Tone;
}

export function StatusLabel({ tone, className, ...props }: StatusLabelProps) {
  return (
    <span
      data-tone={tone}
      className={cn(
        "whitespace-nowrap font-mono text-[11px] font-medium uppercase tracking-[0.08em] underline decoration-1 underline-offset-[4px]",
        TONE_TEXT[tone],
        className,
      )}
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
      data-tone={tone}
      className={cn("inline-block size-2 shrink-0", TONE_DOT[tone], className)}
      {...props}
    />
  );
}
