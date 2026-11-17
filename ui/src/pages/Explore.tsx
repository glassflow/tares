import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import {
  Banner, Button, EmptyState, Input, ListRow, ListSection, LoadingBlocks, Panel, Select, SkeletonRows, Tabs,
} from "../components/edition";
import { Close } from "../components/icons";
import { copyText } from "../editions/copyText";
import { formatCount } from "../lib/number";
import { ageWords } from "../lib/time";
import type { LabelFacet, TimelineEventRow } from "../types";

// Explore: pick something on the left (a repo, a service, a person: whatever the connections label
// their events by), narrow it with more label=value terms (strict AND), and read everything that
// matches, from every connection, as one timeline (the `read` primitive). Picking a project narrows
// the same read to that project's connections. "What the agent gets" shows the exact payload an
// agent receives over MCP for the same read.
//
// Label names and values are the connections' own (data-user-text); the page's words are the rest.

const WINDOWS = [
  { value: "1h", label: "Last hour", empty: "in the last hour" },
  { value: "24h", label: "Last 24 hours", empty: "in the last 24 hours" },
  { value: "7d", label: "Last 7 days", empty: "in the last 7 days" },
  { value: "30d", label: "Last 30 days", empty: "in the last 30 days" },
];
// The scope sentinel for "every connection". Radix Select refuses "" as an item value.
const ALL = "all";

// One label=value term of the conjunction. The first term carries what the header shows (its event
// count, the connections that declare the label).
type Term = { label: string; value: string; events?: number; sources?: string[] };

/** The wire selector: label names as the store expects them (the unnamed primary axis is key_value). */
function toSelector(terms: Term[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const t of terms) out[t.label === "key" ? "key_value" : t.label] = t.value;
  return out;
}

/** A row's offset comes as `T-<seconds>s`; the page says it in the one time grammar. */
function whenOf(offset: string): string {
  const m = /^T-(\d+)s$/.exec(offset);
  return m ? ageWords(Number(m[1]) * 1000) : offset;
}

const plural = (n: number, one: string, many: string) => `${formatCount(n)} ${n === 1 ? one : many}`;

