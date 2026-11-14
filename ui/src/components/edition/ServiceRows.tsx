import * as React from "react";
import { Link } from "react-router-dom";

import { Sparkline } from "@/components/edition/Sparkline";
import { StatusDot, StatusLabel, type Tone } from "@/components/edition/StatusLabel";
import { cn } from "@/lib/utils";

/** One service as a row. Screens turn their own answer into this, so the AI SRE Home and Services
 *  lists draw the same row. */
export interface ServiceRowItem {
  /** Stable key, usually the name. */
  id: string;
  name: string;
  /** Where the row opens. */
  to: string;
  /** What the person calls it, when that is not the name. */
  about?: string | null;
  /** The colour of the dot and of the state words. */
  tone: Tone;
  /** The state in words: "Normal", "Incident open for 2 hours". */
  stateWords: string;
  /** What it sends and from where: "Errors and traffic". */
  signals: string;
  /** Where it comes from: "from prod-otlp". Holds a person's own connection names. */
  feeds?: string;
  /** Lines of activity, such as "12 events in 24 hours", "Checked 4 minutes ago". */
  activity: string[];
  /** The last day, one count an hour. */
  spark: number[];
  /** Says what the line shows. */
  sparkLabel?: string;
  hidden?: boolean;
}

export interface ServiceRowsProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "children"> {
  rows: ServiceRowItem[];
  /** The heading over the line column. Default "Last day". */
  sparkHeading?: string;
}

// The state is said in words, so the colour only adds to it: a calm state reads in plain ink.
const CALM = new Set<Tone>(["ready", "quiet", "done"]);

const SPARK_TEXT: Record<Tone, string> = {
  working: "text-[color:var(--ink-soft)]",
  waiting: "text-[color:var(--warn)]",
  ready: "text-[color:var(--ink-soft)]",
  done: "text-[color:var(--ink-soft)]",
  bad: "text-[color:var(--err)]",
  quiet: "text-[color:var(--ink-faint)]",
};

// One grid for the heading and every row, so the columns line up.
const COLS = "md:grid md:grid-cols-[minmax(0,1.3fr)_minmax(80px,0.7fr)_minmax(0,1.2fr)_minmax(0,1fr)] md:gap-x-6";
const HEAD = "hidden border-b border-border px-[var(--row-x,18px)] py-2.5 font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground";

/** The rows of a services list: a dot, the name and what it is called, a one-day line, the state
 *  and what it sends, and its activity. Under 768px each row stacks. Put it in a ListSection or a
 *  bordered box. */
export function ServiceRows({ rows, sparkHeading = "Last day", className, ...props }: ServiceRowsProps) {
  return (
    <div className={cn("min-w-0", className)} {...props}>
      <div className={cn(HEAD, COLS)} aria-hidden>
        <span>Service</span>
        <span>{sparkHeading}</span>
        <span>Status</span>
        <span>Activity</span>
      </div>
      <ul className="m-0! list-none! p-0!">
        {rows.map((r) => (
          <li key={r.id} className="border-t border-border first:border-t-0" data-testid="service-row" data-name={r.name} data-state-tone={r.tone}>
            <Link
              to={r.to}
              className={cn(
                "grid min-w-0 gap-y-2 px-[var(--row-x,18px)] py-4 [overflow-wrap:anywhere] no-underline! hover:bg-[var(--row-hover)]",
                "text-foreground! focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--accent)]",
                COLS,
                "md:items-start",
              )}
            >
              <span className="flex min-w-0 items-start gap-2.5">
                <StatusDot tone={r.tone} className="mt-[7px]" />
                <span className="min-w-0">
                  <b data-user-text data-testid="service-name" className="block font-semibold">{r.name}</b>
                  {r.about && <span data-user-text data-testid="service-about" className="block text-[13px] text-muted-foreground">{r.about}</span>}
                  {r.hidden && <span data-testid="service-hidden" className="block text-xs text-muted-foreground">Hidden</span>}
                </span>
              </span>
              <span className="min-w-0 self-center">
                <span className={cn("block", SPARK_TEXT[r.tone])}>
                  <Sparkline points={r.spark} height={26} aria-label={r.sparkLabel ?? `${r.name}, errors in the last day`} />
                </span>
              </span>
              <span className="min-w-0 text-[13px]">
                <StatusLabel tone={r.tone} data-testid="service-state"
                  className={cn("block font-sans text-[13px] font-medium normal-case tracking-normal", CALM.has(r.tone) && "text-foreground!")}>
                  {r.stateWords}
                </StatusLabel>
                <span className="block text-muted-foreground">
                  {r.signals}
                  {r.feeds && <>{", "}<span data-user-text>{r.feeds}</span></>}
                </span>
              </span>
              <span className="min-w-0 text-[13px]" data-testid="service-activity">
                {r.activity.map((a, i) => (
                  <span key={i} className={cn("block", i > 0 && "text-muted-foreground")}>{a}</span>
                ))}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
