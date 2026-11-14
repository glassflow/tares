import * as React from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import {
  Banner, Button, EmptyState, LineChart, ListRow, ListSection, Panel, Select, Skeleton, SkeletonRows, Toaster, useToast,
} from "../../components/edition";
import { Tabs, TabsList, TabsTrigger } from "../../components/ui/tabs";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import {
  incidentAction, sreIncident, sreIncidents,
  type GroupState, type IncidentAction, type IncidentData, type IncidentItem, type IncidentsData, type WorthAction,
} from "./Incidents.api";
import { ageWords, clock, dateWords, durationWords, sentence, sinceWords, spanWords, stamp } from "./incidentTime";
import {
  isIncidentSample, isListSample, sampleAct, sampleIncident, sampleIncidents, SAMPLE_ME, SAMPLE_NOW,
} from "./sample/incidents";

// Incidents (TR-751): the list of incidents by state (GET /api/sre/incidents) and one incident
// (GET /api/sre/incidents/{id}). Each screen is one read, polled every 20 seconds while the tab is
// visible. A change (I'm on it, resolve, reopen, worth) answers the incident as it is now and the
// page shows that answer at once. The note, the evidence, a title and a Slack channel are written by
// the agent or by people, so they are React text nodes marked data-words-exempt or data-user-text.

const POLL_MS = 20_000;
const NOT_SAVED = "That did not save. Try again in a moment.";
const READ_ONLY = "That did not save. Your key can read but not change incidents.";
const ALL_SERVICES = "__all__";
const PAGE = 20;

/** The name the cell records for the person looking: the person behind the key, else the key's
 *  name, or "console" on a cell with no sign-in and nobody to name. */
function useMe(): string {
  const { open, credentialName, personName } = useEdition();
  return personName ?? (open ? "console" : credentialName ?? "");
}

const now = (sample: string | null) => (sample ? SAMPLE_NOW : Date.now());

const H1 = "m-0! mb-1.5! text-[22px]! md:text-[28px]!";
const SUB = "m-0 max-w-[640px] text-[15px] text-muted-foreground";
const LINK_BTN = "cursor-pointer border-0! bg-transparent! p-0! text-[13px]! text-foreground! underline!";

// ── the list ─────────────────────────────────────────────────────────────────

type Tab = "all" | "open" | "on_it" | "resolved";
const TAB_STATES: Tab[] = ["all", "open", "on_it", "resolved"];

function subline(c: IncidentsData["counts"], tab: Tab): string {
  const parts: [Tab, number, string][] = [["open", c.open, "open"], ["on_it", c.on_it, "someone on it"], ["resolved", c.resolved, "resolved lately"]];
  const used = parts.filter(([k, n]) => n > 0 && (tab === "all" || tab === k));
  if (used.length === 0) return tab === "all" ? "Nothing yet." : "None in this state.";
  return `${used.map(([, n, w]) => `${n} ${w}`).join(", ")}.`;
}

function Row({ it, at }: { it: IncidentItem; at: number }) {
  const words = it.state === "raised" ? "Raised once, did not repeat" : sentence(it.state_words);
  const when = stamp(Date.parse(it.opened_at), at, it.when_words);
  const body = (
    <>
      <div className="min-w-0 flex-1 basis-[240px]">
        <div className="font-bold text-foreground" data-words-exempt data-testid="incident-title">{it.title}</div>
        <div className="text-[13px] text-muted-foreground" data-testid="incident-words">{words}</div>
      </div>
      <div className="shrink-0 self-start whitespace-nowrap text-[13px] text-muted-foreground" data-testid="incident-when">{when}</div>
    </>
  );
  const attrs = { "data-testid": "incident-row", "data-id": it.id, "data-state": it.state };
  if (!it.incident) return <ListRow {...attrs}>{body}</ListRow>;
  return <ListRow to={`/incidents/${encodeURIComponent(it.id)}`} {...attrs}>{body}</ListRow>;
}

interface More { key: string; base: string | null; items: IncidentItem[]; next: string | null }

