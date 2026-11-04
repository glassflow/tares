import * as React from "react";

import { Button, EmptyState, ListRow } from "../../components/edition";
import { cn } from "@/lib/utils";
import { Detail } from "./HomeDetail";
import type { HomeAction, HomeItem } from "./home.types";

// "Waiting for you" on a cell with more than one person: only what is addressed to the person,
// each item with who it comes from. A cell of one keeps the list in FactoryHome.tsx.

const SHOW_FIRST = 8;
const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-[color:var(--accent-deep)]";
const MUTED = "text-xs text-muted-foreground";

// People other than the crew wear one of these, chosen by name so a person keeps their colour.
const PEOPLE: { bg: string; fg: string }[] = [
  { bg: "#1e0f00", fg: "#faf9ec" },
  { bg: "#2f5d8a", fg: "#ffffff" },
  { bg: "#6b4f8a", fg: "#ffffff" },
];
const CREW_COLOUR = { bg: "var(--accent)", fg: "var(--on-accent)" };

export function colourOf(key: string): { bg: string; fg: string } {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PEOPLE[h % PEOPLE.length];
}

/** The square next to a row: the crew's accent with a Y, or the person's initial in their colour.
 *  `small` is the member square on a project row. */
export function Sender({ from, small }: { from: HomeItem["from"]; small?: boolean }) {
  const crew = !from || from.crew === true;
  const name = crew ? "Your crew" : from.name;
  const c = crew ? CREW_COLOUR : colourOf(from.id ?? from.name);
  return (
    <span
      role="img" aria-label={small ? name : `From ${name}`} title={name} data-testid={small ? "project-member" : "waiting-from"}
      className={cn("grid flex-none select-none place-items-center font-semibold", small ? "size-5 text-[11px]" : "mt-0.5 size-7 text-[13px]")}
      style={{ background: c.bg, color: c.fg }}
      data-user-text
    >
      {(crew ? "Y" : Array.from(name)[0] ?? "?").toUpperCase()}
    </span>
  );
}

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
            <span className="font-semibold" data-user-text>{item.title}</span>
          </div>
          <div className="mt-0.5 [overflow-wrap:anywhere]" data-user-text><Detail text={item.detail} /></div>
          <div className={cn(MUTED, "mt-0.5")} data-user-text>{item.meta}</div>
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

export function WaitingForYou({ items, renderAction, errors, panels }: {
  items: HomeItem[];
  /** What a button of a row is; the page decides what a click does. */
  renderAction: (a: HomeAction, item: HomeItem) => React.ReactNode;
  /** A refusal's sentence by row id, shown under that row. */
  errors?: Record<string, string | undefined>;
  /** A panel open under a row, by row id (the answer box). */
  panels?: Record<string, React.ReactNode>;
}) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_FIRST);
  return (
    <section aria-label="Waiting for you" className="min-w-0" data-testid="waiting-team">
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="m-0! text-base!">Waiting for you</h2>
        <span className="text-[13px] text-muted-foreground">only what is addressed to you</span>
      </div>
      <div className="max-w-[860px] border border-border bg-card">
        {items.length === 0 ? (
          <EmptyState title="Nothing waits for you." className="py-7" />
        ) : (
          <>
            {shown.map((it) => <Row key={it.id} item={it} renderAction={renderAction} error={errors?.[it.id]} panel={panels?.[it.id]} />)}
            {items.length > shown.length && (
              <ListRow className="px-[18px]! py-3!">
                <Button variant="quiet" className="px-0! text-[color:var(--accent-deep)]!" onClick={() => setAll(true)}>
                  Show all {items.length}
                </Button>
              </ListRow>
            )}
          </>
        )}
      </div>
    </section>
  );
}
