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
