import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Banner, Button, EmptyState, Input, Loading, Skeleton } from "../../components/edition";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  KIND_GROUPS, sreTimeline, sreTimelineRaw,
  type KindGroup, type RangeKey, type TimelineData, type TimelineItem, type TimelineQuery, type TimelineRawRow, type TimelineTone,
} from "./Timeline.api";
import { isSample, sampleEarlier, sampleLoad, sampleOpen, sampleQuery, sampleRaw, SAMPLE_NOW } from "./sample/timeline";

// Timeline: what happened on the cell, newest first and grouped by the hour, from one read
// (GET /api/sre/timeline). The service, the range and the kinds live in the address (?service=,
// ?range=, ?kinds=, and ?from= and ?to= for a custom range), so a link from Home or a service page
// lands on the same list. A window or a folded quiet stretch has lines behind it; they are read
// only when a person opens them (GET /api/sre/timeline/raw) and shown in place. The list refreshes
// every 30 seconds and "Load earlier" asks for the page before the last one. Every clock the page
// shows is UTC, as the cell words it.
//
// An item's text is the cell's sentence around names people chose (a service, a repo, an alert, an
// incident title), so it is marked data-user-text or, where a person or an agent wrote the words
// (a commit, an incident title, an alert), data-words-exempt. The lines are raw output, exempt.

const POLL_MS = 30_000;
const FIRST_LINES = 20;
const MORE_LINES = 200;
const MAX_LINES = 1000;
const ALL = "__all";

const RANGES: { key: RangeKey; label: string; ms: number; words: string }[] = [
  { key: "1h", label: "1 hour", ms: 3_600_000, words: "Last hour" },
  { key: "6h", label: "6 hours", ms: 6 * 3_600_000, words: "Last 6 hours" },
  { key: "24h", label: "24 hours", ms: 24 * 3_600_000, words: "Last 24 hours" },
  { key: "7d", label: "7 days", ms: 7 * 24 * 3_600_000, words: "Last 7 days" },
  { key: "custom", label: "Custom", ms: 0, words: "" },
];
const KIND_LABEL: Record<KindGroup, string> = { logs: "Logs", metrics: "Metrics", alerts: "Alerts", deploys: "Deploys", checks: "Checks" };
const isRange = (v: string | null): v is RangeKey => RANGES.some((r) => r.key === v);

const LIVE_DEFAULT = { service: "", range: "6h" as RangeKey, kinds: "" };

// ── the words and the colours of an item
const TAG: Record<TimelineItem["kind"], string> = {
  window: "Logs", quiet: "Logs", deploy: "Deploy", alert: "Alert", check: "Check", incident: "Incident", metric: "Metric",
};
const TONE: Record<TimelineTone, string> = {
  bad: "bg-[var(--err-soft)] text-[color:var(--err)]",
  warn: "border-b border-dashed border-[color:var(--ink-soft)] bg-[var(--wash-2)] text-foreground",
  ok: "bg-[var(--ok-soft)] text-[color:var(--ok)]",
  info: "bg-[var(--wash-2)] text-foreground",
};
/** The text of these kinds is a person's or an agent's own words (a commit, a title, an alert). */
const WRITTEN = new Set<TimelineItem["kind"]>(["deploy", "incident", "alert"]);

