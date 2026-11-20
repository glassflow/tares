import * as React from "react";

import { Banner } from "../../components/edition";
import { sinceWords } from "../../lib/time";
import type { HomeAnswered, HomeItem } from "./home.types";

// Home opened at one item, `/?item=<id>` (TR-1527): the link a Slack message carries. The matching
// row stands first and marked, outside any fold. When the item no longer waits, a line at the top says
// who answered it and when, from the answers record of any day, or that it no longer waits.

export const VIA: Record<HomeAnswered["via"], string> = {
  feed: "from the feed", console: "on Home", session: "in the crew lead's session", message: "from a message",
};

/** The id a link named: `<kind>:<ref>`, both parts there. Anything else is no link at all. */
export function parseFocus(search: string): string | undefined {
  const id = new URLSearchParams(search).get("item")?.trim();
  if (!id) return undefined;
  const at = id.indexOf(":");
  return at > 0 && id.slice(at + 1).trim() ? id : undefined;
}

/** The record names a desk answer `question:D7` or `permission:D7`; a team cell's row for it is `desk:D7`. */
const deskKey = (id: string) => id.replace(/^(question|permission|desk):/, "desk:");
export const sameItem = (a: string, b: string) => deskKey(a) === deskKey(b);

/** The row the link named, taken out of the list, and the rest in their order. */
export function splitFocus(items: HomeItem[], focus?: string): { focus?: HomeItem; rest: HomeItem[] } {
  const hit = focus ? items.find((i) => sameItem(i.id, focus)) : undefined;
  return hit ? { focus: hit, rest: items.filter((i) => i !== hit) } : { rest: items };
}

/** The short label above the marked row. It also brings the row into view, once, when it first shows. */
export function FocusLabel() {
  const ref = React.useRef<HTMLSpanElement>(null);
  React.useEffect(() => {
    ref.current?.closest("[data-focus]")?.scrollIntoView?.({ block: "nearest" });
  }, []);
  return (
    <div className="mb-0.5 font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-foreground">
      <span ref={ref} data-testid="home-focus-label">From your link</span>
    </div>
  );
}

/** The row's classes when a link named it: the accent bar at its edge, and a wash. */
export const FOCUS_ROW = "shadow-[inset_3px_0_0_var(--accent)] bg-muted pl-3";

export const CHOICE = (a: HomeAnswered) => (a.choice === "yes" ? "Yes" : a.choice === "no" ? "No" : "Answered");

/** What Home says when the item a link named no longer waits: who answered it and when, or that it is gone.
 *  `answer` is null when nothing recorded one (or the read failed). */
export function FocusNote({ answer, more }: { answer: HomeAnswered | null; more: boolean }) {
  const rest = more ? " Here is what still waits." : "";
  if (!answer) {
    return <Banner data-testid="home-focus-gone">The item that link named no longer waits for you.{rest}</Banner>;
  }
  return (
    <Banner data-testid="home-focus-answered" data-choice={answer.choice ?? "answered"}>
      <span>
        Already answered: <span data-user-text>{answer.ask}</span>{/[.?!]$/.test(answer.ask) ? " " : ". "}
        {/* an option or words alone when it was neither yes nor no, as the record says it */}
        {answer.choice && <>{CHOICE(answer)}{answer.said && answer.said !== CHOICE(answer) ? ", " : ""}</>}
        {answer.said && answer.said !== CHOICE(answer) && <span data-user-text>{answer.said}</span>}
        , by {answer.by}, {VIA[answer.via]}, {sinceWords(Date.parse(answer.at), Date.now())}.{rest}
      </span>
    </Banner>
  );
}
