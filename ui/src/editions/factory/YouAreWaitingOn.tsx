import * as React from "react";
import { Link } from "react-router-dom";

import { Button, ListRow } from "../../components/edition";
import type { HomeWaitingOn } from "./home.types";

// "You are waiting on" on a cell with more than one person: each project the person leads that has a
// question open for someone else, so it is not ready to start yet. Not drawn when nothing waits.

/** What the list holds: projects the person leads, projects nobody leads yet, or both. */
export function scopeText(entries: HomeWaitingOn[]): string {
  const none = entries.filter((e) => e.no_lead).length;
  if (none === 0) return "projects you lead";
  if (none === entries.length) return "projects with no lead yet";
  return "projects you lead, and ones with no lead yet";
}

export function YouAreWaitingOn({ entries }: { entries: HomeWaitingOn[] }) {
  if (entries.length === 0) return null;
  return (
    <section aria-label="You are waiting on" className="min-w-0" data-testid="waiting-on">
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="m-0! text-base!">You are waiting on</h2>
        <span className="text-[13px] text-muted-foreground" data-testid="waiting-on-scope">{scopeText(entries)}</span>
      </div>
      <div className="border border-border bg-card">
        {entries.map((e) => {
          const links = e.actions.filter((a) => a.href);
          return (
            <ListRow key={e.project.id} data-testid="waiting-on-row"
                     className="items-start justify-between gap-x-4 gap-y-3 px-[18px]! py-4!">
              <div className="min-w-0 flex-[1_1_300px]">
                {/* the name opens the project; the row itself holds buttons, so it is not one link */}
                <Link to={`/projects/${encodeURIComponent(e.project.id)}`} data-testid="waiting-on-project" data-user-text
                      className="block w-fit font-semibold text-foreground! no-underline! [overflow-wrap:anywhere] hover:underline!">
                  {e.title}
                </Link>
                {e.no_lead && <div className="mt-0.5 text-[13px] text-muted-foreground" data-testid="waiting-on-no-lead">No lead yet</div>}
                <div className="mt-0.5 [overflow-wrap:anywhere]" data-user-text>{e.detail}</div>
              </div>
              {links.length > 0 && (
                <div className="flex max-w-full flex-none flex-wrap gap-2">
                  {links.map((a) => (
                    <Button key={a.label} asChild variant="secondary" className="h-8 text-[13px]!">
                      <Link to={a.href!}>{a.label}</Link>
                    </Button>
                  ))}
                </div>
              )}
            </ListRow>
          );
        })}
      </div>
    </section>
  );
}
