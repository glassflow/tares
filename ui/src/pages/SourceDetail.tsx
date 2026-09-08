import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import Dependents from "../components/Dependents";
import ProjectBadge from "../components/ProjectBadge";
import IngestEndpoint from "../components/IngestEndpoint";
import SourceForm from "../components/SourceForm";
import { ErrorState, StatusBadge, TimeAgo, ruleSummary, usePolling } from "../components/bits";
import type { ConnectorSpec, Source } from "../types";

export default function SourceDetail() {
  const { name = "" } = useParams();
  const nav = useNavigate();
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>();
  const [tab, setTab] = useState<"fields" | "events" | "config">("fields");
  const [actionError, setActionError] = useState<string>();
  const [confirmDel, setConfirmDel] = useState(false);
  const [purge, setPurge] = useState(false);
  const [cascade, setCascade] = useState(true);
  const tabsRef = useRef<HTMLDivElement>(null);

  // Editing labels opens the Configuration tab, which renders below the fold — scroll to it so it's
  // obvious something opened (otherwise the page looks unchanged).
  const openConfig = () => {
    setTab("config");
    requestAnimationFrame(() =>
      tabsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const { data: source, error, reload } = usePolling(() => api.source(name));
  const { data: events, error: eventsError } = usePolling(() => api.sourceEvents(name, 30));

  useEffect(() => { api.connectors().then(setSpecs); }, []);

  if (error) return <div className="alert error">{error}</div>;
  if (!source || !specs) return <div className="dim">loading…</div>;

  const spec = specs[source.connector];
  const h = source.health;

  const act = (fn: () => Promise<unknown>) => async () => {
    setActionError(undefined);
    try { await fn(); reload(); } catch (e) { setActionError(String((e as Error).message ?? e)); }
  };

  return (
    <>
      <div className="pagehead">
        <div>
          <h1><span className="mono">{source.name}</span></h1>
          <p className="subtitle">
            {spec?.label ?? source.connector}
            {source.owned_by && <> · <ProjectBadge ownedBy={source.owned_by} customized={source.customized} /></>}
          </p>
        </div>
        <div className="btnrow">
          {source.paused
            ? <button onClick={act(() => api.resumeSource(name))}>Resume</button>
            : <button onClick={act(() => api.pauseSource(name))}>Pause</button>}
          <button className="danger" onClick={() => { setPurge(false); setConfirmDel(true); }}>Delete</button>
        </div>
      </div>

      {actionError && <div className="alert error">{actionError}</div>}

      {spec?.mode === "push" && <IngestEndpoint source={source} />}

      <div className="cards">
        <div className="card"><div className="k">status</div><div className="v"><StatusBadge status={h?.status} /></div></div>
        <div className="card"><div className="k">events stored</div><div className="v">{(h?.events_total ?? 0).toLocaleString()}</div></div>
        <div className="card"><div className="k">since daemon start</div><div className="v">{h?.events_since_start ?? 0} <small>events / {h?.polls ?? 0} polls</small></div></div>
        <div className="card"><div className="k">last ingest</div><div className="v" style={{ fontSize: 15 }}><TimeAgo ts={h?.last_ingest} /></div></div>
      </div>

      {h?.last_error && (
        <div className="alert error">
          last error ({h.consecutive_errors} consecutive): <span className="mono">{h.last_error}</span>
        </div>
      )}

      <LabelsSummary source={source} onEdit={openConfig} />

      <div className="tabs" style={{ marginTop: 16, scrollMarginTop: 60 }} ref={tabsRef}>
        <button className={tab === "fields" ? "active" : ""} onClick={() => setTab("fields")}>Fields</button>
        <button className={tab === "events" ? "active" : ""} onClick={() => setTab("events")}>Recent events</button>
        <button className={tab === "config" ? "active" : ""} onClick={() => setTab("config")}>Configuration</button>
      </div>

      {tab === "fields" && <FieldsPanel name={name} source={source} />}

      {tab === "config" && spec && (
        <div className="panel">
          <SourceForm
            connector={source.connector}
            spec={spec}
            lockName
            initial={{ name: source.name, type: source.type, poll: source.poll, config: source.config }}
            submitLabel="Save changes"
            onSubmit={async (body) => {
              await api.updateSource(name, body);
              setTab("fields");
              reload();
            }}
          />
        </div>
      )}

      {tab === "events" && (
        <>
          {eventsError && <ErrorState error={eventsError} what="recent events" />}
          {!eventsError && !events?.length && <div className="empty">nothing ingested from this source yet</div>}
          {!!events?.length && (
            <table>
              <thead><tr><th>ingested</th><th>key</th><th>type</th><th>text</th></tr></thead>
              <tbody>
                {events.map((e, i) => (
                  <tr key={i}>
                    <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={e.ingest_time} /></td>
                    <td className="mono">{e.key}</td>
                    <td className="mono">{e.event_type}</td>
                    <td className="mono" style={{ whiteSpace: "pre-wrap" }}>{e.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {confirmDel && (
        <ConfirmDialog
          title={`Delete source "${name}"?`}
          confirmLabel="Delete source"
          danger
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setConfirmDel(false);
            setActionError(undefined);
            try { await api.deleteSource(name, purge, cascade); nav("/sources"); }
            catch (e) { setActionError(String((e as Error).message ?? e)); }
          }}
        >
          <p className="help" style={{ whiteSpace: "normal", margin: 0 }}>
            Its configuration is removed. Stored events are kept unless you purge them.
          </p>
          <Dependents kind="source" name={name} cascade={cascade} onChange={(c) => setCascade(c)} />
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} />
            <span>also purge its stored events{h?.events_total ? ` (${h.events_total.toLocaleString()})` : ""}</span>
          </label>
        </ConfirmDialog>
      )}
    </>
  );
}

// The fields this source carries, sampled from recent events. Nested contexts (e.g. a Prometheus
// label set) are flattened to sub-fields (metric.service, …) so the real keyable axes are visible;
// the backend marks which are declared labels/keys. Coverage is only shown when it varies (a full
// 100%-everywhere column is noise), so partial fields stand out.
// The declared entity axes — the single most important config of a source, so it lives on the
// main screen (not buried in the edit form): every event carries these labels, the key names
// the timeline agents read.
function LabelsSummary({ source, onEdit }: { source: Source; onEdit: () => void }) {
  const labels = (source.config?.labels ?? []) as Array<{
    name: string; field?: string; const?: string; primary?: boolean }>;
  // Collapsed by default: the Labels panel below says everything this one does and more (coverage,
  // top values, rules), so on a source with many labels this was mostly page length. The header
  // keeps the count and the Edit button, so nothing is lost while it is shut.
  const [open, setOpen] = useState(false);
  const key = labels.find((l) => l.primary)?.name;
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="pagehead" style={{ marginBottom: open && labels.length ? 8 : 0 }}>
        <button className="disclosure" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <svg className={"disclosure-caret" + (open ? " open" : "")} width="10" height="7"
               viewBox="0 0 10 7" fill="none" aria-hidden="true">
            <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          <h2 style={{ margin: 0 }}>Labels &amp; key</h2>
          {!open && labels.length > 0 && (
            <small className="dim">
              · {labels.length} label{labels.length === 1 ? "" : "s"}{key ? <> · key <span className="mono">{key}</span></> : null}
            </small>
          )}
        </button>
        <button onClick={onEdit}>Edit</button>
      </div>
      {!open ? null : labels.length ? (
        <table>
          <thead><tr><th>label</th><th>from</th></tr></thead>
          <tbody>
            {labels.map((l) => (
              <tr key={l.name}>
                <td className="mono">
                  {l.name}
                  {l.primary && <span className="badge ok" style={{ marginLeft: 8 }}>key</span>}
                </td>
                <td className="mono">
                  {"field" in l && l.field
                    ? <><span className="badge push" style={{ marginRight: 8 }}>field</span>{l.field}</>
                    : <><span className="badge push" style={{ marginRight: 8 }}>const</span>{String(l.const ?? "")}</>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="help" style={{ margin: 0, whiteSpace: "normal" }}>
          No labels declared; events fall back to the connector&rsquo;s default key, and agents
          can&rsquo;t slice this source by any axis. Check <strong>Fields</strong> below for the
          candidates observed in your data, then declare them here.
        </p>
      )}
    </div>
  );
}

const FIELDS_PAGE = 12;

function FieldsPanel({ name, source }: { name: string; source: Source }) {
  // The rules live in the source config; the profile API only reports coverage and values. Cross
  // referencing by name is what lets this table say WHICH labels are derived — it claims to include
  // them and used to render a regex-rewritten label identically to a straight pass-through one.
  const rules = Object.fromEntries(
    ((source.config?.labels ?? []) as Array<{ name: string; pattern?: string; map?: object }>)
      .map((l) => [l.name, ruleSummary(l.pattern, Object.keys(l.map ?? {}).length)]));
  const { data } = usePolling(() => api.sourceFields(name), 5000);
  const [showAll, setShowAll] = useState(false);
  if (!data || !data.fields.length) return null;
  const fmt = (v: string) => (v.length > 44 ? v.slice(0, 43) + "…" : v);
  // ONLY the fields that are not already labels. Anything declared is listed — with more detail —
  // in the Labels table directly above, so including it here printed every declared axis twice on
  // one screen. What is left is exactly the useful question this table answers: what else is in the
  // data that you could promote?
  const candidates = data.fields.filter((f) => !f.is_label && !f.is_key);
  const allFull = candidates.every((f) => f.coverage === data.sampled);
  // Densest first — the best promotion candidates lead, long tails fold behind "show all".
  const sorted = [...candidates].sort((a, b) =>
    b.coverage - a.coverage || a.name.localeCompare(b.name));
  const shown = showAll ? sorted : sorted.slice(0, FIELDS_PAGE);
  const labels = data.labels ?? [];
  const allLabelsFull = labels.every((l) => l.coverage === data.sampled);
  return (
    <>
    {labels.length > 0 && (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Labels <small className="dim">· {data.sampled} events sampled</small></h2>
        {/* No subtitle. It described the columns that are right underneath it — the badges, the
            coverage bar and the values say the same thing without a paragraph. The one fact it
            carried that the table doesn't is kept where it matters: the 0-coverage rows say so
            inline, on the row it applies to. */}
        <table>
          <thead><tr>
            <th>label</th>
            {!allLabelsFull && <th style={{ width: 180 }}>coverage</th>}
            <th className="num">distinct</th>
            <th>top values</th>
          </tr></thead>
          <tbody>
            {labels.map((l) => (
              <tr key={l.name} style={l.coverage === 0 ? { opacity: 0.55 } : undefined}>
                <td className="mono">
                  {/* One row, never wrapping. Inline, a long name pushed the badge onto a second
                      line while short names kept it alongside, so badges staggered down the column
                      by name length. A nowrap flex cell lines them all up. */}
                  <div className="label-cell">
                    <span className="label-cell-name">{l.name}</span>
                    {l.is_key ? <span className="badge ok">key</span>
                      : <span className="badge starting">label</span>}
                    {rules[l.name] && (
                      <span className="badge rule-chip" title="this label rewrites its values before they are stored">
                        ≈ {rules[l.name]}
                      </span>
                    )}
                  </div>
                  {l.coverage === 0 && (
                    <div className="help" style={{ fontFamily: "inherit" }}>
                      no sampled event carries this label; check the field mapping / regex
                    </div>
                  )}
                </td>
                {!allLabelsFull && (
                  <td>
                    <div className="cov"><div className="cov-bar" style={{ width: `${(l.coverage / Math.max(1, data.sampled)) * 100}%` }} /></div>
                    <small className="dim">{l.coverage} / {data.sampled}</small>
                  </td>
                )}
                <td className="num">{l.distinct}</td>
                <td>
                  {l.values.length
                    ? <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        {l.values.slice(0, 6).map((v) => (
                          <span className="chip" key={v.value} title={`${v.value} · ${v.events} events`}>
                            {fmt(v.value)} <span className="dim">({v.events})</span>
                          </span>
                        ))}
                      </span>
                    : <span className="help">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}
    <div className="panel">
      {/* "not declared as labels" earns its place: without it the absence of `path` here reads as a
          bug rather than as "it's a label, it's in the table above". */}
      <h2 style={{ marginTop: 0 }}>Fields{" "}
        <small className="dim">· not declared as labels · {data.sampled} events sampled</small></h2>
      {sorted.length === 0 ? (
        <p className="help" style={{ margin: 0, whiteSpace: "normal" }}>
          Every field observed in the sample is already a label.
        </p>
      ) : (
      <table>
        <thead><tr>
          <th>field</th>
          {!allFull && <th style={{ width: 180 }}>coverage</th>}
          <th className="num">distinct</th>
          <th>top values</th>
        </tr></thead>
        <tbody>
          {shown.map((f) => (
            <tr key={f.name} style={f.coverage === 0 ? { opacity: 0.55 } : undefined}>
              {/* No key/label badge here any more — nothing in this table is one. */}
              <td className="mono">
                {f.name}
                {f.coverage === 0 && (
                  <div className="help" style={{ fontFamily: "inherit" }}>not observed in the sample</div>
                )}
              </td>
              {!allFull && (
                <td>
                  <div className="cov"><div className="cov-bar" style={{ width: `${(f.coverage / Math.max(1, data.sampled)) * 100}%` }} /></div>
                  <small className="dim">{f.coverage} / {data.sampled}</small>
                </td>
              )}
              <td className="num">{f.distinct}</td>
              <td>
                {f.values.length
                  ? <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                      {f.values.slice(0, 6).map((v) => (
                        <span className="chip" key={v.value} title={`${v.value} · ${v.events} events`}>
                          {fmt(v.value)} <span className="dim">({v.events})</span>
                        </span>
                      ))}
                    </span>
                  : <span className="help">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}
      {sorted.length > FIELDS_PAGE && (
        <button style={{ marginTop: 10 }} onClick={() => setShowAll((s) => !s)}>
          {showAll ? "show fewer" : `show all ${sorted.length} fields`}
        </button>
      )}
      {sorted.length > 0 && allFull && (
        <p className="help" style={{ marginTop: 8 }}>Every field is present in all {data.sampled} sampled events.</p>
      )}
    </div>
    </>
  );
}

