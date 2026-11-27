import * as React from "react";

import { formatCount, formatDecimal } from "../../../lib/number";
import type { DeliveredFigures } from "../FactoryReport.api";

// What more than one section of the Report says or draws the same way.

export const tickets = (n: number) => (n === 1 ? "ticket" : "tickets");

export const percent = (value: number) => `${formatCount(value)}%`;

/** The share of the tickets first reviewed that passed that review, in whole percent. For a `reviewed`
 *  above 0. */
export const passPercent = (first: DeliveredFigures["first_pass"]) => Math.round((100 * first.passed) / first.reviewed);

export function roundsWords(rounds: number) {
  const round = formatDecimal(rounds, 1);
  return `${round} review ${round === "1" ? "round" : "rounds"} per ticket reviewed`;
}

export const CAPTION = "font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground";
// The platform pages style every table, th and td by element; each of those is set here. The section
// around lets a word break anywhere, for a long title; in a table the words stay whole.
export const TABLE = "w-full border-collapse border-0 bg-transparent text-[13px] [overflow-wrap:normal]";
export const HEAD = "border-0 border-b border-solid border-rule-strong bg-transparent py-1 pl-0 pr-3 text-left align-bottom font-mono text-[11px] font-normal uppercase tracking-[0.08em] text-muted-foreground last:pr-0";
export const CELL = "border-0 bg-transparent py-1.5 pl-0 pr-3 text-left align-top font-normal tabular-nums last:pr-0";

export function NoValue() {
  return <><span aria-hidden="true">-</span><span className="sr-only">no value</span></>;
}

/** A line of small print, a link in a line of text, and a second line in a cell or a list item. */
export const NOTE = "m-0 max-w-[640px] text-xs text-muted-foreground";
export const LINK = "text-foreground underline decoration-border underline-offset-[3px]";
export const SUB = "block text-xs text-muted-foreground";

/** A caption over a table that scrolls sideways when it does not fit. The box is `relative` so that
 *  text for screen readers in a far column stays inside it and does not widen the page. */
export function Figure({ title, children, ...props }: { title: string; children: React.ReactNode; "data-testid": string }) {
  const id = React.useId();
  return (
    <figure aria-labelledby={id} className="m-0 grid min-w-0 content-start gap-2" {...props}>
      <figcaption id={id} className={CAPTION}>{title}</figcaption>
      <div role="region" aria-labelledby={id} tabIndex={0} className="relative min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-ring">
        {children}
      </div>
    </figure>
  );
}