export default function Explore() {
  const { data: entities, error, reload } = usePolling(() => api.entities(), 15000);
  const { data: projects } = usePolling(() => api.projects(), 30000);

  const [q, setQ] = useState("");
  const [terms, setTerms] = useState<Term[]>([]);
  const [scope, setScope] = useState(ALL);
  const [window_, setWindow] = useState("1h");
  const [mode, setMode] = useState<"human" | "agent">("human");
  const [payload, setPayload] = useState<string>();
  const [rows, setRows] = useState<TimelineEventRow[]>();
  const [readFrom, setReadFrom] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [qerror, setQerror] = useState<string>();
  const [copied, setCopied] = useState(false);

  // Labels with values: primary (key) labels first, then the richest first, so the group holding
  // the landing value sits at the top of the picker.
  const facets = useMemo(
    () => (entities?.labels ?? [])
      .filter((f) => f.values.length)
      .sort((a, b) =>
        Number(b.primary) - Number(a.primary) ||
        (b.values[0]?.events ?? 0) - (a.values[0]?.events ?? 0)),
    [entities],
  );

  // Landing: the value with the most events, the richest timeline on screen when the page opens.
  useEffect(() => {
    if (terms.length || !facets.length) return;
    let best: Term | null = null;
    for (const f of facets) {
      const top = f.values[0]; // the API sorts values by events, most first
      if (top && (!best || top.events > (best.events ?? 0))) {
        best = { label: f.label, value: top.value, events: top.events, sources: f.sources };
      }
    }
    if (best) setTerms([best]);
  }, [facets, terms.length]);

  const primary = terms[0];
  const selector = useMemo(() => toSelector(terms), [terms]);
  const projectList = projects?.projects ?? [];
  const project = scope !== ALL && projectList.some((p) => p.id === scope) ? scope : "";
  const windowWords = WINDOWS.find((w) => w.value === window_) ?? WINDOWS[0];

  // (Re)read whenever the selector, the project or the window changes; refresh on an interval so the
  // timeline stays live.
  useEffect(() => {
    if (!terms.length) { setPayload(undefined); setRows(undefined); return; }
    let live = true;
    const read = (spinner: boolean) => {
      if (document.hidden) return;
      if (spinner) setLoading(true);
      api.read(selector, window_, project ? { project } : undefined)
        .then((r) => { if (live) { setReadFrom(r.sources); setPayload(r.payload); setRows(r.rows ?? []); setQerror(undefined); } })
        .catch((e) => { if (live) { setQerror(String((e as Error).message ?? e)); setPayload(undefined); setRows(undefined); } })
        .finally(() => { if (live) setLoading(false); });
    };
    read(true);
    const id = setInterval(() => read(false), 10000);
    // read() skips while the tab is hidden, so a read that was due then fires the moment the tab is
    // visible again, or "Reading" would sit there until the next tick.
    const onVisible = () => { if (!document.hidden) read(true); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { live = false; clearInterval(id); document.removeEventListener("visibilitychange", onVisible); };
  }, [selector, project, window_]); // eslint-disable-line react-hooks/exhaustive-deps

  const needle = q.trim().toLowerCase();
  const pick = (f: LabelFacet, value: string, events: number) => {
    setTerms([{ label: f.label, value, events, sources: f.sources }]);
    setMode("human");
  };
  const addTerm = (label: string, value: string) => setTerms((t) => [...t, { label, value }]);
  const removeTerm = (i: number) => setTerms((t) => t.filter((_, j) => j !== i));

  const copy = () => {
    if (payload === undefined) return;
    copyText(payload).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => {});
  };

  // The picker: one group per label, its values beneath. "Read and alert by" holds the primary
  // labels, "Other ways to slice" the rest, so the picker explains itself.
  const renderFacet = (f: LabelFacet) => {
    const vals = f.values.filter((v) => !needle ||
      v.value.toLowerCase().includes(needle) || f.label.toLowerCase().includes(needle));
    if (needle && !vals.length) return null;
    return (
      <div className="grid gap-0.5" key={f.label} data-testid="explore-group">
        <div className="flex items-baseline gap-2 px-2 pb-1 pt-2 text-xs text-muted-foreground">
          <span className="font-mono" data-user-text>{f.label}</span>
          {f.high_cardinality && (
            <span className="text-faint" title="Too many values to list them all, so this is a live sample.">sample</span>
          )}
          <span className="ml-auto font-mono">{vals.length}</span>
        </div>
        {vals.map((v) => {
          const active = primary?.label === f.label && primary?.value === v.value;
          return (
            <Button key={v.value} variant="quiet" aria-pressed={active} title={v.value} data-testid="explore-value"
                    onClick={() => pick(f, v.value, v.events)}
                    className={`h-8 w-full justify-between gap-3 px-2 ${active ? "bg-muted font-semibold text-foreground shadow-[inset_3px_0_0_var(--accent)]" : "font-normal"}`}>
              <span className="min-w-0 truncate font-mono text-[13px]" data-user-text>{v.value}</span>
              <span className="font-mono text-xs text-faint">{formatCount(v.events)}</span>
            </Button>
          );
        })}
      </div>
    );
  };
  const primaryNodes = facets.filter((f) => f.primary).map(renderFacet).filter(Boolean);
  const otherNodes = facets.filter((f) => !f.primary).map(renderFacet).filter(Boolean);
  const sectionHead = "px-2 pt-3 font-mono text-[11px] uppercase tracking-[0.08em] text-faint";

  const timeline = (
    <TimelineRows rows={rows} loading={rows === undefined && !qerror} title={windowWords.label}
                  empty={project
                    ? "This project's connections hold nothing for this. Read from every connection to see the rest."
                    : `Nothing matches this ${windowWords.empty}. Try a longer window, or remove a filter.`} />
  );
  const agent = (
    <Panel title={`Exactly what an agent receives over MCP for the ${windowWords.label.toLowerCase()}`}
           action={<Button variant="secondary" onClick={copy} disabled={payload === undefined}>{copied ? "Copied" : "Copy"}</Button>}
           data-testid="explore-agent">
      {payload !== undefined
        ? <pre className="m-0 max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs text-foreground" data-user-text data-testid="explore-payload">{payload}</pre>
        : !qerror && <SkeletonRows count={3} />}
    </Panel>
  );

  return (
    <div className="grid min-w-0 gap-5" data-testid="explore-page">
      <header className="min-w-0">
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Explore</h1>
        <p className="m-0 max-w-2xl text-sm text-muted-foreground">
          Pick a repo, a service or anything else your connections label their events by, and narrow it if you
          like. Everything about it, from every connection, on one timeline.
        </p>
      </header>

      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
          Tares is not answering: {error}
        </Banner>
      )}
      {!entities && !error && <LoadingBlocks />}
      {entities && !facets.length && (
        <EmptyState title="Nothing to explore yet" data-testid="explore-empty"
                    action={<Button asChild variant="secondary"><Link to="/sources">See the connections</Link></Button>}>
          Once a connection receives events, what they are about shows here: each repo, service or person they name.
        </EmptyState>
      )}

      {facets.length > 0 && (
        <div className="grid min-w-0 items-start gap-6 md:grid-cols-[17rem_minmax(0,1fr)]">
          <aside className="grid min-w-0 gap-1 md:sticky md:top-4" aria-label="What to explore">
            <Input label="Search" placeholder="A value or a label" value={q}
                   onChange={(e) => setQ(e.target.value)} data-testid="explore-search" />
            <div className="grid max-h-80 min-w-0 gap-1 overflow-y-auto border-b border-border pb-2 md:max-h-[70vh] md:border-b-0">
              {!!primaryNodes.length && <div className={sectionHead} title="The labels an agent is read and woken by.">Read and alert by</div>}
              {primaryNodes}
              {!!otherNodes.length && <div className={sectionHead} title="More labels the same events carry.">Other ways to slice</div>}
              {otherNodes}
              {needle && !primaryNodes.length && !otherNodes.length && (
                <EmptyState className="px-2" data-testid="explore-nomatch">Nothing matches “{q}”.</EmptyState>
              )}
            </div>
          </aside>

          <section className="grid min-w-0 gap-4" data-testid="explore-detail">
            {!primary && <EmptyState>Pick something on the left to read its timeline.</EmptyState>}
            {primary && (
              <>
                <div className="grid min-w-0 gap-1">
                  <h2 className="m-0 font-mono text-lg [overflow-wrap:anywhere]" data-user-text data-testid="explore-title">{primary.value}</h2>
                  <p className="m-0 text-[13px] text-muted-foreground" data-testid="explore-meta">
                    <span className="font-mono" data-user-text>{primary.label}</span>
                    {primary.events != null && <>, {plural(primary.events, "event", "events")}</>}
                    {readFrom.length > 0 && <>, from {plural(readFrom.length, "connection", "connections")}: <span data-user-text>{readFrom.join(", ")}</span></>}
                  </p>
                </div>

                {/* The selector: label=value terms, all of which must hold. */}
                <div className="flex min-w-0 flex-wrap items-end gap-2" data-testid="explore-terms">
                  {terms.map((t, i) => (
                    <span key={`${t.label}=${t.value}`} data-testid="explore-term"
                          className="inline-flex h-9 min-w-0 items-center gap-1 border border-border bg-card pl-2.5 font-mono text-xs">
                      <span className="min-w-0 truncate" data-user-text>{t.label}={t.value}</span>
                      <Button variant="quiet" aria-label={`Remove ${t.label}=${t.value}`} onClick={() => removeTerm(i)}
                              className="h-8 w-8 px-0">
                        <Close />
                      </Button>
                    </span>
                  ))}
                  <AddTerm facets={facets} used={terms.map((t) => t.label)} onAdd={addTerm} />
                </div>

                <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,16rem)_minmax(0,12rem)_1fr] sm:items-end">
                  <Select label="Read from" value={project || ALL} onValueChange={setScope} data-testid="explore-scope"
                          options={[{ value: ALL, label: "Every connection" },
                                    ...projectList.map((p) => ({ value: p.id, label: `Project ${p.name}` }))]} />
                  <Select label="Window" value={window_} onValueChange={setWindow} data-testid="explore-window"
                          options={WINDOWS.map(({ value, label }) => ({ value, label }))} />
                  <span role="status" className="pb-2 text-[13px] text-muted-foreground sm:justify-self-end">
                    {loading ? "Reading" : ""}
                  </span>
                </div>

                {qerror && (
                  <Banner tone="error" data-testid="explore-read-error">Tares could not read this: {qerror}</Banner>
                )}

                <Tabs aria-label="How to show it" value={mode} onValueChange={(v) => setMode(v as "human" | "agent")}
                      items={[
                        { value: "human", label: "Timeline", content: mode === "human" ? timeline : null },
                        { value: "agent", label: "What the agent gets", content: mode === "agent" ? agent : null },
                      ]} />
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

/** Narrow the selector: pick a label, then one of its values. Two kit Selects, so the keyboard and a
 *  screen reader get a real listbox, not a hand-drawn menu. */
function AddTerm({ facets, used, onAdd }:
  { facets: LabelFacet[]; used: string[]; onAdd: (label: string, value: string) => void }) {
  const [axis, setAxis] = useState("");
  const available = facets.filter((f) => !used.includes(f.label));
  const facet = facets.find((f) => f.label === axis);
  if (!available.length) return null;
  if (!facet) {
    return (
      <Select label="Add a filter" placeholder="Pick a label" value="" onValueChange={setAxis}
              className="h-9 w-44" data-testid="explore-add"
              options={available.map((f) => ({ value: f.label, label: `${f.label} (${formatCount(f.values.length)})` }))} />
    );
  }
  return (
    <span className="inline-flex items-end gap-1">
      <Select label={`Filter by ${facet.label}`} placeholder="Pick a value" value=""
              onValueChange={(v) => { onAdd(facet.label, v); setAxis(""); }}
              className="h-9 w-56" data-testid="explore-add-value"
              options={facet.values.map((v) => ({ value: v.value, label: `${v.value} (${formatCount(v.events)})` }))} />
      <Button variant="quiet" onClick={() => setAxis("")} className="h-9">Cancel</Button>
    </span>
  );
}

function TimelineRows({ rows, loading, title, empty }:
  { rows: TimelineEventRow[] | undefined; loading: boolean; title: string; empty: string }) {
  if (loading) return <SkeletonRows count={4} className="pt-3" />;
  if (!rows) return null;
  if (!rows.length) return <EmptyState className="pt-3" data-testid="explore-nothing">{empty}</EmptyState>;
  return (
    <ListSection paper className="mt-3" title={title}
                 aside={<span data-testid="explore-count">{plural(rows.length, "event", "events")}</span>}>
      {rows.map((r, i) => {
        const labels = Object.entries(r.labels ?? {});
        return (
          <ListRow key={i} data-testid="explore-row"
                   className="grid grid-cols-1 items-start gap-x-5 gap-y-1 md:grid-cols-[8rem_10rem_minmax(0,1fr)]">
            <span className="whitespace-nowrap text-[13px] text-muted-foreground" data-testid="explore-when">{whenOf(r.offset)}</span>
            <span className="min-w-0 truncate font-mono text-xs text-muted-foreground" data-user-text data-testid="explore-from">{r.source}</span>
            <div className="grid min-w-0 gap-1.5">
              <span className="whitespace-pre-wrap font-mono text-[13px] text-foreground [overflow-wrap:anywhere]" data-user-text>{r.text}</span>
              {labels.length > 0 && (
                <span className="flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-faint" data-user-text>
                  {labels.map(([k, v]) => <span key={k} className="[overflow-wrap:anywhere]">{k}={v}</span>)}
                </span>
              )}
            </div>
          </ListRow>
        );
      })}
    </ListSection>
  );
}
