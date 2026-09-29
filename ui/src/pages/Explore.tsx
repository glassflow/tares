import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { Close, Search } from "../components/icons";
import { Picker, usePolling } from "../components/bits";
import type { LabelFacet, TimelineEventRow } from "../types";

// Explore — the hero, selector-first. Pick an entity on the left to start a selector, optionally
// narrow it with more label=value constraints (strict AND), and read everything matching across
// ALL sources as one correlated timeline (the `read` primitive). Picking a project narrows the
// same read to that project's sources. The Timeline/Agent toggle shows the readable timeline or
// the exact payload an agent receives over MCP.

const WINDOWS = ["1h", "24h", "7d", "30d"];
const ALL = "";  // scope sentinel: every source

// One label=value term of the conjunction. The first term carries display metadata (event count,
// the axis's declared sources) so we can label the header.
type Term = { label: string; value: string; events?: number; sources?: string[] };

/** The wire selector: label names as the store expects them (the unnamed primary axis is key_value). */
function toSelector(terms: Term[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const t of terms) out[t.label === "key" ? "key_value" : t.label] = t.value;
  return out;
}

export default function Explore() {
  const { data: entities, error } = usePolling(() => api.entities(), 15000);
  const { data: projects } = usePolling(() => api.projects(), 30000);

  const [q, setQ] = useState("");
  const [terms, setTerms] = useState<Term[]>([]);
  const [scope, setScope] = useState(ALL);
  const [window_, setWindow] = useState("1h");
  const [mode, setMode] = useState<"human" | "agent">("human");
  const [payload, setPayload] = useState<string>();
  const [rows, setRows] = useState<TimelineEventRow[]>();
  const [readSources, setReadSources] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [qerror, setQerror] = useState<string>();
  const [copied, setCopied] = useState(false);

  // Facets with values: primary (key) axes first, then the richest axis first so the group that
  // holds the default landing entity sits at the top of the picker (visible, highlighted).
  const facets = useMemo(
    () => (entities?.labels ?? [])
      .filter((f) => f.values.length)
      .sort((a, b) =>
        Number(b.primary) - Number(a.primary) ||
        (b.values[0]?.events ?? 0) - (a.values[0]?.events ?? 0)),
    [entities],
  );

  // Default landing: the entity with the most events (the richest timeline — best thing on screen
  // when a demo opens).
  useEffect(() => {
    if (terms.length || !facets.length) return;
    let best: Term | null = null;
    for (const f of facets) {
      const top = f.values[0]; // API returns values sorted by events desc
      if (top && (!best || top.events > (best.events ?? 0))) {
        best = { label: f.label, value: top.value, events: top.events, sources: f.sources };
      }
    }
    if (best) setTerms([best]);
  }, [facets, terms.length]);

  const primary = terms[0];
  const selector = useMemo(() => toSelector(terms), [terms]);
  const projectList = projects?.projects ?? [];
  const effectiveScope = scope && projectList.some((p) => p.id === scope) ? scope : ALL;

  // (Re)run the read whenever the selector, scope or window changes; refresh on an interval so the
  // timeline feels live.
  useEffect(() => {
    if (!terms.length) { setPayload(undefined); setRows(undefined); return; }
    let live = true;
    const run = (spinner: boolean) => {
      if (document.hidden) return;
      if (spinner) setLoading(true);
      api.read(selector, window_, effectiveScope ? { project: effectiveScope } : undefined)
        .then((r) => { if (live) setReadSources(r.sources); return r; })
        .then((r) => { if (live) { setPayload(r.payload); setRows(r.rows ?? []); setQerror(undefined); } })
        .catch((e) => { if (live) { setQerror(String((e as Error).message ?? e)); setPayload(undefined); setRows(undefined); } })
        .finally(() => { if (live) setLoading(false); });
    };
    run(true);
    const id = setInterval(() => run(false), 10000);
    // run() skips while the tab is hidden, so a read that was due then must fire the moment the
    // tab becomes visible — otherwise "reading timeline…" sits there until the next interval tick.
    const onVisible = () => { if (!document.hidden) run(true); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { live = false; clearInterval(id); document.removeEventListener("visibilitychange", onVisible); };
  }, [selector, effectiveScope, window_]); // eslint-disable-line react-hooks/exhaustive-deps

  const needle = q.trim().toLowerCase();
  const pick = (f: LabelFacet, value: string, events: number) => {
    setTerms([{ label: f.label, value, events, sources: f.sources }]);
    setMode("human");
  };
  const addTerm = (label: string, value: string) => setTerms((t) => [...t, { label, value }]);
  const removeTerm = (i: number) => setTerms((t) => t.filter((_, j) => j !== i));

  const copy = () => {
    if (payload === undefined) return;
    navigator.clipboard?.writeText(payload);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Left picker: one axis (label) → a group of its values. Split into "read & alert by" (primary
  // labels) and "other ways to slice" (the rest) so the picker explains itself without jargon.
  const renderFacet = (f: LabelFacet) => {
    const vals = f.values.filter((v) => !needle ||
      v.value.toLowerCase().includes(needle) || f.label.toLowerCase().includes(needle));
    if (needle && !vals.length) return null;
    return (
      <div className="cat-group" key={f.label}>
        <div className="cat-group-head">
          <span>{f.label}</span>
          {f.high_cardinality && (
            <span className="badge starting" title="high-cardinality: too many distinct values to profile as an entity axis; showing a live sample">high-card</span>
          )}
          <span className="n">{vals.length}</span>
        </div>
        {vals.map((v) => {
          const active = primary?.label === f.label && primary?.value === v.value;
          return (
            <div key={v.value} className={"explore-item ent" + (active ? " active" : "")}
                 onClick={() => pick(f, v.value, v.events)} title={v.value}>
              <span className="ent-val">{v.value}</span>
              <span className="ent-n">{v.events.toLocaleString()}</span>
            </div>
          );
        })}
      </div>
    );
  };
  const primaryNodes = facets.filter((f) => f.primary).map(renderFacet).filter(Boolean);
  const secondaryNodes = facets.filter((f) => !f.primary).map(renderFacet).filter(Boolean);

  const shownSources = readSources;
  const emptyHint = effectiveScope === ALL
    ? "no events match this selector in the last " + window_ + "; widen the window or remove a filter."
    : "this project's sources hold nothing for this selector; switch to All sources to read every source.";

  return (
    <>
      <h1>Explore</h1>
      <p className="subtitle">pick an entity, filter it if you like; read everything matching across every source, <em>one timeline</em></p>

      {error && <div className="alert error">{error}</div>}
      {!facets.length && !error && (
        <div className="empty">
          no entities yet; add a source (each declares one or more labels; one is its key), then
          ingest some events.
        </div>
      )}

      {facets.length > 0 && (
        <div className="explore">
          {/* left: entity picker, grouped by axis */}
          <aside className="explore-list">
            <div className="search-box full" style={{ marginBottom: 12 }}>
              <Search />
              <input type="text" className="search" placeholder="Filter entities…"
                     value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            {!!primaryNodes.length && (
              <div className="ent-section first" title="the entities you read and alert an agent by">
                read &amp; alert by
              </div>
            )}
            {primaryNodes}
            {!!secondaryNodes.length && (
              <div className="ent-section" title="extra labels you can slice the same events by">
                other ways to slice
              </div>
            )}
            {secondaryNodes}
            {needle && !primaryNodes.length && !secondaryNodes.length && (
              <div className="dim" style={{ fontSize: 13, padding: "4px 9px" }}>no entities match “{q}”</div>
            )}
          </aside>

          {/* right: the correlated timeline */}
          <section className="explore-detail">
            {!primary && <div className="dim">select an entity…</div>}
            {primary && (
              <>
                <div className="tl-head">
                  <div>
                    <h2 style={{ margin: 0 }}><span className="mono">{primary.value}</span></h2>
                    <span className="subtitle" style={{ margin: 0 }}>
                      {primary.label}{primary.events != null && <> · {primary.events.toLocaleString()} events</>}
                      {shownSources.length > 0 && <> · {shownSources.length} source{shownSources.length === 1 ? "" : "s"}</>}
                    </span>
                  </div>
                  <div className="seg">
                    <button className={mode === "human" ? "active" : ""} onClick={() => setMode("human")}>Timeline</button>
                    <button className={mode === "agent" ? "active" : ""} onClick={() => setMode("agent")}>What the agent gets</button>
                  </div>
                </div>

                {/* selector: the conjunction of label=value terms (strict AND) */}
                <div className="tl-selector">
                  {terms.map((t, i) => (
                    <span className="chip term" key={`${t.label}=${t.value}`}>
                      <span className="mono">{t.label}={t.value}</span>
                      <button className="term-x" onClick={() => removeTerm(i)} aria-label="remove">
                        <Close />
                      </button>
                    </span>
                  ))}
                  <AddTerm facets={facets} used={terms.map((t) => t.label)} onAdd={addTerm} />
                </div>

                <div className="tl-controls">
                  {/* A div, not a label — as the `window` control beside it already is. Picker
                      renders a <button>, which IS labelable, so a wrapping <label> forwards clicks
                      on the word into opening the menu. */}
                  <div className="tl-ctl">
                    <span className="lbl">read from</span>
                    {/* Picker, not a native <select>: the OS draws a <select>'s open menu and won't
                        let us theme it, so it lands as a light box in the dark console. */}
                    <Picker value={effectiveScope} onChange={setScope} ariaLabel="read from"
                            options={[ALL, ...projectList.map((p) => p.id)]}
                            labels={{ [ALL]: "All sources",
                                      ...Object.fromEntries(projectList.map((p) => [p.id, `Project ${p.name}`])) }} />
                  </div>
                  <div className="tl-ctl">
                    <span className="lbl">window</span>
                    <div className="seg small">
                      {WINDOWS.map((w) => (
                        <button key={w} className={window_ === w ? "active" : ""} onClick={() => setWindow(w)}>{w}</button>
                      ))}
                    </div>
                  </div>
                  {shownSources.length > 0 && (
                    <div className="tl-sources">
                      {shownSources.map((s) => <span className="chip" key={s}>{s}</span>)}
                    </div>
                  )}
                  <span className="grow" />
                  {loading && <span className="help">reading…</span>}
                </div>

                {qerror && <div className="alert error">{qerror}</div>}

                {mode === "agent" ? (
                  <div className="panel" style={{ marginTop: 12 }}>
                    <div className="tl-agent-head">
                      <span className="help" style={{ margin: 0 }}>
                        exactly what the agent receives over MCP · read · {window_}
                      </span>
                      <button className="copybtn" onClick={copy}>{copied ? "copied" : "copy"}</button>
                    </div>
                    {payload !== undefined
                      ? <pre className="payload" style={{ marginTop: 8 }}>{payload}</pre>
                      : !qerror && <div className="help">reading timeline…</div>}
                  </div>
                ) : (
                  <TimelineTable rows={rows} loading={rows === undefined && !qerror} emptyHint={emptyHint} />
                )}
              </>
            )}
          </section>
        </div>
      )}
    </>
  );
}

/** The conjunction builder: a page-native popover — pick an axis, then a value, to AND-narrow the
 *  selector. Custom (not a native <select>) so the menu matches the console's look. */
function AddTerm({ facets, used, onAdd }:
  { facets: LabelFacet[]; used: string[]; onAdd: (label: string, value: string) => void }) {
  const [open, setOpen] = useState(false);
  const [axis, setAxis] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const available = facets.filter((f) => !used.includes(f.label));
  const facet = facets.find((f) => f.label === axis);
  const close = () => { setOpen(false); setAxis(""); };

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  if (!available.length) return null;
  return (
    <div className="tl-add" ref={ref}>
      <button className="tl-add-btn" onClick={() => (open ? close() : setOpen(true))}>+ add filter</button>
      {open && (
        <div className="menu" role="menu">
          {!facet ? (
            available.map((f) => (
              <button key={f.label} className="menu-item" role="menuitem" onClick={() => setAxis(f.label)}>
                <span className="menu-val">{f.label}</span>
                <span className="menu-n">{f.values.length}</span>
              </button>
            ))
          ) : (
            <>
              <button className="menu-item back" onClick={() => setAxis("")}>← {facet.label}</button>
              <div className="menu-scroll">
                {facet.values.map((v) => (
                  <button key={v.value} className="menu-item" role="menuitem" title={v.value}
                          onClick={() => { onAdd(axis, v.value); close(); }}>
                    <span className="menu-val">{v.value}</span>
                    <span className="menu-n">{v.events.toLocaleString()}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function TimelineTable({ rows, loading, emptyHint }:
  { rows: TimelineEventRow[] | undefined; loading: boolean; emptyHint: string }) {
  if (loading) return <div className="help" style={{ marginTop: 14 }}>reading timeline…</div>;
  if (!rows) return null;
  if (!rows.length) return <div className="empty">{emptyHint}</div>;
  return (
    <table style={{ marginTop: 12 }}>
      <thead><tr><th style={{ width: 74 }}>when</th><th style={{ width: 180 }}>source</th><th>event</th></tr></thead>
      <tbody>
        {rows.map((r, i) => {
          const labels = Object.entries(r.labels ?? {});
          return (
            <tr key={i}>
              <td className="mono dim" style={{ whiteSpace: "nowrap" }}>{r.offset}</td>
              <td><span className="chip">{r.source}</span></td>
              <td>
                <span className="mono" style={{ whiteSpace: "pre-wrap" }}>{r.text}</span>
                {labels.length > 0 && (
                  <span className="tl-labels">
                    {labels.map(([k, v]) => <span className="chip lbl" key={k}>{k}={v}</span>)}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
