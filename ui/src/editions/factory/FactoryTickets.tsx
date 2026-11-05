import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ChevronDown, ChevronRight } from "../../components/icons";
import { Banner, Button, EmptyState, Skeleton } from "../../components/edition";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  factoryTickets, type FactoryTicketsData, type StateKey, type TicketRow, type TicketSort, type TicketsQuery,
} from "./FactoryTickets.api";
import { isSample, sampleLoad } from "./ticketsSamples";

// Tickets: every ticket of every project the crew holds, as a list, a board or swimlanes. One read
// (GET /api/factory/tickets), repeated every 20 seconds. The address keeps the layout and the filters:
// ?layout=list|board|lanes, ?group=milestone|who (swimlanes), ?project=&state=&milestone=&q=&sort=.
// A card or a row is one link to the ticket's page; nothing here moves a ticket. The List pages by 25
// (Show more asks for the next 25); the Board and the Swimlanes read the 200 newest tickets once and
// group them by state here. Titles, project and milestone names are the person's own text and marked
// data-user-text.

const POLL_MS = 20_000;
const PAGE = 25;
const BOARD_LIMIT = 200;
const BOARD_CAP = 8;
const LANE_CAP = 5;
const PREF_KEY = "tares-tickets-layout";
/** Below this width the board and the swimlanes show one state at a time, as tabs. */
const NARROW_QUERY = "(max-width: 1000px)";

type Layout = "list" | "board" | "lanes";
type Group = "project" | "milestone" | "who";

const LAYOUTS: { value: Layout; label: string }[] = [
  { value: "list", label: "List" },
  { value: "board", label: "Board" },
  { value: "lanes", label: "Swimlanes" },
];
const GROUPS: { value: Group; label: string }[] = [
  { value: "project", label: "Group by project" },
  { value: "milestone", label: "Group by milestone" },
  { value: "who", label: "Group by who works on it" },
];
const SORTS: { value: TicketSort; label: string }[] = [
  { value: "new", label: "Last change, newest first" },
  { value: "old", label: "Last change, oldest first" },
  { value: "ref", label: "Ref" },
];

/** The state words, the keys the address uses and the order the columns run in. */
const STATE_LABEL: Record<StateKey, string> = {
  to_do: "To do", in_progress: "In progress", in_review: "In review", changes_asked: "Changes asked",
  blocked: "Blocked", merged: "Merged", canceled: "Canceled",
};
const STATE_URL: Record<StateKey, string> = {
  to_do: "to-do", in_progress: "in-progress", in_review: "in-review", changes_asked: "changes-asked",
  blocked: "blocked", merged: "merged", canceled: "canceled",
};
const COLUMNS: StateKey[] = ["to_do", "in_progress", "in_review", "changes_asked", "blocked", "merged"];
const MOVING_KEYS: StateKey[] = ["in_progress", "in_review", "changes_asked"];
/** The order the State filter lists them in. */
const FILTER_STATES: StateKey[] = ["in_progress", "in_review", "changes_asked", "blocked", "to_do", "merged", "canceled"];

const stateFromUrl = (s: string): StateKey | "moving" | "" => {
  if (s === "moving") return "moving";
  return (Object.keys(STATE_URL) as StateKey[]).find((k) => STATE_URL[k] === s) ?? "";
};

const DOT: Record<StateKey, string> = {
  to_do: "border-[color:var(--ink-faint)] bg-transparent",
  in_progress: "border-[color:var(--accent)] bg-[var(--accent)]",
  in_review: "border-[color:var(--accent-deep)] bg-[var(--accent-deep)] dark:border-[3px] dark:bg-transparent",
  changes_asked: "border-[color:var(--warn)] bg-[var(--warn)]",
  merged: "border-[color:var(--ok)] bg-[var(--ok)]",
  blocked: "border-[color:var(--err)] bg-[var(--err)]",
  canceled: "border-[color:var(--ink-faint)] bg-[var(--ink-faint)]",
};

const MUTED = "text-muted-foreground";
const FIELD = "h-9! w-full! md:w-auto! min-w-0 border! border-border! bg-[var(--input-bg)]! px-2.5! py-0! text-sm! text-foreground! disabled:opacity-60";
const LINKISH = "cursor-pointer! border-0! bg-transparent! p-0! text-[length:inherit]! font-normal! text-foreground! underline! decoration-border! underline-offset-[3px]";
const TILE_LINK = "no-underline! text-foreground! hover:text-foreground! focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]";

// ----------------------------------------------------------------------------------------- small things

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const keyOf = (t: TicketRow) => `${t.project.id}/${t.ref}`;
/** "27 minutes ago" as "27 min ago", for the narrow places. */
const short = (s: string | null) =>
  (s ?? "").replace(/ minutes?/, " min").replace(/ hours?/, " hr").replace(/ weeks?/, " wk").replace(/ months?/, " mo");
const whoOf = (t: TicketRow) => t.who ?? (t.state_key === "canceled" ? "No one" : "");
const blockedText = (t: TicketRow) => (t.blocked_by.length ? `Blocked by ${t.blocked_by.join(", ")}` : "Blocked");
const stateText = (t: TicketRow) => (t.state_key === "blocked" ? blockedText(t) : STATE_LABEL[t.state_key] ?? t.state);
const prText = (t: TicketRow) => {
  if (t.pr) return [t.pr.number != null ? `PR ${t.pr.number}` : "", t.pr.state_text].filter(Boolean).join(" ");
  return t.state_key === "in_progress" ? "No PR yet" : "";
};