// ── time, always UTC
const pad = (n: number) => String(n).padStart(2, "0");
const ms = (t: string) => Date.parse(t);
const clockSeconds = (t: string) => (Number.isNaN(Date.parse(t)) ? "" : new Date(t).toISOString().slice(11, 19));
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The hour an item sits under. A span that ends exactly on the hour belongs to the hour before. */
const hourOf = (it: TimelineItem): number | null => {
  const t = it.end ? ms(it.end) - 1 : ms(it.at);
  return Number.isFinite(t) ? Math.floor(t / HOUR) * HOUR : null;   // null: the cell sent a time that is not one
};
const hourWords = (hour: number | null, now: number) => {
  if (hour === null) return "Time unknown";
  const d = new Date(hour);
  const days = Math.floor(now / DAY) - Math.floor(hour / DAY);
  const clock = `${pad(d.getUTCHours())}:00`;
  if (days <= 0) return clock;
  if (days === 1) return `Yesterday ${clock}`;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${clock}`;
};

// ── the list: newest end first; at one moment a point before a span, then kind, then id, as the cell sorts
const RANK: Record<TimelineItem["kind"], number> = { incident: 0, alert: 1, window: 2, check: 3, deploy: 4, metric: 5, quiet: 6 };
const NO_TIME = Number.MIN_SAFE_INTEGER;   // an item with a bad time sorts last and is never cut by a range
const endOf = (it: TimelineItem) => { const t = ms(it.end ?? it.at); return Number.isNaN(t) ? NO_TIME : t; };
const order = (a: TimelineItem, b: TimelineItem) => endOf(b) - endOf(a) || Number(a.end !== null) - Number(b.end !== null) || RANK[a.kind] - RANK[b.kind] || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

/** `incoming` over `have` by id, newest first, without what is older than `from`. */
function merge(have: TimelineItem[], incoming: TimelineItem[], from: number | null): TimelineItem[] {
  const byId = new Map<string, TimelineItem>();
  for (const it of have) byId.set(it.id, it);
  for (const it of incoming) byId.set(it.id, it);
  return [...byId.values()].filter((it) => from === null || endOf(it) === NO_TIME || endOf(it) >= from).sort(order);
}

/** A poll's first page over what is held. The first page is replaced by what the cell sent, so an
 *  item the cell stopped sending (a quiet stretch whose start moved with the range) goes. With no
 *  way on, or no earlier page loaded yet, the first page is the whole list: the next "Load earlier"
 *  starts again from the new first page's cursor. Once earlier pages are loaded, what sorts strictly
 *  below the last item of the new first page stays, except an id the first page now carries. */
function mergePoll(have: TimelineItem[], fresh: TimelineItem[], hasMore: boolean, older: boolean, from: number | null): TimelineItem[] {
  const head = merge([], fresh, from);
  const last = head[head.length - 1];
  if (!hasMore || !older || !last) return head;
  const sent = new Set(head.map((it) => it.id));
  const keep = (it: TimelineItem) => (from === null || endOf(it) === NO_TIME || endOf(it) >= from) && !sent.has(it.id) && order(it, last) > 0;
  return [...head, ...have.filter(keep)].sort(order);
}

interface Acc {
  key: string;
  from: number | null;
  items: TimelineItem[];
  /** The `before` for the next older page; null when there is none. */
  cursor: string | null;
  /** An older page has been read, so a refresh keeps its cursor. */
  older: boolean;
  rangeWords: string;
  emptyWords: string | null;
}
interface Polled {
  key: string;
  from: number | null;
  data?: TimelineData;
}

type Lines = { status: "loading" | "ok" | "error"; limit: number; rows: TimelineRawRow[]; count: number; more: boolean };

const fieldsLine = (f: Record<string, unknown>) =>
  Object.entries(f ?? {}).map(([k, v]) => `${k}=${typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}`).join(" ");
const utc = (v: string) => (/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(v) ? `${v}:00Z` : v);

// ── small parts
const btnBase = "inline-flex! items-center! rounded-none! border! border-border! bg-[var(--white)]! dark:bg-background! text-[13px]! font-normal! text-muted-foreground! hover:border-border! hover:text-foreground!";
const btnOn = "font-semibold! text-foreground! bg-[var(--row-hover)]! dark:bg-[var(--row-hover)]! shadow-[inset_0_-2px_0_var(--accent)]";

function Tag({ item }: { item: TimelineItem }) {
  const tone = item.tone;
  return (
    <span data-testid="timeline-tag" data-tone={tone}
          className={cn("inline-block px-1.5 py-px font-mono text-[11px] font-medium uppercase tracking-[0.04em] whitespace-nowrap", TONE[tone])}>
      {TAG[item.kind]}
    </span>
  );
}

const LinkButton = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement>>(({ className, ...p }, ref) => (
  <button ref={ref} type="button" {...p}
          className={cn("linklike text-[13px]! text-foreground! underline! decoration-border! underline-offset-[3px] hover:opacity-100!", className)} />
));
LinkButton.displayName = "LinkButton";

function RawLines({ lines, onMore, onHide, onRetry }: {
  lines: Lines; onMore: () => void; onHide: () => void; onRetry: () => void;
}) {
  const waiting = lines.status === "loading";
  return (
    <div className="mt-2 min-w-0 border border-border bg-[var(--white)] dark:bg-background" data-testid="timeline-raw" aria-busy={waiting || undefined}>
      {waiting && lines.rows.length === 0 && (
        <div role="status" aria-label="Loading the lines" className="grid gap-2 px-3 py-2.5" data-testid="timeline-raw-loading">
          <Skeleton width="30%" /><Skeleton width="80%" /><Skeleton width="55%" />
        </div>
      )}
      {lines.rows.map((r, i) => {
        const f = fieldsLine(r.fields);
        return (
          <div key={`${r.at}-${i}`} data-words-exempt data-testid="timeline-raw-row"
               className="grid grid-cols-1 gap-x-3 gap-y-0.5 border-t border-border px-3 py-1.5 font-mono text-xs leading-[1.55] first:border-t-0 [overflow-wrap:anywhere] md:grid-cols-[70px_minmax(0,1fr)]">
            <i className="not-italic text-muted-foreground">{clockSeconds(r.at)}</i>
            <span className="min-w-0">
              {r.text}
              {f && <><br /><i className="not-italic text-muted-foreground">{f}</i></>}
            </span>
          </div>
        );
      })}
      {lines.status === "error" && (
        <Banner tone="warn" className="m-1.5" data-testid="timeline-raw-error"
                action={<Button variant="secondary" onClick={onRetry} data-testid="timeline-raw-retry" className="h-[30px]">Try again</Button>}>
          The lines did not load.
        </Banner>
      )}
      {lines.status === "ok" && (
        <div className="border-t border-border px-3 py-1.5 text-[13px] first:border-t-0" data-testid="timeline-raw-footer">
          <span data-testid="timeline-raw-count">
            {lines.more
              ? `Showing ${lines.rows.length.toLocaleString("en-US")} of ${lines.count.toLocaleString("en-US")} lines.`
              : lines.count === 1 ? "Showing the line." : `Showing all ${lines.count.toLocaleString("en-US")} lines.`}
          </span>
          {lines.more && lines.limit < MAX_LINES && <> <LinkButton onClick={onMore} data-testid="timeline-raw-more">Show {MORE_LINES} more</LinkButton></>}
          {" "}<LinkButton onClick={onHide} data-testid="timeline-raw-hide">Hide the lines</LinkButton>
        </div>
      )}
      {lines.status === "loading" && lines.rows.length > 0 && (
        <div className="border-t border-border px-3 py-1.5 text-[13px] text-muted-foreground" role="status">Loading more lines.</div>
      )}
    </div>
  );
}

function Row({ item, first, lines, onOpen, onMore, onHide }: {
  item: TimelineItem; first: boolean; lines?: Lines;
  onOpen: (it: TimelineItem, limit: number) => void; onMore: () => void; onHide: () => void;
}) {
  const count = item.raw?.count ?? 0;
  const written = WRITTEN.has(item.kind);
  const text = written ? <span data-words-exempt>{item.text}</span> : <span data-user-text>{item.text}</span>;
  return (
    <div className={cn("border-t border-border", first && "border-t-0")} data-testid="timeline-item" data-kind={item.kind} data-tone={item.tone} data-id={item.id}>
      <div className="grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-1.5 px-3.5 py-[11px] md:grid-cols-[110px_92px_minmax(0,1fr)] md:px-[18px]">
        <div className="pt-0.5 text-[13px] whitespace-nowrap text-muted-foreground" data-testid="timeline-time">{item.time_words}</div>
        <div className="justify-self-start pt-0.5"><Tag item={item} /></div>
        <div className="col-span-2 min-w-0 [overflow-wrap:anywhere] md:col-span-1" data-testid="timeline-text">
          {text}
          {item.link && (
            <small className="block text-[13px] text-muted-foreground">
              <Link to={item.link} data-testid="timeline-incident-link" className="text-foreground! underline!">Open the incident</Link>
            </small>
          )}
          {item.raw && !lines && (
            <small className="block text-[13px] text-muted-foreground">
              <LinkButton onClick={() => onOpen(item, FIRST_LINES)} data-testid="timeline-open-raw">
                {count === 1 ? "Show the line" : `Show the ${count.toLocaleString("en-US")} lines`}
              </LinkButton>
            </small>
          )}
        </div>
      </div>
      {lines && (
        <div className="px-3.5 pb-3 md:px-[18px]">
          <RawLines lines={lines} onMore={onMore} onHide={onHide} onRetry={() => onOpen(item, lines.limit)} />
        </div>
      )}
    </div>
  );
}

function TimelineSkeleton() {
  return (
    <Loading className="border border-border bg-card" data-testid="timeline-loading">
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div key={i} className={cn("grid gap-2 px-3.5 py-[11px] md:px-[18px]", i > 0 && "border-t border-border")}>
          <Skeleton width="40%" height={14} /><Skeleton width="40%" height={14} /><Skeleton width="70%" height={14} />
        </div>
      ))}
    </Loading>
  );
}

export default function Timeline() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params, setParams] = useSearchParams();

  // ── the filter bar, from the address; a sample starts from its own bar when the address is silent
  const dflt = sample ? sampleQuery(sample) : LIVE_DEFAULT;
  const service = params.has("service") ? params.get("service") ?? "" : dflt.service;
  const asked = params.get("range");
  // a from or a to in the address without a range is a custom range (a link made by hand)
  const range: RangeKey = isRange(asked) ? asked : params.has("from") || params.has("to") ? "custom" : dflt.range;
  const kindsAsked = params.has("kinds") ? params.get("kinds") ?? "" : dflt.kinds;
  const picked = KIND_GROUPS.filter((k) => kindsAsked.split(",").includes(k));
  const kinds = picked.length ? picked : KIND_GROUPS;
  const kindsCsv = kinds.length === KIND_GROUPS.length ? "" : kinds.join(",");
  const customFrom = params.get("from") ?? "";
  const customTo = params.get("to") ?? "";

  const put = (changes: Record<string, [value: string, dflt: string] | null>) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      for (const [k, v] of Object.entries(changes)) {
        if (v === null || v[0] === v[1]) n.delete(k); else n.set(k, v[0]);
      }
      return n;
    });
  const pickRange = (r: RangeKey) => put({ range: [r, dflt.range], ...(r === "custom" ? {} : { from: null, to: null }) });
  const pickService = (s: string) => put({ service: [s, dflt.service] });
  const toggleKind = (k: KindGroup) => {
    const on = kinds.includes(k);
    if (on && kinds.length === 1) return; // the last kind stays on
    const next = KIND_GROUPS.filter((x) => (x === k ? !on : kinds.includes(x)));
    put({ kinds: [next.length === KIND_GROUPS.length ? "" : next.join(","), dflt.kinds] });
  };

  // ── the read: one call for the first page, again every 30 seconds and when the bar changes
  const key = JSON.stringify([service, range, range === "custom" ? [customFrom, customTo] : "", kindsCsv]);
  const query = (): { q: TimelineQuery; fromMs: number | null } => {
    const q: TimelineQuery = {};
    if (service) q.service = service;
    if (kindsCsv) q.kinds = kindsCsv;
    let fromMs: number | null = null;
    if (range === "custom") {
      if (customFrom) { q.from = utc(customFrom); fromMs = ms(q.from); }
      if (customTo) q.to = utc(customTo);
    } else {
      fromMs = Date.now() - (RANGES.find((r) => r.key === range)?.ms ?? 0);
      q.from = new Date(fromMs).toISOString();
    }
    return { q, fromMs };
  };

  // a range that ends at a set time does not change: it is read once, and again only on a click
  const live = !(range === "custom" && customTo);
  const polled = usePagePolling<Polled>(async () => {
    const { q, fromMs } = query();
    const from = sample ? null : fromMs;
    try {
      return { key, from, data: sample ? await sampleLoad(sample) : await sreTimeline(q) };
    } catch {
      return { key, from };
    }
  }, live ? POLL_MS : 0);

  const [acc, setAcc] = React.useState<Acc | null>(null);
  const [failed, setFailed] = React.useState<string | null>(null); // the key whose read failed
  const [services, setServices] = React.useState<string[]>([]);
  const res = polled.data;
  React.useEffect(() => {
    if (!res) return;
    if (!res.data) { setFailed(res.key); return; }
    const d = res.data;
    setFailed(null);
    setServices(d.services);
    const more = d.next_before !== null;
    setAcc((prev) => prev && prev.key === res.key
      ? { ...prev, from: res.from, items: mergePoll(prev.items, d.items, more, prev.older, res.from),
          cursor: more && prev.older ? prev.cursor : d.next_before, older: more && prev.older,
          rangeWords: d.range_words, emptyWords: d.empty_words }
      : { key: res.key, from: res.from, items: merge([], d.items, res.from), cursor: d.next_before, older: false,
          rangeWords: d.range_words, emptyWords: d.empty_words });
  }, [res]);

  // a changed bar reads again at once; the first load is the hook's own
  // (when the bar also flips between a live and a fixed range, the hook starts over and reads by itself)
  const seen = React.useRef(key);
  const wasLive = React.useRef(live);
  React.useEffect(() => {
    if (seen.current === key) return;
    seen.current = key;
    const flipped = wasLive.current !== live;
    wasLive.current = live;
    if (!flipped) polled.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const fresh = acc && acc.key === key ? acc : null;
  const showError = failed === key;

  // ── earlier items
  const [earlier, setEarlier] = React.useState<{ busy: boolean; error: boolean }>({ busy: false, error: false });
  React.useEffect(() => setEarlier({ busy: false, error: false }), [key]);
  const loadEarlier = async () => {
    if (!fresh?.cursor || earlier.busy) return;
    const at = key;
    const before = fresh.cursor;
    setEarlier({ busy: true, error: false });
    try {
      const { q } = query();
      const d = sample ? await sampleEarlier() : await sreTimeline({ ...q, before });
      setAcc((a) => a && a.key === at ? { ...a, items: merge(a.items, d.items, a.from), cursor: d.next_before, older: true } : a);
      setEarlier({ busy: false, error: false });
    } catch {
      setEarlier({ busy: false, error: true });
    }
  };

  // ── the lines behind a window, read when a person opens them
  const [lines, setLines] = React.useState<Record<string, Lines>>({});
  const openLines = async (it: TimelineItem, limit: number) => {
    if (!it.raw) return;
    const id = it.id;
    setLines((l) => ({ ...l, [id]: { status: "loading", limit, rows: l[id]?.rows ?? [], count: it.raw?.count ?? 0, more: false } }));
    try {
      const d = sample ? await sampleRaw(sample, limit)
        : await sreTimelineRaw({ service: it.service || undefined, from: it.raw.from, to: it.raw.to, limit });
      setLines((l) => ({ ...l, [id]: { status: "ok", limit, rows: d.rows, count: d.count, more: d.more } }));
    } catch {
      setLines((l) => ({ ...l, [id]: { status: "error", limit, rows: l[id]?.rows ?? [], count: it.raw?.count ?? 0, more: false } }));
    }
  };
  const hideLines = (id: string) => setLines((l) => { const n = { ...l }; delete n[id]; return n; });

  // a sample can start with the lines of one window open
  const opened = React.useRef(false);
  React.useEffect(() => {
    if (!sample || opened.current || !fresh) return;
    const id = sampleOpen(sample);
    const it = id ? fresh.items.find((i) => i.id === id) : undefined;
    if (!it) return;
    opened.current = true;
    void openLines(it, FIRST_LINES);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fresh, sample]);

  // ── custom range: two times in UTC, applied together
  const [draft, setDraft] = React.useState({ from: customFrom.slice(0, 16), to: customTo.slice(0, 16) });
  React.useEffect(() => setDraft({ from: customFrom.slice(0, 16), to: customTo.slice(0, 16) }), [customFrom, customTo]);
  const span = draft.from && draft.to ? ms(utc(draft.to)) - ms(utc(draft.from)) : NaN;
  const draftOk = span > 0 && span <= 31 * DAY;

  // ── the page
  const now = sample ? Date.parse(SAMPLE_NOW) : Date.now();
  const rangeWords = range === "custom" ? fresh?.rangeWords : RANGES.find((r) => r.key === range)?.words;
  const items = fresh?.items ?? [];

  // one heading per hour, newest first. A point on the hour sorts before a span that ends on it (the span
  // belongs to the hour before), so the headings follow the list; an hour is still found by its number.
  const groups: { hour: number | null; items: TimelineItem[] }[] = [];
  for (const it of items) {
    const h = hourOf(it);
    const have = groups.find((g) => g.hour === h);
    if (have) have.items.push(it); else groups.push({ hour: h, items: [it] });
  }
  groups.sort((a, b) => (b.hour ?? NO_TIME) - (a.hour ?? NO_TIME));

  const serviceOptions = [...new Set([...services, ...(service ? [service] : [])])];
  const narrowed = kinds.length < KIND_GROUPS.length;

  return (
    <div className="grid gap-[18px]" data-testid="sre-timeline">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Timeline</h1>
        <p className="m-0! min-h-[1.5em] text-[15px] text-muted-foreground" data-testid="timeline-sub">
          {fresh && rangeWords ? <>{rangeWords}, {service ? <span data-user-text>{service}</span> : "all services"}.</> : "\u00a0"}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2.5">
        <Select value={service || ALL} onValueChange={(v) => pickService(v === ALL ? "" : v)}>
          <SelectTrigger aria-label="Service" data-testid="timeline-service"
                         className="h-9 w-auto! max-w-full min-w-0 justify-start! gap-2! bg-[var(--white)]! dark:bg-background!">
            <span className="text-muted-foreground">Service</span>
            <span data-user-text className="truncate"><SelectValue /></span>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All services</SelectItem>
            {serviceOptions.map((s) => <SelectItem key={s} value={s}><span data-user-text>{s}</span></SelectItem>)}
          </SelectContent>
        </Select>
        <div role="group" aria-label="Range" className="inline-flex w-full border border-border bg-[var(--white)] dark:bg-background md:w-auto" data-testid="timeline-range">
          {RANGES.map((r) => (
            <button key={r.key} type="button" aria-pressed={range === r.key} data-testid={`timeline-range-${r.key}`} onClick={() => pickRange(r.key)}
                    className={cn("flex-1 cursor-pointer rounded-none! border-y-0! border-r-0! border-l! border-border! bg-transparent! px-2! py-1.5! text-sm! font-normal! text-muted-foreground! first:border-l-0! hover:border-border! hover:text-foreground! md:flex-none md:px-3!",
                                  range === r.key && btnOn)}>
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {range === "custom" && (
        <div className="flex flex-wrap items-end gap-2.5" data-testid="timeline-custom">
          <Input label="From (UTC)" type="datetime-local" value={draft.from} data-testid="timeline-custom-from"
                 onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))} className="h-9 bg-[var(--white)]! dark:bg-background!" />
          <Input label="To (UTC)" type="datetime-local" value={draft.to} data-testid="timeline-custom-to"
                 onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))} className="h-9 bg-[var(--white)]! dark:bg-background!" />
          <Button variant="secondary" disabled={!draftOk} data-testid="timeline-custom-apply" className="h-9"
                  onClick={() => put({ from: [draft.from, ""], to: [draft.to, ""] })}>
            Apply
          </Button>
          {!draftOk && draft.from && draft.to && <span className="text-[13px] text-muted-foreground">Pick a start before the end, up to 31 days.</span>}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2.5" role="group" aria-label="Kinds" data-testid="timeline-kinds">
        {KIND_GROUPS.map((k) => {
          const on = kinds.includes(k);
          const last = on && kinds.length === 1;
          return (
            <button key={k} type="button" aria-pressed={on} aria-disabled={last || undefined} data-testid={`timeline-kind-${k}`}
                    title={last ? "At least one kind stays on" : undefined} onClick={() => toggleKind(k)}
                    className={cn(btnBase, "h-[30px] cursor-pointer gap-1.5! px-2.5! py-0!", on && btnOn, last && "cursor-default")}>
              <b aria-hidden className={cn("inline-block size-2 bg-foreground", !on && "opacity-25")} />
              {KIND_LABEL[k]}
            </button>
          );
        })}
      </div>

      {showError && (
        <Banner tone="error" data-testid="timeline-error"
                action={<Button variant="secondary" onClick={polled.reload} data-testid="timeline-retry" className="h-[30px]">Try now</Button>}>
          <b>The timeline did not load.</b> <span className="text-foreground">Tares could not read it just now.{live && " It will try again in 30 seconds."}</span>
        </Banner>
      )}

      {!fresh && !showError && <TimelineSkeleton />}

      {fresh && items.length === 0 && (
        <EmptyState title={narrowed ? "Nothing to show" : "Nothing arrived"} data-testid="timeline-empty"
                    className="border border-dashed border-[color:var(--border-strong)]">
          <span data-user-text>{fresh.emptyWords ?? "Nothing to show."}</span>{" "}
          {service ? "Try a longer range or all services." : "Try a longer range."}
        </EmptyState>
      )}

      {fresh && items.length > 0 && (
        <div className="border border-border bg-card" data-testid="timeline-list">
          {groups.map((g, gi) => (
            <React.Fragment key={g.hour ?? "unknown"}>
              <div role="heading" aria-level={2} data-testid="timeline-hour"
                   className={cn("border-y border-border bg-[var(--row-hover)] px-3.5 py-2 text-[13px] font-semibold text-muted-foreground md:px-[18px]", gi === 0 && "border-t-0")}>
                {hourWords(g.hour, now)}
              </div>
              {g.items.map((it, i) => (
                <Row key={it.id} item={it} first={i === 0} lines={lines[it.id]} onOpen={openLines}
                     onMore={() => openLines(it, Math.min((lines[it.id]?.limit ?? FIRST_LINES) + MORE_LINES, MAX_LINES))}
                     onHide={() => hideLines(it.id)} />
              ))}
            </React.Fragment>
          ))}
          {fresh.cursor && (
            <div className="grid justify-items-start gap-2 border-t border-border px-3.5 py-3 md:px-[18px]">
              {earlier.error && (
                <Banner tone="warn" className="w-full" data-testid="timeline-earlier-error">The earlier items did not load.</Banner>
              )}
              <Button variant="secondary" busy={earlier.busy} onClick={loadEarlier} data-testid="timeline-earlier" className="h-9">
                {earlier.error ? "Try again" : "Load earlier"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
