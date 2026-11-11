// Tares enterprise licence, see ee/LICENSE
import * as React from "react";
import { Link } from "react-router-dom";

import { Button, EmptyState, ListRow } from "@/components/edition";
import { cn } from "@/lib/utils";
import { Detail, DetailLines } from "@/editions/factory/HomeDetail";
import type { HomeAction, HomeItem, HomeWaitingOn, HomeWaitingOnProps, HomeWaitingProps } from "./home.types";
import { Sender, senderWords } from "./Sender";
import { InlineIcon, ItemIcon } from "@/editions/factory/ProjectIcon";
import { GroupHead, groupWaiting, OlderFold, QuestionChoices } from "@/editions/factory/waitingGroups";

// The team's side of the factory home: "Waiting for you" with who each item is from, and "You are
// waiting on". A cell of one keeps the plain list in FactoryHome.tsx.

const SHOW_FIRST = 8;
const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-[color:var(--accent-deep)]";
const MUTED = "text-xs text-muted-foreground";

function Row({ item, renderAction, error, panel }: {
  item: HomeItem; renderAction: (a: HomeAction, item: HomeItem) => React.ReactNode; error?: string; panel?: React.ReactNode;
}) {
  // Outline buttons first, the primary one last.
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <ListRow data-testid="waiting-row" className="items-start justify-between gap-x-4 gap-y-3 px-[18px]! py-4!">
      <div className="flex min-w-0 flex-[1_1_360px] gap-3">
        <Sender from={item.from} />
        <div className="min-w-0">
          <div className="[overflow-wrap:anywhere]">
            <span className={cn(KIND, "mr-2.5")}>{item.label}</span>
            <span className="font-semibold" data-user-text><ItemIcon project={item.project} title={item.title} />{item.title}</span>
          </div>
          <div className="mt-0.5 [overflow-wrap:anywhere]" data-user-text><Detail text={item.detail} /></div>
          <DetailLines lines={item.lines} />
          <div className={cn(MUTED, "mt-0.5")} data-testid="waiting-meta">
            From <span data-user-text>{senderWords(item.from)}</span> · <span data-user-text>{item.meta}</span>
          </div>
          <QuestionChoices item={item} render={renderAction} />
        </div>
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2">
          {actions.map((a) => <React.Fragment key={a.label}>{renderAction(a, item)}</React.Fragment>)}
        </div>
      )}
      {panel}
      {error && (
        <div role="alert" data-testid="row-error" className="basis-full pl-10 text-[13px] text-[color:var(--err)]" data-words-exempt>
          {error}
        </div>
      )}
    </ListRow>
  );
}

export function WaitingForYou({ items, renderAction, errors, panels }: HomeWaitingProps) {
  const [all, setAll] = React.useState(false);
  const g = groupWaiting(items);
  const rest = all ? g.rest : g.rest.slice(0, SHOW_FIRST);
  const row = (it: HomeItem) => <Row key={it.id} item={it} renderAction={renderAction} error={errors?.[it.id]} panel={panels?.[it.id]} />;
  return (
    <section aria-label="Waiting for you" className="min-w-0" data-testid="waiting-team">
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="m-0! text-base!">Waiting for you</h2>
        <span className="text-[13px] text-muted-foreground">only what is addressed to you</span>
      </div>
      <div className="border border-border bg-card">
        {items.length === 0 ? (
          <EmptyState title="Nothing waits for you." className="py-7" />
        ) : (
          <>
            {g.now.length > 0 && <GroupHead title="The crew is waiting on you" testid="waiting-now" />}
            {g.now.map(row)}
            {g.now.length > 0 && rest.length > 0 && <GroupHead title="When you have a moment" testid="waiting-rest" />}
            {rest.map(row)}
            {g.rest.length > rest.length && (
              <ListRow className="px-[18px]! py-3!">
                <Button variant="quiet" className="px-0! text-[color:var(--accent-deep)]!" onClick={() => setAll(true)}>
                  Show all {g.rest.length}
                </Button>
              </ListRow>
            )}
            <OlderFold items={g.older}>{(older) => older.map(row)}</OlderFold>
          </>
        )}
      </div>
    </section>
  );
}

/** What the list holds: projects the person leads, projects nobody leads yet, or both. */
export function scopeText(entries: HomeWaitingOn[]): string {
  const none = entries.filter((e) => e.no_lead).length;
  if (none === 0) return "projects you lead";
  if (none === entries.length) return "projects with no lead yet";
  return "projects you lead, and ones with no lead yet";
}

export function YouAreWaitingOn({ entries }: HomeWaitingOnProps) {
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
                  <InlineIcon icon={e.project.icon} />{e.title}
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
