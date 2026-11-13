import * as React from "react";

import { Button } from "../../components/edition";
import { cn } from "@/lib/utils";
import type { HomeAction, HomeItem } from "./home.types";

// What every "Waiting for you" list shares (TR-1021): the cell sends the items in order (what
// holds the crew up first, the rest, then what waited more than a week and holds nothing up);
// the page shows the first under "The crew is waiting on you", folds the older ones, and lets a
// desk question be answered with one of its own options.

export interface WaitingGroups { now: HomeItem[]; rest: HomeItem[]; older: HomeItem[] }

export function groupWaiting(items: HomeItem[]): WaitingGroups {
  const now = items.filter((i) => i.blocking);
  const rest = items.filter((i) => !i.blocking && !i.older);
  const older = items.filter((i) => !i.blocking && i.older);
  // older things fold away only below something newer; when they are all there is, they show
  return now.length + rest.length === 0 ? { now, rest: older, older: [] } : { now, rest, older };
}

/** The decision on top of the list: the first item the page draws, from what holds the crew up,
 *  else the rest. Its main action is Home's one primary button (TR-1275); none when nothing waits. */
export function topItem(items: HomeItem[]): HomeItem | undefined {
  const g = groupWaiting(items);
  return g.now[0] ?? g.rest[0];
}

/** What a button does, for a screen reader, when its label alone does not say it. Allow grants
 *  the command the crew asked for, so its name carries that command (TR-1275). */
export function actionName(action: HomeAction, item?: HomeItem): string | undefined {
  if (action.do !== "allow" || !item) return undefined;
  const cmd = /`([\s\S]+)`/.exec(item.detail)?.[1].split(/\s+/).join(" ").trim();
  if (!cmd) return undefined;
  const where = item.project ? ` in ${item.project.name}` : "";
  return `${action.label}: let the crew run ${cmd}${where}`; // words-ok
}

/** One button per option of a desk question, and the crew's recommendation above them. A click
 *  is an action `choose` with the option's own text, which the page sends as the answer. */
export function QuestionChoices({ item, render }: {
  item: HomeItem;
  render: (a: HomeAction, item: HomeItem) => React.ReactNode;
}) {
  const q = item.question;
  if (!q || q.options.length === 0) return null;
  return (
    <div className="mt-2 grid gap-1.5" data-testid="question-choices">
      {q.blocks && <div className="text-xs text-muted-foreground" data-user-text>Holds up {q.blocks}</div>}
      {q.recommendation && (
        <div className="text-[13px]" data-testid="question-recommendation">
          <span className="text-muted-foreground">The crew recommends: </span><span data-user-text>{q.recommendation}</span>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {q.options.map((o, i) => (
          <React.Fragment key={i}>
            {render({ label: o, primary: false, do: "choose", desk: q.desk, choice: o, letter: String.fromCharCode(65 + i) }, item)}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

/** The fold under a list: "3 older things", open in place. */
export function OlderFold({ items, children }: { items: HomeItem[]; children: (items: HomeItem[]) => React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  if (items.length === 0) return null;
  return (
    <>
      <div className="border-t border-border px-4 py-2.5" data-testid="waiting-older">
        <Button variant="quiet" className="px-0! text-foreground! underline!" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide" : "Show"} {items.length} older {items.length === 1 ? "thing" : "things"}
        </Button>
        {!open && <span className="ml-2 text-xs text-muted-foreground">waited more than a week, holding nothing up</span>}
      </div>
      {open && children(items)}
    </>
  );
}

/** A heading inside the list, above a group. */
export function GroupHead({ title, testid }: { title: string; testid: string }) {
  return (
    <div className={cn("border-b border-border bg-[var(--wash)] px-4 py-2 text-[12.5px] font-semibold")} data-testid={testid}>
      {title}
    </div>
  );
}