export default function Incidents() {
  const raw = useSample();
  const sample = isListSample(raw) ? raw : null;
  const [params, setParams] = useSearchParams();
  const { toast } = useToast();
  const tabParam = params.get("state");
  const tab: Tab = TAB_STATES.includes(tabParam as Tab) ? (tabParam as Tab) : "all";
  const service = params.get("service") ?? "";
  const showAll = params.get("show") === "all";
  const key = `${sample ?? "live"}|${service}|${showAll}`;

  const { data: got, error, reload } = usePagePolling(async () => {
    const q = { service: service || undefined, show: showAll ? ("all" as const) : undefined };
    return { key, d: await (sample ? sampleIncidents(sample, q) : sreIncidents(q)) };
  }, POLL_MS);
  // A change of service or of "all findings" asks again at once; the older answer is dimmed meanwhile.
  const first = React.useRef(true);
  React.useEffect(() => {
    if (first.current) { first.current = false; return; }
    reload();
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const data = got?.d;
  const stale = got !== undefined && got.key !== key;

  const [more, setMore] = React.useState<More | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const extra = more && data && more.key === key && more.base === data.next_before ? more : null;

  const setParam = (name: string, value: string | null) => {
    const p = new URLSearchParams(params);
    if (value) p.set(name, value); else p.delete(name);
    setParams(p, { replace: true });
  };

  async function showMore() {
    if (!data || loadingMore) return;
    const before = extra ? extra.next : data.next_before;
    if (!before) return;
    setLoadingMore(true);
    try {
      const q = { service: service || undefined, before };
      const page = await (sample ? sampleIncidents(sample, q) : sreIncidents(q));
      const items = page.groups.find((g) => g.state === "resolved")?.items ?? [];
      setMore({ key, base: data.next_before, items: [...(extra?.items ?? []), ...items], next: page.next_before });
    } catch {
      toast("The next 20 did not load. Try again in a moment.");
    } finally {
      setLoadingMore(false);
    }
  }

  if (error && !data) {
    return (
      <div className="grid gap-6 md:gap-8" data-testid="incidents-page">
        <div><h1 className={H1}>Incidents</h1><p className={SUB}>&nbsp;</p></div>
        <Banner tone="error" data-testid="incidents-error"
                action={<Button variant="secondary" onClick={reload} data-testid="incidents-retry">Try now</Button>}>
          <b className="block">The incidents did not load.</b>
          Tares could not read them just now. It will try again in 20 seconds.
        </Banner>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="grid gap-6 md:gap-8" data-testid="incidents-page" data-loading>
        <div><h1 className={H1}>Incidents</h1><p className={SUB}>&nbsp;</p></div>
        <div className="border border-border bg-card"><SkeletonRows count={5} /></div>
      </div>
    );
  }

  const at = now(sample);
  const c = data.counts;
  const total = c.open + c.on_it + c.resolved;
  const raisedItems = data.groups.find((g) => g.state === "raised")?.items ?? [];
  const nothing = total === 0 && !service && raisedItems.length === 0;
  const groups = data.groups
    .map((g) => {
      const items = g.state === "resolved" && extra ? [...g.items, ...extra.items.filter((x) => !g.items.some((y) => y.id === x.id))] : g.items;
      return { ...g, items };
    })
    .filter((g) => g.items.length > 0 && (tab === "all" || g.state === tab));
  const next = extra ? extra.next : data.next_before;
  const count = (t: Tab) => (t === "all" ? total : c[t]);
  const services = [{ value: ALL_SERVICES, label: "All services" }, ...[...new Set([...data.services, ...(service ? [service] : [])])].map((s) => ({ value: s, label: s }))];

  // The way to the raised findings, and back, does not depend on there being quiet checks to count.
  const quietLine = (
    <p className="m-0 text-[13px] text-muted-foreground" data-testid="quiet-line">
      {data.quiet_checks_words && <><span data-testid="quiet-words">{data.quiet_checks_words}.</span>{" "}</>}
      <button type="button" className={LINK_BTN} data-testid="show-all-findings"
              onClick={() => setParam("show", showAll ? null : "all")}>
        {showAll ? "Show incidents only" : "Show all findings"}
      </button>
    </p>
  );

  return (
    <div className="grid gap-6 md:gap-8" data-testid="incidents-page">
      <div>
        <h1 className={H1}>Incidents</h1>
        <p className={SUB} data-testid="incidents-sub">{nothing ? "Nothing yet." : subline(c, tab)}</p>
      </div>

      {nothing ? (
        <div className="border border-dashed border-border bg-card" data-testid="incidents-empty">
          <EmptyState title="No incidents">The watcher has raised nothing that needed a note.</EmptyState>
          <div className="border-t border-border px-[18px] py-3">{quietLine}</div>
        </div>
      ) : (
        <div className={cn("grid gap-4 md:gap-5", stale && "opacity-60")} aria-busy={stale || undefined}>
          <div className="flex flex-wrap items-center gap-2.5">
            <Tabs value={tab} onValueChange={(v) => setParam("state", v === "all" ? null : v)} className="min-w-0 max-w-full">
              <TabsList aria-label="Incidents by state" className="h-auto w-auto max-w-full flex-wrap gap-0 border border-border bg-card">
                {TAB_STATES.map((t) => (
                  <TabsTrigger key={t} value={t} data-testid={`tab-${t}`}
                               className="border-r! border-r-border! px-3! max-sm:px-2! max-sm:text-[13px]! last:border-r-0! data-[state=active]:border-b-[color:var(--accent)]! data-[state=active]:hover:border-b-[color:var(--accent)]!">
                    {{ all: "All", open: "Open", on_it: "Someone on it", resolved: "Resolved" }[t]}
                    <b className="ml-1 font-mono text-xs font-medium" data-testid={`tab-count-${t}`}>{count(t)}</b>
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <div className="border border-border bg-card px-3 [&>div]:flex! [&>div]:items-center [&>div]:gap-2 [&_label]:text-sm! [&_label]:font-normal! [&_label]:text-muted-foreground!">
              <Select label="Service" data-testid="service-filter" options={services} value={service || ALL_SERVICES}
                      onValueChange={(v) => setParam("service", v === ALL_SERVICES ? null : v)}
                      className="h-9 min-w-[130px] border-0! bg-transparent! px-0! shadow-none!" />
            </div>
          </div>

          {groups.length === 0 ? (
            <div className="border border-border bg-card" data-testid="incidents-none">
              <EmptyState title="Nothing here">No incident matches this filter.</EmptyState>
            </div>
          ) : (
            groups.map((g) => (
              <ListSection key={g.state} title={g.label} aside={g.state === "resolved" ? c.resolved : g.items.length}
                           data-testid={`group-${g.state}`}>
                {g.items.map((it) => <Row key={it.id} it={it} at={at} />)}
                {g.state === "resolved" && next && (tab === "all" || tab === "resolved") && (
                  <div className="border-t border-border px-[18px] py-3">
                    <Button variant="secondary" busy={loadingMore} onClick={showMore} data-testid="show-more">Show {PAGE} more</Button>
                  </div>
                )}
              </ListSection>
            ))
          )}

          {quietLine}
        </div>
      )}
      <Toaster />
    </div>
  );
}

// ── one incident ─────────────────────────────────────────────────────────────

const GROUP_TAG: Record<GroupState, { label: string; cls: string }> = {
  open: { label: "Open", cls: "border-[color:var(--danger-border)] bg-[var(--err-soft)] text-[color:var(--err)]" },
  on_it: { label: "Someone on it", cls: "border-[color:var(--warn-border)] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]" },
  resolved: { label: "Resolved", cls: "border-[color:var(--ok)] bg-[var(--ok-soft)] text-[color:var(--ok)]" },
  raised: { label: "Raised", cls: "border-border bg-transparent text-foreground" },
};

function Back() {
  return <Link to="/incidents" data-testid="back" className="w-fit text-[13px] text-muted-foreground! no-underline! hover:text-foreground!">&larr; Incidents</Link>;
}

/** The status sentence under the title. */
function statusOf(d: IncidentData, me: string, at: number): string {
  const opened = Date.parse(d.opened_at);
  if (d.state === "open") return `Open, nobody on it yet. It has waited ${spanWords(at - opened)}.`;
  if (d.state === "on_it") {
    const who = d.who;
    const mine = !!who && !!me && who.name === me;
    const since = sinceWords(Date.parse(who?.since ?? d.opened_at), at);
    return `${mine ? "You are" : `${who?.name ?? "Someone"} is`} on it${since ? ` since ${since}` : ""}.`;
  }
  const r = d.resolved;
  if (!r) return "Resolved.";
  const rAt = Date.parse(r.at);
  if (r.words.startsWith("Resolved itself")) return `${r.words}. Nobody had to act.`;
  if (r.words.startsWith("Closed")) return `${r.words}.`;
  const by = r.by && me && r.by === me ? "Resolved by you" : r.words;
  const after = rAt > opened ? `, after ${durationWords(rAt - opened)}` : "";
  return `${by}, ${ageWords(at - rAt)}${after}.`;
}

/** "Who was on it". */
function whoLine(d: IncidentData, me: string, at: number): string {
  const you = (n: string | null | undefined) => (n && me && n === me ? "You" : n ?? "Someone");
  if (d.state === "on_it" && d.who) {
    const since = sinceWords(Date.parse(d.who.since), at);
    return `${you(d.who.name)}${since ? `, since ${since}` : ""}.`;
  }
  if (d.state === "resolved" && d.resolved?.by) return `${you(d.resolved.by)} resolved it.`;
  return d.state === "open" ? "Nobody yet." : "Nobody was on it.";
}

interface Block { heading: string | null; text: string }

/** The note as headings and paragraphs: "## Heading" lines start a block, a blank line splits paragraphs. */
function blocksOf(d: IncidentData): Block[] {
  const parts: Block[] = [
    { heading: "What is failing", text: d.failing ?? "" },
    { heading: "Likely cause", text: d.likely_cause ?? "" },
    { heading: "Next step", text: d.next_step ?? "" },
  ].filter((b) => b.text.trim());
  if (parts.length > 0 && d.note.length <= 1200) return parts;
  const out: Block[] = [];
  let heading: string | null = null;
  for (const chunk of d.note.split(/\n\s*\n/)) {
    const lines = chunk.split("\n");
    let rest = chunk.trim();
    if (/^#{1,6}\s/.test(lines[0])) {
      heading = lines[0].replace(/^#{1,6}\s+/, "").trim();
      rest = lines.slice(1).join("\n").trim();
      out.push({ heading, text: rest });
      heading = null;
    } else if (rest) {
      out.push({ heading: null, text: rest });
    }
  }
  return out.length > 0 ? out : parts;
}

const CLIP = 260;
const CLIP_SLACK = 120;   // a note only a little over the cut is shown whole: a button should not hide two lines

function Note({ d }: { d: IncidentData }) {
  const blocks = React.useMemo(() => blocksOf(d), [d]);
  const inner = React.useRef<HTMLDivElement>(null);
  const [tall, setTall] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  React.useLayoutEffect(() => {
    const el = inner.current;
    if (!el) return;
    const measure = () => setTall(el.offsetHeight > CLIP + CLIP_SLACK);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [blocks]);
  if (blocks.length === 0) return null;
  const clipped = tall && !open;
  return (
    <div className="grid gap-3">
      <Panel title="The note" data-testid="note" data-clipped={clipped || undefined}>
        <div className="relative" style={clipped ? { maxHeight: CLIP, overflow: "hidden" } : undefined} data-testid="note-body">
          <div ref={inner} className="grid gap-3 text-[15px]" data-words-exempt>
            {blocks.map((b, i) => (
              <div key={i}>
                {b.heading && <h4 className="m-0! mb-1.5! text-[14px]! font-bold!">{b.heading}</h4>}
                {b.text.split(/\n\s*\n/).map((p, j) => <p key={j} className="m-0 mb-2 whitespace-pre-line last:mb-0">{p}</p>)}
              </div>
            ))}
          </div>
          {clipped && <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-card to-transparent" data-testid="note-fade" />}
        </div>
      </Panel>
      {tall && (
        <div>
          <Button variant="secondary" onClick={() => setOpen((v) => !v)} aria-expanded={open} data-testid="note-toggle">
            {open ? "Show less" : "Read the whole note"}
          </Button>
        </div>
      )}
    </div>
  );
}

const SHOWN_EVIDENCE = 5;

function Evidence({ d }: { d: IncidentData }) {
  const [all, setAll] = React.useState(false);
  const rows = [...d.evidence].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  if (rows.length === 0) return null;
  const shown = all ? rows : rows.slice(0, SHOWN_EVIDENCE);
  const hidden = rows.length - SHOWN_EVIDENCE;
  return (
    <section data-testid="evidence">
      <h2 className="m-0! mb-2! text-[15px]!">What it read</h2>
      <div className="border border-border bg-card">
        {shown.map((e, i) => (
          <div key={`${e.at}-${i}`} className="min-w-0 border-t border-border px-[18px] py-3 text-sm [overflow-wrap:anywhere] first:border-t-0" data-testid="evidence-row">
            <span className="mr-1.5 font-mono text-muted-foreground">{clock(Date.parse(e.at))}</span>
            <span data-words-exempt>{e.text}</span>
          </div>
        ))}
        {hidden > 0 && (
          <div className="border-t border-border px-[18px] py-3">
            <button type="button" className={LINK_BTN} onClick={() => setAll((v) => !v)} aria-expanded={all} data-testid="evidence-toggle">
              {all ? "Show fewer" : `Show ${hidden} more`}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function SmallPanel({ title, children, ...props }: { title: string; children: React.ReactNode } & React.HTMLAttributes<HTMLElement>) {
  return (
    <section className="min-w-0 border border-border bg-card px-[18px] py-3.5 [overflow-wrap:anywhere]" {...props}>
      <h3 className="m-0! text-[14px]!">{title}</h3>
      {children}
    </section>
  );
}

function IncidentView({ id }: { id: string }) {
  const raw = useSample();
  const sample = isIncidentSample(raw) ? raw : null;
  const live = useMe();
  const me = sample ? SAMPLE_ME : live;   // a sample is seen by its own person, whatever cell it sits on
  const { toast } = useToast();
  const { data: got, error, reload } = usePagePolling<IncidentData | "missing">(async () => {
    try {
      return await (sample ? sampleIncident(sample) : sreIncident(id));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return "missing";
      throw e;
    }
  }, POLL_MS);

  const [override, setOverride] = React.useState<IncidentData | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [changing, setChanging] = React.useState(false);
  // On a live cell the next read is the truth again; a sample has no cell, so its answer stays.
  React.useEffect(() => { if (!sample) setOverride(null); }, [got, sample]);

  const d = override ?? (got && got !== "missing" ? got : null);

  async function act(a: IncidentAction | WorthAction) {
    if (busy || !d) return;
    setBusy(a);
    try {
      const next = await (sample ? sampleAct(sample, d, a) : incidentAction(id, a));
      setOverride(next);
      setChanging(false);
      if (!sample) reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // The incident is not in a state that allows this: say so and show it as it is now.
        toast(e.message);
        setChanging(false);
        if (!sample) reload();
      } else {
        toast(e instanceof ApiError && e.status === 403 ? READ_ONLY : NOT_SAVED);
      }
    } finally {
      setBusy(null);
    }
  }

  const frame = (children: React.ReactNode) => (
    <div className="grid gap-6 md:gap-8" data-testid="incident-page">
      <Back />
      {children}
      <Toaster />
    </div>
  );

  if (got === "missing" && !override) {
    return frame(
      <div className="border border-border bg-card" data-testid="incident-missing">
        <EmptyState title="No incident with that id" action={<Button variant="secondary" asChild><Link to="/incidents">Back to all incidents</Link></Button>}>
          It may have been removed.
        </EmptyState>
      </div>,
    );
  }
  if (error && !d) {
    return frame(
      <Banner tone="error" data-testid="incident-error"
              action={<Button variant="secondary" onClick={reload} data-testid="incident-retry">Try now</Button>}>
        <b className="block">This incident did not load.</b>
        It will try again in 20 seconds.
      </Banner>,
    );
  }
  if (!d) {
    return frame(
      <div className="grid gap-4" data-testid="incident-loading" role="status" aria-busy="true" aria-label="Loading">
        <Skeleton width={220} height={14} />
        <Skeleton width="62%" height={28} />
        <Skeleton height={64} />
        <Skeleton height={180} />
      </div>,
    );
  }

  const at = now(sample);
  const opened = Date.parse(d.opened_at);
  const tag = GROUP_TAG[d.state];
  const mine = !!d.who && !!me && d.who.name === me;
  const can = (a: IncidentAction) => d.actions.includes(a);
  const slackUrl = d.slack?.url && /^https?:\/\//i.test(d.slack.url) ? d.slack.url : null;
  const worth = d.worth;
  const askWorth = worth.can_answer && (worth.answer === null || changing);
  const traffic = d.chart.traffic ?? [];
  const series = [{ label: "errors", points: d.chart.points }, ...(traffic.length > 0 ? [{ label: "traffic", points: traffic }] : [])];
  const openedWords = stamp(opened, at, dateWords(opened));
  const btn = (a: IncidentAction, label: string, variant: "primary" | "secondary") => (
    <Button variant={variant} busy={busy === a} disabled={busy !== null && busy !== a} onClick={() => act(a)} data-testid={`act-${a}`}>{label}</Button>
  );

  return frame(
    <>
      <header className="grid gap-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("border px-1.5 py-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.06em]", tag.cls)} data-testid="incident-tag">{tag.label}</span>
          <span className="border border-border bg-card px-2 py-0.5 text-xs" data-user-text data-testid="incident-service">{d.service}</span>
          {openedWords && <span className="text-[13px] text-muted-foreground" data-testid="incident-opened">opened {openedWords}</span>}
        </div>
        <h1 className="m-0! text-[22px]! [overflow-wrap:anywhere] md:text-[28px]!" data-words-exempt data-testid="incident-title">{d.title}</h1>
      </header>

      <section className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border border-border bg-card px-[18px] py-3.5" data-testid="status">
        <p className="m-0 min-w-0 font-bold [overflow-wrap:anywhere]" data-testid="status-line">{statusOf(d, me, at)}</p>
        <div className="flex flex-wrap gap-2">
          {d.state === "open" && <>{can("on_it") && btn("on_it", "I'm on it", "primary")}{can("resolve") && btn("resolve", "Mark resolved", "secondary")}</>}
          {d.state === "on_it" && can("resolve") && btn("resolve", "Mark resolved", "primary")}
          {d.state === "resolved" && can("reopen") && btn("reopen", "Reopen", "secondary")}
        </div>
      </section>

      <Panel title={traffic.length > 0 ? "Errors and traffic" : "Errors"} data-testid="chart">
        <LineChart series={series} marks={d.chart.deploys} marksList height={150} aria-label={d.chart.label} />
      </Panel>

      <Note d={d} />
      <Evidence d={d} />

      {d.slack && (
        <section className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border border-border bg-card px-[18px] py-3.5" data-testid="slack">
          <p className="m-0 min-w-0 text-sm [overflow-wrap:anywhere]">
            The note was posted in <b data-user-text>{d.slack.where}</b>.
          </p>
          {slackUrl && (
            <Button variant="secondary" asChild>
              <a href={slackUrl} target="_blank" rel="noopener noreferrer" data-testid="slack-link">Open the thread in Slack</a>
            </Button>
          )}
        </section>
      )}

      <SmallPanel title="Who was on it" data-testid="who">
        <p className="m-0 mt-1 text-sm text-muted-foreground" data-testid="who-line"><span data-user-text>{whoLine(d, me, at)}</span></p>
      </SmallPanel>

      {(worth.can_answer || worth.answer !== null) && (
        <SmallPanel title="Was this worth raising?" data-testid="worth">
          {askWorth ? (
            <div className="mt-2.5 flex flex-wrap gap-2">
              <Button variant="secondary" busy={busy === "worth_yes"} disabled={busy !== null && busy !== "worth_yes"} onClick={() => act("worth_yes")} data-testid="worth-yes">Yes</Button>
              <Button variant="secondary" busy={busy === "worth_no"} disabled={busy !== null && busy !== "worth_no"} onClick={() => act("worth_no")} data-testid="worth-no">No</Button>
            </div>
          ) : (
            <p className="m-0 mt-1 flex flex-wrap items-center gap-x-3 text-sm text-muted-foreground" data-testid="worth-answer">
              <span>
                <span data-user-text>{worth.by && me && worth.by === me ? "You" : worth.by ?? "Someone"}</span>
                {" "}answered {worth.answer ? "yes" : "no"}. Thank you.
              </span>
              {worth.can_answer && <button type="button" className={LINK_BTN} onClick={() => setChanging(true)} data-testid="worth-change">Change</button>}
            </p>
          )}
        </SmallPanel>
      )}

      {d.joined.length > 0 && (
        <ListSection title="Later notes on the same problem" aside={d.joined.length} data-testid="joined">
          {d.joined.map((j, i) => {
            const t = clock(Date.parse(j.at));
            return (
              <ListRow key={`${j.at}-${i}`} data-testid="joined-row">
                <div className="min-w-0 flex-1 basis-[240px]">
                  <div className="font-bold text-foreground" data-words-exempt>{j.title}</div>
                  <div className="text-[13px] text-muted-foreground">Joined this incident at {t}. It did not start a new one.</div>
                </div>
                <div className="shrink-0 whitespace-nowrap text-[13px] text-muted-foreground">{t}</div>
              </ListRow>
            );
          })}
        </ListSection>
      )}
    </>,
  );
}

export function IncidentPage() {
  const { id = "" } = useParams();
  return <IncidentView key={id} id={id} />;
}
