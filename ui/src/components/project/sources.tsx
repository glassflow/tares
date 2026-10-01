import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import IngestEndpoint from "../IngestEndpoint";
import { Combo, ErrorState, TimeAgo, usePolling } from "../bits";
import type { ConnectorSpec, Source } from "../../types";
import { EventsTable, Facts, NotHere, SourceBadge, VLink, ViewHead, type Ctx } from "./common";

/** Take a source out of the project, after asking; the source itself stays. */
function useRemoveSource(ctx: Ctx, after?: () => void) {
  const [name, setName] = useState<string | null>(null);
  const dialog = name && (
    <ConfirmDialog title={`Remove ${name} from this project?`}
      message="The source stays connected and keeps ingesting; it is only no longer part of this project. A trigger of this project that reads it has to drop it first."
      confirmLabel="Remove from project"
      onConfirm={async () => {
        const n = name; setName(null);
        try { await api.removeProjectSource(ctx.id, n); ctx.refresh(); after?.(); }
        catch (e) { ctx.fail(e); }
      }}
      onCancel={() => setName(null)} />
  );
  return { ask: setName, dialog };
}

/** Sources: every source of the project, with adding an existing one or connecting a new one. */
export function SourcesView({ ctx }: { ctx: Ctx }) {
  const adding = ctx.params.get("add") === "1";
  const setAdding = (open: boolean) => ctx.go({ kind: "sources" }, open ? { add: "1" } : undefined, true);
  const [pick, setPick] = useState("");
  const remove = useRemoveSource(ctx);
  const notMine = (ctx.sources ?? []).filter((x) => !ctx.mySources.some((m) => m.name === x.name)).map((x) => x.name);
  const addExisting = async (name: string) => {
    try { await api.addProjectSource(ctx.id, name); setPick(""); setAdding(false); ctx.refresh(); }
    catch (e) { ctx.fail(e); }
  };
  return (
    <>
      <ViewHead title="Sources" sub="Where this project's events come from. A source can be shared by several projects.">
        <button type="button" onClick={() => { setAdding(!adding); setPick(""); }}>Add existing source</button>
        <Link className="btn" to={`/sources/new?project=${encodeURIComponent(ctx.id)}`}>Connect a new source</Link>
      </ViewHead>
      {adding && (
        <div className="panel" style={{ marginBottom: 12 }}>
          {notMine.length ? (
            <div className="field" style={{ maxWidth: 360, margin: 0 }}>
              <span className="lbl">source</span>
              <Combo value={pick} options={notMine} placeholder="pick a source to add…"
                     onChange={(v) => (notMine.includes(v) ? addExisting(v) : setPick(v))} />
              <span className="help">sources are shared: it stays in its other projects too</span>
            </div>
          ) : <p className="help" style={{ margin: 0 }}>Every source is already in this project. Connect a new one instead.</p>}
        </div>
      )}
      {ctx.sources === undefined ? <div className="dim">loading…</div>
        : ctx.mySources.length ? (
          <table>
            <thead><tr><th>source</th><th>type</th><th>status</th><th className="num">events</th><th>last ingest</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {ctx.mySources.map((x) => (
                <tr key={x.name} className="clickable" onClick={() => ctx.go({ kind: "source", name: x.name })}>
                  <td><VLink v={{ kind: "source", name: x.name }} className="mono">{x.name}</VLink></td>
                  <td><span className="chip">{x.connector}</span></td>
                  <td><SourceBadge x={x} /></td>
                  <td className="num">{(x.health?.events_total ?? 0).toLocaleString()}</td>
                  <td>{x.health?.last_ingest ? <TimeAgo ts={x.health.last_ingest} /> : <span className="dim">never</span>}</td>
                  <td style={{ textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                    <button type="button" onClick={() => remove.ask(x.name)} title="take it out of this project; the source stays">remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : !adding && (
          <div className="empty">
            No sources in this project yet. Connect a new source, or add one you already have with
            Add existing source.
          </div>
        )}
      {remove.dialog}
    </>
  );
}

/** One source: its health, where it reads from, its labels, what reads it here, recent events. */
export function SourceView({ ctx, name }: { ctx: Ctx; name: string }) {
  const navigate = useNavigate();
  const x = ctx.mySources.find((m) => m.name === name);
  const remove = useRemoveSource(ctx, () => ctx.go({ kind: "sources" }, undefined, true));
  if (ctx.sources === undefined) return <div className="dim">loading…</div>;
  if (!x) return <NotHere what="source" name={name} back={{ kind: "sources" }} />;
  const spec = ctx.specs?.[x.connector];
  const others = ctx.otherProjects(x).map(ctx.projectName);
  const readBy = ctx.triggers.filter((t) => (t.sources ?? []).includes(name));
  const labels = (x.config?.labels ?? []) as Array<{ name: string; field?: string; const?: string; primary?: boolean }>;
  return (
    <>
      <ViewHead title={<span className="mono">{x.name}</span>}
                sub={<>{spec?.label ?? x.connector}{others.length > 0 && <> · also used by {others.join(", ")}</>}</>}>
        <button className="primary" onClick={() => navigate(`/sources/${encodeURIComponent(x.name)}`)}>Configure</button>
        <button type="button" onClick={() => remove.ask(x.name)}>Remove from project</button>
      </ViewHead>
      {x.health?.last_error && <div className="alert error">{x.health.last_error}</div>}
      <Facts rows={[
        ["status", <SourceBadge x={x} />],
        ["events", <>{(x.health?.events_total ?? 0).toLocaleString()} {x.health?.last_ingest && <> · last one <TimeAgo ts={x.health.last_ingest} /></>}</>],
        ["read by", readBy.length
          ? readBy.map((t) => <VLink key={t.name} v={{ kind: "trigger", name: t.name }} className="chip mono">{t.name}</VLink>)
          : <span className="help">no trigger of this project reads it yet</span>],
        ["labels", labels.length
          ? labels.map((l) => (
              <span key={l.name} className="chip mono" title={l.field ? `from ${l.field}` : `always ${String(l.const ?? "")}`}>
                {l.name}{l.primary && <span className="help"> · key</span>}
              </span>))
          : <span className="help">none declared; Configure to add them</span>],
      ]} />
      <SourceTarget x={x} spec={spec} />
      <h3 style={{ margin: "20px 0 8px" }}>Recent events</h3>
      <SourceEvents name={x.name} />
      {remove.dialog}
    </>
  );
}

/** What a producer needs: the ingest endpoint of a push source, the polled target of a poll one. */
function SourceTarget({ x, spec }: { x: Source; spec?: ConnectorSpec }) {
  if (spec?.mode === "push") return <div style={{ marginTop: 14 }}><IngestEndpoint source={x} /></div>;
  const shown = (spec?.fields ?? [])
    .filter((f) => !f.secret && (f.type === "string" || f.type === "number") && x.config?.[f.name] !== undefined && x.config?.[f.name] !== "")
    .map((f) => ({ name: f.name, value: String(x.config[f.name]) }));
  return (
    <table className="pv-facts" style={{ marginTop: 14 }}>
      <tbody>
        {shown.map((f) => (
          <tr key={f.name}><td className="help">{f.name}</td><td className="mono" style={{ wordBreak: "break-all" }}>{f.value}</td></tr>
        ))}
        <tr><td className="help">polls every</td><td className="mono">{x.poll}</td></tr>
      </tbody>
    </table>
  );
}

function SourceEvents({ name }: { name: string }) {
  const { data, error, reload } = usePolling(() => api.sourceEvents(name, 30), 10000);
  if (error && !data) return <ErrorState error={error} what="its events" onRetry={reload} />;
  if (!data) return <div className="dim">loading…</div>;
  return data.length ? <EventsTable events={data} showSource={false} />
    : <div className="empty">Nothing ingested yet. Events show up here as soon as the source receives or polls one.</div>;
}

/** Events: the newest stored rows across the project's sources, merged on the client; no
 *  filtering, no label unpacking, so it stays fast however big the sources get. */
export function EventsView({ ctx }: { ctx: Ctx }) {
  const names = ctx.mySources.map((x) => x.name);
  const { data: recent } = usePolling(
    () => Promise.all(names.map((n) => api.sourceEvents(n, 30).catch(() => [])))
      .then((lists) => lists.flat().sort((a, b) => (b.ingest_time > a.ingest_time ? 1 : -1)).slice(0, 50)),
    10000);
  return (
    <>
      <ViewHead title="Events" sub="The newest events across this project's sources." />
      {recent === undefined || ctx.sources === undefined ? <div className="dim">loading…</div>
        : recent.length ? <EventsTable events={recent} />
        : <div className="empty">
            {names.length
              ? "Nothing ingested yet across this project's sources. Events show up here as soon as one arrives."
              : <>This project has no sources yet. <VLink v={{ kind: "sources" }} extra={{ add: "1" }}>Add one</VLink> to see its events here.</>}
          </div>}
    </>
  );
}
