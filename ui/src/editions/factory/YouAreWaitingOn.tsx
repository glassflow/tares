import * as React from "react";
import { Link } from "react-router-dom";

import { Button, ListRow } from "../../components/edition";
import { cn } from "@/lib/utils";
import type { HomeAction, HomeWaitingOn, HomeWaitingRepo } from "./home.types";

// "You are waiting on" on a cell with more than one person: each project the person leads that
// cannot go to a crew yet, with where every repo's owner stands. Not drawn when nothing waits.

const CHIP: Record<HomeWaitingRepo["state"], string> = {
  signed_off: "bg-[var(--ok-soft)] text-[color:var(--ok)]",
  waiting: "bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  no_owner: "bg-[var(--err-soft)] text-[color:var(--err)]",
  yours: "bg-muted text-muted-foreground",
};

/** "example-org/service-b · Kim · waiting": the repo, who owns it and where that stands. */
export function chipText(r: HomeWaitingRepo): string {
  if (r.state === "no_owner") return `${r.repo} · no owner`;
  if (r.state === "yours") return `${r.repo} · you`;
  return `${r.repo} · ${r.owner?.name ?? "someone"} · ${r.state === "signed_off" ? "signed off" : "waiting"}`;
}

/** The buttons the page can act on: a link, or Hand over anyway. Anything else is left out. */
function usable(actions: HomeAction[]): HomeAction[] {
  return actions.filter((a) => (a.href ? true : a.do === "hand_over_anyway"));
}

function ActionButton({ action, onHandOver }: { action: HomeAction; onHandOver?: () => void }) {
  const cls = "h-8 text-[13px]!" + (action.primary ? " border-foreground!" : "");
  const href = action.href;
  if (href) {
    return (
      <Button asChild variant="secondary" className={cls}>
        {href.startsWith("/") ? <Link to={href}>{action.label}</Link> : <a href={href}>{action.label}</a>}
      </Button>
    );
  }
  return <Button variant="secondary" className={cls} onClick={onHandOver}>{action.label}</Button>;
}

export function YouAreWaitingOn({ entries, onHandOver }: {
  entries: HomeWaitingOn[];
  /** The page opens the Hand over anyway dialog for this entry. */
  onHandOver?: (entry: HomeWaitingOn) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <section aria-label="You are waiting on" className="min-w-0" data-testid="waiting-on">
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="m-0! text-base!">You are waiting on</h2>
        <span className="text-[13px] text-muted-foreground">projects you lead</span>
      </div>
      <div className="max-w-[860px] border border-border bg-card">
        {entries.map((e) => {
          const actions = usable(e.actions);
          return (
            <ListRow key={e.project.id} data-testid="waiting-on-row"
                     className="items-start justify-between gap-x-4 gap-y-3 px-[18px]! py-4!">
              <div className="min-w-0 flex-[1_1_300px]">
                {/* the name opens the project; the row itself holds buttons, so it is not one link */}
                <Link to={`/projects/${encodeURIComponent(e.project.id)}`} data-testid="waiting-on-project" data-user-text
                      className="block w-fit font-semibold text-foreground! no-underline! [overflow-wrap:anywhere] hover:underline!">
                  {e.title}
                </Link>
                <div className="mt-0.5 [overflow-wrap:anywhere]" data-user-text>{e.detail}</div>
                {e.repos.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {e.repos.map((r) => (
                      <span key={r.repo} data-testid="waiting-on-chip" data-state={r.state} data-user-text
                            className={cn("px-2 py-0.5 text-xs [overflow-wrap:anywhere]", CHIP[r.state])}>
                        {chipText(r)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              {actions.length > 0 && (
                <div className="flex max-w-full flex-none flex-wrap gap-2">
                  {actions.map((a) => <ActionButton key={a.label} action={a} onHandOver={() => onHandOver?.(e)} />)}
                </div>
              )}
            </ListRow>
          );
        })}
      </div>
    </section>
  );
}
