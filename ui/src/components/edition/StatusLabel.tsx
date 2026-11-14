import * as React from "react";

import { cn } from "@/lib/utils";

/** How a status reads. A page never picks a tone: it looks its status up in a table in
 *  statusTones.ts, so the same state reads the same way everywhere. */
export type Tone = "working" | "waiting" | "ready" | "done" | "bad" | "quiet";

// States are typographic (.impeccable.md): a mono word with a 1px underline. Amber is for live work
// only; waiting and quiet take a dashed neutral line, so a ticket that waits does not look like a button.
const TONE_TEXT: Record<Tone, string> = {
  working: "text-brand-deep decoration-brand",
  waiting: "text-foreground decoration-muted-foreground decoration-dashed",
  ready: "text-ok decoration-ok",
  done: "text-muted-foreground no-underline",
  bad: "text-err decoration-err",
  quiet: "text-faint decoration-faint decoration-dashed",
};
// The dot repeats the tone in a list where the word sits apart: solid for a state with colour,
// a 1px ring for the neutral ones.
const TONE_DOT: Record<Tone, string> = {
  working: "bg-brand",
  waiting: "border border-muted-foreground",
  ready: "bg-ok",
  done: "bg-muted-foreground",
  bad: "bg-err",
  quiet: "border border-faint",
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