function readPref(): Layout | null {
  try {
    const v = window.localStorage.getItem(PREF_KEY);
    return v === "list" || v === "board" || v === "lanes" ? v : null;
  } catch {
    return null;
  }
}
function writePref(l: Layout) {
  try {
    window.localStorage.setItem(PREF_KEY, l);
  } catch {
    // The choice still works for this visit.
  }
}

function useNarrow(): boolean {
  const [narrow, setNarrow] = React.useState(() => typeof window !== "undefined" && window.matchMedia(NARROW_QUERY).matches);
  React.useEffect(() => {
    const m = window.matchMedia(NARROW_QUERY);
    const on = () => setNarrow(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return narrow;
}

function Dot({ k }: { k: StateKey }) {
  return <i aria-hidden className={cn("inline-block size-[9px] shrink-0 border", DOT[k])} />;
}

// ----------------------------------------------------------------------------------------- tickets

/** The whole row or card is one link to the ticket; its words are the ticket's own. */
function Row({ t }: { t: TicketRow }) {
  const canceled = t.state_key === "canceled";
  const pr = prText(t);
  const cell = "min-w-0 text-[13px]";
  return (
    <Link to={t.href} data-testid="ticket-row" data-ref={t.ref} data-state={t.state_key}
          className={cn("group grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-2.5 gap-y-1 border-t border-border px-3.5 py-3 first:border-t-0",
            "min-[1360px]:grid-cols-[56px_minmax(0,1fr)_100px_120px_108px_130px_140px_88px] min-[1360px]:gap-x-3 min-[1360px]:px-4 min-[1360px]:py-2.5",
            "hover:bg-[var(--row-hover)] hover:shadow-[inset_3px_0_0_var(--accent)]", TILE_LINK)}>
      <span className={cn("col-start-1 row-start-1 pt-0.5 font-mono text-xs font-medium", "min-[1360px]:col-auto min-[1360px]:row-auto")} data-testid="ticket-ref">{t.ref}</span>
      <span data-user-text data-testid="ticket-title" title={t.title}
            className={cn("col-span-2 row-start-2 line-clamp-2 min-w-0 font-semibold [overflow-wrap:anywhere] group-hover:underline",
              "min-[1360px]:col-span-1 min-[1360px]:row-auto", canceled && "text-muted-foreground line-through")}>
        {t.title}
      </span>
      <span className={cn("col-span-2 row-start-3 flex min-w-0 flex-wrap gap-y-0.5 text-[12.5px]", "min-[1360px]:contents")}>
        <span data-user-text className={cn(cell, "font-medium [overflow-wrap:anywhere] after:mx-[7px] after:font-normal after:text-muted-foreground after:content-['·']", "min-[1360px]:pt-px min-[1360px]:after:content-none")}>{t.project.name}</span>
        <span data-user-text title={t.milestone ? `${t.milestone.number} ${t.milestone.name}` : undefined}
              className={cn(cell, MUTED, "after:mx-[7px] after:content-['·'] last:after:content-none", "min-[1360px]:truncate min-[1360px]:pt-px min-[1360px]:after:content-none")}>
          {t.milestone ? `${t.milestone.number} ${t.milestone.name}` : ""}
        </span>
        <span className={cn(cell, canceled && MUTED, "[overflow-wrap:anywhere]", "min-[1360px]:pt-px")}>{whoOf(t)}</span>
      </span>
      <span className={cn("col-start-2 row-start-1 flex items-baseline justify-end gap-[7px] text-[13px]", "min-[1360px]:col-auto min-[1360px]:row-auto min-[1360px]:justify-start min-[1360px]:pt-px", canceled && MUTED)}>
        <span className="translate-y-px"><Dot k={t.state_key} /></span>
        <span data-testid="ticket-state">{stateText(t)}</span>
      </span>
      <span className={cn("col-start-1 row-start-4 min-w-0 text-[12.5px] [overflow-wrap:anywhere]", MUTED, "min-[1360px]:col-auto min-[1360px]:row-auto min-[1360px]:pt-0.5")}>{pr}</span>
      <span className={cn("col-start-2 row-start-4 whitespace-nowrap text-right text-[12.5px]", MUTED, "min-[1360px]:col-auto min-[1360px]:row-auto min-[1360px]:text-left min-[1360px]:pt-0.5")}>{t.changed ?? ""}</span>
    </Link>
  );
}

type CardCtx = "board" | Group;

function Card({ t, ctx }: { t: TicketRow; ctx: CardCtx }) {
  const who = whoOf(t);
  const ms = t.milestone?.number ?? "";
  const meta = (ctx === "board" ? [t.project.name, who]
    : ctx === "who" ? [t.project.name, ms]
    : ctx === "project" ? [who || ms]
    : [who]).filter(Boolean).join(" · ");
  const blocked = t.state_key === "blocked" && t.blocked_by.length > 0;
  const pr = blocked ? blockedText(t) : prText(t);
  return (
    <Link to={t.href} data-testid="ticket-card" data-ref={t.ref} data-state={t.state_key}
          className={cn("group flex min-w-0 flex-col gap-[3px] border border-border bg-card px-2.5 py-[9px]",
            "hover:border-foreground! hover:shadow-[inset_3px_0_0_var(--accent)]", TILE_LINK)}>
      <span className="flex min-w-0 justify-between gap-1.5 whitespace-nowrap text-xs text-muted-foreground">
        <span className="font-mono font-medium text-foreground">{t.ref}</span>
        <span className="min-w-0 truncate">{short(t.changed)}</span>
      </span>
      <span data-user-text title={t.title}
            className={cn("line-clamp-2 text-[13px] font-semibold leading-[1.35] [overflow-wrap:anywhere] group-hover:underline",
              t.state_key === "canceled" && "text-muted-foreground line-through")}>
        {t.title}
      </span>
      {meta && <span data-user-text className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{meta}</span>}
      {pr && <span className={cn("text-xs [overflow-wrap:anywhere]", blocked ? "font-medium text-[color:var(--err)]" : MUTED)}>{pr}</span>}
    </Link>
  );
}

/** Cards for one cell: the first `cap`, then "Showing 8 of 14" and "Show 8 more". */
function Cell({ rows, total, cap, id, ctx, quiet, caps, bump, listHref }: {
  rows: TicketRow[]; total: number; cap: number; id: string; ctx: CardCtx; quiet?: boolean;
  caps: Record<string, number>; bump: (id: string, cap: number) => void;
  /** The List filtered to this column, for when the column counts more tickets than the board read. */
  listHref?: string;
}) {
  const lim = caps[id] ?? cap;
  const shown = rows.slice(0, lim);
  const rest = total - shown.length;
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="cell" data-cell={id}>
      {rows.length === 0
        ? (quiet ? null : <div className="px-0.5 py-1 text-[12.5px] text-muted-foreground">No tickets</div>)
        : shown.map((t) => <Card key={keyOf(t)} t={t} ctx={ctx} />)}
      {rest > 0 && (
        <div className="flex flex-col items-stretch gap-1.5 pt-0.5 text-xs text-muted-foreground">
          <span data-testid="cell-showing">Showing {shown.length} of {total}</span>
          {shown.length < rows.length || !listHref ? (
            <Button variant="secondary" className="h-8!" data-testid="cell-more" onClick={() => bump(id, cap)}>
              Show {Math.min(cap, rows.length - shown.length || rest)} more
            </Button>
          ) : (
            <Link to={listHref} data-testid="cell-list" className="text-center text-foreground! underline decoration-border underline-offset-[3px]">
              See all {total} in the List
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------------------- lanes

interface Lane { key: string; label: string; rank: number[]; rows: TicketRow[] }

function whoRank(w: string | null): number {
  if (!w) return 500;
  const m = /^Builder for M(\d+)/.exec(w);
  if (m) return 100 + Number(m[1]);
  if (w === "Reviewer") return 200;
  if (w === "Lead") return 210;
  if (w === "Not picked up yet") return 400;
  return 300;
}

/** Lanes in the order the page promises: projects as the Projects page lists them, milestones in
 *  order, then the people who work on tickets. Lanes with no tickets are not drawn. */
function makeLanes(rows: TicketRow[], group: Group, projects: FactoryTicketsData["projects"]): Lane[] {
  const at = (id: string) => {
    const i = projects.findIndex((p) => p.id === id);
    return i < 0 ? 999 : i;
  };
  const map = new Map<string, Lane>();
  for (const t of rows) {
    let key: string, label: string, rank: number[];
    if (group === "project") {
      key = t.project.id; label = t.project.name; rank = [at(t.project.id)];
    } else if (group === "milestone") {
      const n = t.milestone ? Number(t.milestone.number.replace(/\D/g, "")) || 998 : 999;
      key = `${t.project.id}|${t.milestone?.id ?? ""}`;
      label = `${t.project.name}, ${t.milestone ? `${t.milestone.number} ${t.milestone.name}` : "no milestone"}`;
      rank = [at(t.project.id), n];
    } else {
      key = t.who ?? "";
      label = t.who ?? "Finished";
      rank = [whoRank(t.who)];
    }
    const lane = map.get(key) ?? { key, label, rank, rows: [] };
    lane.rows.push(t);
    map.set(key, lane);
  }
  return [...map.values()].sort((a, b) => {
    for (let i = 0; i < Math.max(a.rank.length, b.rank.length); i++) {
      const d = (a.rank[i] ?? 0) - (b.rank[i] ?? 0);
      if (d) return d;
    }
    return a.label.localeCompare(b.label);
  });
}

const movingIn = (rows: TicketRow[]) => rows.filter((r) => MOVING_KEYS.includes(r.state_key)).length;

function LaneHead({ lane, open, toggle, sub, userText }: { lane: Lane; open: boolean; toggle: () => void; sub: string; userText: boolean }) {
  const Chev = open ? ChevronDown : ChevronRight;
  return (
    <button type="button" aria-expanded={open} onClick={toggle} data-testid="lane-toggle"
            className={cn("flex! w-full items-center! gap-2.5 border-0! bg-card! px-3! py-2.5! text-left! text-foreground! hover:bg-[var(--row-hover)]!", open && "border-b! border-border!")}>
      <Chev className="size-3.5 shrink-0" aria-hidden />
      <b className="min-w-0 text-[15px] [overflow-wrap:anywhere]" {...(userText ? { "data-user-text": true } : {})}>{lane.label}</b>
      <span className="ml-auto text-right text-[12.5px] font-normal text-muted-foreground" data-testid="lane-sub">{sub}</span>
    </button>
  );
}

// ----------------------------------------------------------------------------------------- bodies

interface BodyProps {
  data: FactoryTicketsData;
  rows: TicketRow[];
  cols: StateKey[];
  group: Group;
  narrow: boolean;
  caps: Record<string, number>;
  bump: (id: string, cap: number) => void;
  closed: Record<string, boolean>;
  flip: (id: string) => void;
  /** The List, with today's filters, narrowed to one state. */
  listOf: (k: StateKey) => string;
}

const countOf = (data: FactoryTicketsData, k: StateKey) => data.counts[k];
const colTotal = (data: FactoryTicketsData, rows: TicketRow[], k: StateKey) => {
  const n = rows.filter((r) => r.state_key === k).length;
  return data.more ? Math.max(n, countOf(data, k)) : n;
};

function StateTabs({ cols, data, tab, setTab }: { cols: StateKey[]; data: FactoryTicketsData; tab: StateKey; setTab: (k: StateKey) => void }) {
  return (
    <div role="tablist" aria-label="State" className="grid grid-cols-3 gap-1.5" data-testid="state-tabs">
      {cols.map((k) => (
        <button key={k} type="button" role="tab" aria-selected={k === tab} data-testid="state-tab" data-state={k} onClick={() => setTab(k)}
                className={cn("flex! min-h-[50px] min-w-0 flex-col! items-start! justify-center! gap-0.5 border! border-border! bg-[var(--input-bg)]! px-2! py-1.5! text-left! text-[12.5px]! font-normal! text-foreground!",
                  k === tab && "border-foreground! bg-[var(--wash)]! font-semibold! shadow-[inset_0_-3px_0_var(--accent)]")}>
          <span className="flex min-w-0 items-center gap-1.5"><Dot k={k} /><span className="truncate">{STATE_LABEL[k]}</span></span>
          <b className="pl-[15px] text-[15px]">{countOf(data, k)}</b>
        </button>
      ))}
    </div>
  );
}

function BoardBody(p: BodyProps) {
  const { data, rows, cols, narrow, caps, bump, listOf } = p;
  const [tab, setTab] = useTab(cols);
  if (narrow) {
    const list = rows.filter((r) => r.state_key === tab);
    return (
      <div className="grid gap-3">
        <StateTabs cols={cols} data={data} tab={tab} setTab={setTab} />
        <Cell rows={list} total={colTotal(data, rows, tab)} cap={BOARD_CAP} id={`b|${tab}`} ctx="board" caps={caps} bump={bump} listHref={listOf(tab)} />
      </div>
    );
  }
  return (
    <div className="grid items-start gap-2.5" style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(0, 1fr))`, maxWidth: cols.length * 360 }} data-testid="board">
      {cols.map((k) => {
        const list = rows.filter((r) => r.state_key === k);
        return (
          <section key={k} className="min-w-0 border border-border bg-[var(--wash)]" data-testid="board-col" data-state={k}>
            <div className="flex min-w-0 items-center gap-[7px] border-b border-border px-2.5 py-[9px] text-[13px] font-semibold">
              <Dot k={k} /><span className="truncate">{STATE_LABEL[k]}</span>
              <span className="ml-auto text-[12.5px] font-medium text-muted-foreground" data-testid="col-count">{countOf(data, k)}</span>
            </div>
            <div className="p-2">
              <Cell rows={list} total={colTotal(data, rows, k)} cap={BOARD_CAP} id={`b|${k}`} ctx="board" caps={caps} bump={bump} listHref={listOf(k)} />
            </div>
          </section>
        );
      })}
    </div>
  );
}

/** The state tab a narrow screen shows; it follows the columns, so a filter that hides it picks the next. */
function useTab(cols: StateKey[]): [StateKey, (k: StateKey) => void] {
  const [tab, setTab] = React.useState<StateKey>("in_progress");
  return [cols.includes(tab) ? tab : cols[0], setTab];
}

function LanesBody(p: BodyProps) {
  const { data, rows, cols, group, narrow, caps, bump, closed, flip } = p;
  const [tab, setTab] = useTab(cols);
  const ctx: CardCtx = group;
  const userText = group !== "who";
  if (narrow) {
    const list = rows.filter((r) => r.state_key === tab);
    const lanes = makeLanes(list, group, data.projects);
    return (
      <div className="grid gap-3">
        <StateTabs cols={cols} data={data} tab={tab} setTab={setTab} />
        {lanes.length === 0 && <div className="text-[12.5px] text-muted-foreground">No tickets</div>}
        {lanes.map((l) => {
          const id = `${group}|${l.key}`;
          const open = !closed[id];
          return (
            <section key={id} className="border border-border bg-card" data-testid="lane" data-lane={l.label}>
              <LaneHead lane={l} open={open} toggle={() => flip(id)} sub={plural(l.rows.length, "ticket", "tickets")} userText={userText} />
              {open && (
                <div className="bg-[var(--wash)] p-2.5">
                  <Cell rows={l.rows} total={l.rows.length} cap={BOARD_CAP} id={`l|${id}|${tab}`} ctx={ctx} caps={caps} bump={bump} />
                </div>
              )}
            </section>
          );
        })}
      </div>
    );
  }
  const tpl = { gridTemplateColumns: `repeat(${cols.length}, minmax(0, 1fr))` };
  const lanes = makeLanes(rows, group, data.projects);
  return (
    <div className="grid gap-3" style={{ maxWidth: cols.length * 360 }} data-testid="lanes">
      <div className="sticky top-0 z-[2] bg-background px-[11px]" data-testid="lane-heads">
        <div className="grid gap-2.5" style={tpl}>
          {cols.map((k) => (
            <div key={k} className="flex min-w-0 items-center gap-[7px] border border-border bg-[var(--wash)] px-2.5 py-[9px] text-[13px] font-semibold">
              <Dot k={k} /><span className="truncate">{STATE_LABEL[k]}</span>
              <span className="ml-auto text-[12.5px] font-medium text-muted-foreground" data-testid="col-count">{countOf(data, k)}</span>
            </div>
          ))}
        </div>
      </div>
      {lanes.map((l) => {
        const id = `${group}|${l.key}`;
        const open = !closed[id];
        return (
          <section key={id} className="border border-border bg-card" data-testid="lane" data-lane={l.label}>
            <LaneHead lane={l} open={open} toggle={() => flip(id)} sub={`${plural(l.rows.length, "ticket", "tickets")}, ${movingIn(l.rows)} moving`} userText={userText} />
            {open && (
              <div className="grid items-start gap-2.5 bg-[var(--wash)] p-2.5" style={tpl}>
                {cols.map((k) => {
                  const list = l.rows.filter((r) => r.state_key === k);
                  return <Cell key={k} rows={list} total={list.length} cap={LANE_CAP} id={`l|${id}|${k}`} ctx={ctx} quiet caps={caps} bump={bump} />;
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function ListBody({ rows }: { rows: TicketRow[] }) {
  const th = "text-xs font-semibold text-muted-foreground";
  return (
    <div className="border border-border bg-card" data-testid="list">
      <div className={cn("hidden items-center gap-x-3 border-b border-border bg-[var(--wash)] px-4 py-2", th, "min-[1360px]:grid min-[1360px]:grid-cols-[56px_minmax(0,1fr)_100px_120px_108px_130px_140px_88px]")} aria-hidden>
        <div>Ref</div><div>Title</div><div>Project</div><div>Milestone</div><div>Worked by</div><div>State</div><div>Pull request</div><div>Changed</div>
      </div>
      {rows.map((t) => <Row key={keyOf(t)} t={t} />)}
    </div>
  );
}

// ----------------------------------------------------------------------------------------- page

interface Filters { project: string; state: StateKey | "moving" | ""; milestone: string; q: string; sort: TicketSort | "" }

interface Loaded { key: string; resp: FactoryTicketsData }

function Head({ sub }: { sub?: string }) {
  return (
    <div>
      <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Tickets</h1>
      {sub && <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground" data-testid="tickets-sub">{sub}</p>}
    </div>
  );
}

function SkeletonBody({ layout, narrow }: { layout: Layout; narrow: boolean }) {
  const card = (i: number) => (
    <div key={i} className="grid gap-2 border border-border bg-card p-2.5">
      <Skeleton width="40%" /><Skeleton width="92%" /><Skeleton width="70%" />
    </div>
  );
  let body: React.ReactNode;
  if (layout === "list") {
    body = (
      <div className="border border-border bg-card">
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="grid grid-cols-2 gap-3 border-t border-border px-4 py-3.5 first:border-t-0">
            <Skeleton width="60%" /><Skeleton width="85%" /><Skeleton width="75%" /><Skeleton width="50%" />
          </div>
        ))}
      </div>
    );
  } else if (narrow) {
    body = (
      <div className="grid gap-3">
        <div className="grid grid-cols-3 gap-1.5">
          {Array.from({ length: 6 }, (_, i) => <div key={i} className="grid min-h-[50px] gap-2 border border-border bg-[var(--input-bg)] p-2"><Skeleton width="80%" /><Skeleton width="30%" /></div>)}
        </div>
        {[0, 1, 2, 3].map(card)}
      </div>
    );
  } else {
    body = (
      <div className="grid items-start gap-2.5" style={{ gridTemplateColumns: "repeat(6, minmax(0, 1fr))" }}>
        {COLUMNS.map((k) => (
          <section key={k} className="min-w-0 border border-border bg-[var(--wash)]">
            <div className="border-b border-border px-2.5 py-3"><Skeleton width="60%" /></div>
            <div className="grid gap-2 p-2">{[0, 1, 2].map(card)}</div>
          </section>
        ))}
      </div>
    );
  }
  return <div role="status" aria-busy="true" aria-label="Loading" data-testid="tickets-loading">{body}</div>;
}

function Tickets() {
  const [params, setParams] = useSearchParams();
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const narrow = useNarrow();

  const filters: Filters = {
    project: params.get("project") ?? "",
    state: stateFromUrl(params.get("state") ?? ""),
    milestone: params.get("milestone") ?? "",
    q: params.get("q") ?? "",
    sort: ((s) => (s === "old" || s === "ref" ? s : ""))(params.get("sort")),
  };
  const hasFilter = !!(filters.project || filters.state || filters.milestone || filters.q);
  const layoutParam = params.get("layout");
  const layout: Layout = layoutParam === "list" || layoutParam === "board" || layoutParam === "lanes"
    ? layoutParam : (readPref() ?? (hasFilter ? "list" : "board"));
  const group: Group = ((g) => (g === "milestone" || g === "who" ? g : "project"))(params.get("group"));

  // What the read asks for. The List pages by 25; the Board and the Swimlanes take 200, open first.
  const query: TicketsQuery = {
    project: filters.project, milestone: filters.milestone, state: filters.state ? (filters.state === "moving" ? "moving" : STATE_URL[filters.state]) : "",
    q: filters.q.trim(),
    ...(layout === "list" ? { sort: filters.sort || undefined, limit: PAGE } : { sort: "open" as const, limit: BOARD_LIMIT }),
  };
  const queryKey = JSON.stringify(query);
  const queryRef = React.useRef(query);
  queryRef.current = query;
  const keyRef = React.useRef(queryKey);
  keyRef.current = queryKey;
  const shownRef = React.useRef(0);

  const { data, error, reload } = usePagePolling<Loaded>(async () => {
    const q = { ...queryRef.current };
    // A poll asks again for everything the List shows, so "Show more" rows are not lost.
    if (q.limit === PAGE) q.limit = Math.min(BOARD_LIMIT, Math.max(PAGE, shownRef.current));
    const key = keyRef.current;
    return { key, resp: await (sample ? sampleLoad(sample, q) : factoryTickets(q)) };
  }, POLL_MS);

  // A new question (a filter, the search, the sort, List against Board) is a new read; the page keeps
  // showing the old answer, dimmed, until it comes.
  const lastKey = React.useRef(queryKey);
  React.useEffect(() => {
    if (lastKey.current === queryKey) return;
    lastKey.current = queryKey;
    reload();
  }, [queryKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (changes: Record<string, string | null>, replace = false) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    // Changing a filter does not move the person to another layout: the layout in view is kept in the
    // address when the new filters would open another one, and left out when they would open this one.
    if (!("layout" in changes) && !readPref()) {
      const filtered = ["project", "state", "milestone", "q"].some((k) => !!next.get(k));
      if (layout === (filtered ? "list" : "board")) next.delete("layout");
      else next.set("layout", layout);
    }
    setParams(next, { replace });
  };

  // The search box: typing waits a moment before it asks, and the address follows the box.
  const [qText, setQText] = React.useState(filters.q);
  const wrote = React.useRef(filters.q);
  React.useEffect(() => {
    if (filters.q !== wrote.current) {
      wrote.current = filters.q;
      setQText(filters.q);
    }
  }, [filters.q]);
  React.useEffect(() => {
    if (qText === wrote.current) return;
    const t = setTimeout(() => {
      wrote.current = qText;
      patch({ q: qText.trim() ? qText : null }, true);
    }, 300);
    return () => clearTimeout(t);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const pickLayout = (l: Layout) => {
    writePref(l);
    patch({ layout: l === (hasFilter ? "list" : "board") ? null : l });
  };
  const clear = () => {
    wrote.current = "";
    setQText("");
    patch({ project: null, state: null, milestone: null, q: null, sort: null });
  };

  // Rows past the first page of the List, and which cells and lanes the person opened or closed.
  const [tail, setTail] = React.useState<{ key: string; rows: TicketRow[]; more: boolean }>({ key: "", rows: [], more: false });
  const [moreState, setMoreState] = React.useState<"idle" | "busy" | "error">("idle");
  const [capsAt, setCapsAt] = React.useState<{ sig: string; caps: Record<string, number> }>({ sig: "", caps: {} });
  const [closed, setClosed] = React.useState<Record<string, boolean>>({});
  const sig = `${queryKey}|${layout}|${group}`;
  const caps = capsAt.sig === sig ? capsAt.caps : {};
  const bump = (id: string, cap: number) => setCapsAt({ sig, caps: { ...caps, [id]: (caps[id] ?? cap) + cap } });
  const flip = (id: string) => setClosed((c) => ({ ...c, [id]: !c[id] }));

  const resp = data?.resp;
  const fresh = !!data && data.key === queryKey;
  let rows: TicketRow[] = resp?.tickets ?? [];
  let more = !!resp?.more;
  if (layout === "list" && resp && tail.key === queryKey && tail.rows.length) {
    const have = new Set(rows.map(keyOf));
    const extra = tail.rows.filter((t) => !have.has(keyOf(t)));
    rows = [...rows, ...extra];
    if (extra.length) more = tail.more;
  }
  shownRef.current = layout === "list" && fresh ? rows.length : 0;

  const showMore = async () => {
    setMoreState("busy");
    const q = { ...queryRef.current, offset: rows.length, limit: PAGE };
    const key = queryKey;
    try {
      const r = await (sample ? sampleLoad(sample, q) : factoryTickets(q));
      setTail((t) => ({ key, rows: [...(t.key === key ? t.rows : []), ...r.tickets], more: r.more }));
      setMoreState("idle");
    } catch {
      setMoreState("error");
    }
  };

  const retry = (
    <Button variant="secondary" onClick={reload} data-testid="tickets-retry">Retry</Button>
  );

  if (!resp) {
    const failed = error && (
      <Banner tone="error" action={retry}>
        <b>We could not load the tickets.</b> <span className="text-foreground">Check your connection, then try again.</span>
      </Banner>
    );
    return (
      <div className="grid gap-5 md:gap-6" data-testid="factory-tickets">
        <Head />
        {failed || (
          <>
            <FilterBar layout={layout} filters={filters} qText={qText} group={group} data={undefined} disabled onLayout={pickLayout} patch={patch} setQText={setQText} />
            <SkeletonBody layout={layout} narrow={narrow} />
          </>
        )}
      </div>
    );
  }

  const projectsTotal = resp.projects.reduce((n, p) => n + p.count, 0);
  if (resp.projects.length === 0) {
    return (
      <div className="grid gap-5 md:gap-6" data-testid="factory-tickets">
        <Head />
        <div className="border border-border bg-card" data-testid="tickets-empty">
          <EmptyState title="No projects yet."
                      action={<Button asChild><Link to="/projects">Go to Projects</Link></Button>}>
            Tickets show here once the crew has a project to build. Start one from Projects.
          </EmptyState>
        </div>
      </div>
    );
  }

  const cols: StateKey[] = filters.state === "moving" ? MOVING_KEYS : filters.state ? [filters.state] : COLUMNS;
  const total = resp.total;
  const showing = Math.min(rows.length, total);
  let count: string;
  if (total === 0) count = "0 tickets match";
  else if (layout === "list") {
    count = hasFilter ? `${plural(total, "ticket matches", "tickets match")}${showing < total ? `, showing ${showing}` : ""}` : `Showing ${showing} of ${total} tickets`;
  } else count = hasFilter ? plural(total, "ticket matches", "tickets match") : plural(total, "ticket", "tickets");
  const hidden = resp.hidden_canceled;

  const body = () => {
    if (total === 0) {
      const nothingYet = !hasFilter && projectsTotal === 0;
      return (
        <div className="border border-border bg-card" data-testid="tickets-zero">
          <EmptyState title={nothingYet ? "No tickets yet." : `No ticket matches${filters.q ? ` "${filters.q}"` : " these filters"}.`}
                      action={nothingYet ? undefined : <Button variant="secondary" onClick={clear} data-testid="zero-clear">Clear filters</Button>}>
            {nothingYet ? "Tickets show here once the crew has planned a project." : `Try fewer words, or clear the filters to see all ${projectsTotal} tickets.`}
          </EmptyState>
        </div>
      );
    }
    if (layout === "list") {
      return (
        <div className="grid gap-0">
          <ListBody rows={rows} />
          {(more || moreState === "error") && (
            <div className="grid justify-items-center gap-2 border border-t-0 border-border bg-card px-4 py-3">
              {moreState === "error" && (
                <Banner tone="error" className="w-full"><b>More tickets did not load.</b> <span className="text-foreground">Try again.</span></Banner>
              )}
              <Button variant="secondary" busy={moreState === "busy"} onClick={showMore} data-testid="list-more">Show {PAGE} more</Button>
            </div>
          )}
        </div>
      );
    }
    const listOf = (k: StateKey) => {
      const next = new URLSearchParams(params);
      next.set("layout", "list");
      next.set("state", STATE_URL[k]);
      next.delete("group");
      return `?${next.toString()}`;
    };
    const p: BodyProps = { data: resp, rows, cols, group, narrow, caps, bump, closed, flip, listOf };
    return (
      <>
        {more && <Banner tone="note" className="mb-3" data-testid="tickets-capped">Showing every open ticket and the newest merged ones, {BOARD_LIMIT} in all. Narrow the filters, or use the List, to reach the rest.</Banner>}
        {layout === "board" ? <BoardBody {...p} /> : <LanesBody {...p} />}
      </>
    );
  };

  return (
    <div className="grid gap-5 md:gap-6" data-testid="factory-tickets">
      <Head sub={hasFilter ? "Every ticket of every project the crew holds" : `${plural(resp.counts.total, "ticket", "tickets")} in ${plural(resp.projects.length, "project", "projects")}`} />
      {error && (
        <Banner tone="error" action={retry}>
          <b>We could not load the tickets.</b> <span className="text-foreground">Check your connection, then try again.</span>
        </Banner>
      )}
      <div className="grid gap-4" aria-busy={!fresh}>
        <FilterBar layout={layout} filters={filters} qText={qText} group={group} data={resp} onLayout={pickLayout} patch={patch} setQText={setQText} />
        <div className="-mt-1.5 flex flex-wrap justify-between gap-x-3 gap-y-1 text-[13px] text-muted-foreground" data-testid="tickets-count-line">
          <span>
            <span data-testid="tickets-count">{count}.</span>
            {!filters.state && hidden > 0 && (
              <span data-testid="canceled-hint">{" "}{plural(hidden, "canceled ticket is", "canceled tickets are")} hidden.{" "}
                <button type="button" className={LINKISH} data-testid="show-canceled" onClick={() => patch({ state: "canceled" })}>Show</button>
              </span>
            )}
          </span>
          {hasFilter && <button type="button" className={LINKISH} data-testid="clear-filters" onClick={clear}>Clear filters</button>}
        </div>
        <div className={cn("min-w-0 transition-opacity", !fresh && "opacity-60")} data-testid="tickets-body" data-layout={layout}>{body()}</div>
      </div>
    </div>
  );
}

// The themed menu (Radix) in place of the browser's own. Radix keeps "" for "nothing chosen", so
// "All ..." travels as ALL and turns back into "" on the way out.
const ALL = "__all__";
type FilterOption = { value: string; label: string; user?: boolean };

function FilterSelect({ label, testid, value, options, onChange, disabled, title, className }: {
  label: string; testid: string; value: string; options: readonly FilterOption[]; onChange: (v: string) => void;
  disabled?: boolean; title?: string; className?: string;
}) {
  const wrap = (v: string) => (v === "" ? ALL : v);
  return (
    <Select value={wrap(value)} disabled={disabled} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectTrigger aria-label={label} data-testid={testid} title={title}
                     className={cn("w-full! md:w-auto! min-w-0 border-border! bg-[var(--input-bg)]! px-2.5! disabled:opacity-60!", className)}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={wrap(o.value)} data-value={o.value} {...(o.user ? { "data-user-text": "" } : {})}>{o.label}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function FilterBar({ layout, filters, qText, group, data, disabled, onLayout, patch, setQText }: {
  layout: Layout; filters: Filters; qText: string; group: Group; data: FactoryTicketsData | undefined; disabled?: boolean;
  onLayout: (l: Layout) => void; patch: (c: Record<string, string | null>, replace?: boolean) => void; setQText: (s: string) => void;
}) {
  const counts = data?.counts;
  const opt = (k: StateKey) => `${STATE_LABEL[k]}${counts ? ` (${counts[k]})` : ""}`;
  const noProject = !filters.project;
  return (
    <div role="search" className="grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-center" data-testid="tickets-filters">
      <div role="group" aria-label="Layout" className="col-span-full flex h-9 w-full flex-none border border-foreground bg-[var(--input-bg)] md:inline-flex md:w-auto">
        {LAYOUTS.map((l) => (
          <button key={l.value} type="button" aria-pressed={layout === l.value} disabled={disabled} data-testid={`layout-${l.value}`} onClick={() => onLayout(l.value)}
                  className={cn("h-full! flex-1 border-0! border-r! border-border! bg-transparent! px-3.5! py-0! text-[13px]! font-medium! text-foreground! last:border-r-0! hover:bg-[var(--row-hover)]! disabled:opacity-60 max-md:px-1.5!",
                    layout === l.value && "bg-foreground! text-background! hover:bg-foreground!")}>
            {l.label}
          </button>
        ))}
      </div>
      <input type="text" value={qText} disabled={disabled} onChange={(e) => setQText(e.target.value)} placeholder="Search by ref or title" aria-label="Search tickets"
             data-testid="f-q" className={cn(FIELD, "col-span-full md:min-w-[150px] md:flex-1 md:basis-[150px]")} />
      <FilterSelect label="Project" testid="f-project" value={filters.project} disabled={disabled} className="md:min-w-[128px]"
                    onChange={(v) => patch({ project: v || null, milestone: null })}
                    options={[{ value: "", label: "All projects" },
                      ...(data?.projects ?? []).map((p) => ({ value: p.id, label: `${p.name} (${p.count})`, user: true })),
                      ...(data && filters.project && !data.projects.some((p) => p.id === filters.project) ? [{ value: filters.project, label: filters.project, user: true }] : [])]} />
      <FilterSelect label="State" testid="f-state" value={filters.state} disabled={disabled} className="md:min-w-[128px]"
                    onChange={(v) => patch({ state: v || null })}
                    options={[{ value: "", label: "All states" }, { value: "moving", label: `Moving now${counts ? ` (${counts.moving})` : ""}` },
                      ...FILTER_STATES.map((k) => ({ value: STATE_URL[k], label: opt(k) }))]} />
      <FilterSelect label="Milestone" testid="f-milestone" value={filters.milestone} disabled={disabled || noProject} title={noProject ? "Choose a project first" : undefined}
                    className="md:min-w-[128px]" onChange={(v) => patch({ milestone: v || null })}
                    options={[{ value: "", label: "All milestones" },
                      ...(data?.milestones ?? []).filter((m) => m.project === filters.project).map((m) => ({ value: m.id, label: `${m.number} ${m.name}`, user: true }))]} />
      {layout === "list" && (
        <FilterSelect label="Sort" testid="f-sort" value={filters.sort || "new"} disabled={disabled} className="col-span-full md:col-auto md:ml-auto md:min-w-[190px]"
                      onChange={(v) => patch({ sort: v === "new" ? null : v })} options={SORTS} />
      )}
      {layout === "lanes" && (
        <FilterSelect label="Group by" testid="f-group" value={group} disabled={disabled} className="col-span-full md:col-auto md:ml-auto md:min-w-[190px]"
                      onChange={(v) => patch({ group: v === "project" ? null : v })} options={GROUPS} />
      )}
    </div>
  );
}

export default function FactoryTickets() {
  return <Tickets />;
}
