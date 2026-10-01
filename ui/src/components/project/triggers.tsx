import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import TriggerEditor from "../TriggerEditor";
import { TimeAgo, conditionText } from "../bits";
import type { Trigger } from "../../types";
import { Facts, NotHere, VLink, ViewHead, type Ctx } from "./common";

// the newest firing in the recent log, else what the project summary last saw
const lastFired = (ctx: Ctx, name: string) =>
  ctx.dispatches?.find((d) => d.trigger === name)?.fired_at
  ?? ctx.s.triggers?.find((t) => t.name === name)?.last_fired ?? null;

/** Triggers: each with its condition, what it wakes and when it last fired. */
export function TriggersView({ ctx }: { ctx: Ctx }) {
  const adding = ctx.params.get("add") === "1";
  const close = () => ctx.go({ kind: "triggers" }, undefined, true);
  return (
    <>
      <ViewHead title="Triggers" sub="A trigger fires when its condition trips on this project's events, and wakes the agents on it.">
        {!adding && <button type="button" className="primary"
                            onClick={() => ctx.go({ kind: "triggers" }, { add: "1" }, true)}>Add trigger</button>}
      </ViewHead>
      {adding && (
        <div style={{ marginBottom: 12 }}>
          <TriggerEditor project={ctx.id}
                         onSaved={() => { ctx.refresh(); close(); }}
                         onCancel={close} />
        </div>
      )}
      {!ctx.triggersLoaded ? <div className="dim">loading…</div>
        : ctx.triggers.length ? (
          <table>
            <thead><tr><th>trigger</th><th>fires when</th><th>wakes</th><th>last fired</th></tr></thead>
            <tbody>
              {ctx.triggers.map((t) => {
                const wakes = ctx.agents.filter((a) => a.trigger === t.name);
                return (
                  <tr key={t.name} className="clickable" onClick={() => ctx.go({ kind: "trigger", name: t.name })}>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <VLink v={{ kind: "trigger", name: t.name }} className="mono">{t.name}</VLink>
                      {t.paused && <span className="badge paused" style={{ marginLeft: 8 }}>paused</span>}
                    </td>
                    <td className="mono">{conditionText(t.condition)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {wakes.length
                        ? wakes.map((a) => <VLink key={a.name} v={{ kind: "agent", name: a.name }} className="chip mono">{a.name}</VLink>)
                        : <span className="help">no agent</span>}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {lastFired(ctx, t.name) ? <TimeAgo ts={lastFired(ctx, t.name)} /> : <span className="dim">never</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : !adding && (
          <div className="empty">
            No triggers yet. Add a trigger to say which events matter; agents run when it fires.
          </div>
        )}
    </>
  );
}

/** One trigger: its configuration, what it wakes, and its recent firings. */
export function TriggerView({ ctx, name }: { ctx: Ctx; name: string }) {
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const t = ctx.triggers.find((x) => x.name === name);
  if (!ctx.triggersLoaded) return <div className="dim">loading…</div>;
  if (!t) return <NotHere what="trigger" name={name} back={{ kind: "triggers" }} />;
  const wakes = ctx.agents.filter((a) => a.trigger === t.name);
  // who else it delivers to: Slack channels and webhooks subscribed to it
  const others = (ctx.roster?.agents ?? []).filter((a) => a.kind !== "tares")
    .flatMap((a) => a.subscriptions.filter((sub) => sub.trigger === t.name).map((sub) => ({ a, sub })));
  const firings = (ctx.dispatches ?? []).filter((d) => d.trigger === t.name).slice(0, 20);
  const togglePause = async () => {
    try { if (t.paused) await api.resumeTrigger(t.name); else await api.pauseTrigger(t.name); ctx.refresh(); }
    catch (e) { ctx.fail(e); }
  };
  return (
    <>
      <ViewHead title={<><span className="mono">{t.name}</span>{t.paused && <span className="badge paused" style={{ marginLeft: 10 }}>paused</span>}</>}
                sub={t.paused ? "Paused: it is not evaluated and does not fire." : "Fires when the condition trips, and wakes every agent on it."}>
        {!editing && <>
          <button onClick={() => ctx.go({ kind: "agents" }, { add: "1", trigger: t.name })}>Add agent</button>
          <button onClick={togglePause}>{t.paused ? "Resume" : "Pause"}</button>
          <button className="primary" onClick={() => setEditing(true)}>Edit</button>
          <button className="danger" onClick={() => setConfirmDel(true)}>Delete</button>
        </>}
      </ViewHead>
      {editing ? (
        <TriggerEditor initial={t}
                       onSaved={() => { setEditing(false); ctx.refresh(); }}
                       onCancel={() => setEditing(false)} />
      ) : <TriggerConfig t={t} ctx={ctx} />}

      <h3 style={{ margin: "20px 0 8px" }}>Wakes</h3>
      {wakes.length || others.length ? (
        <p style={{ margin: 0 }}>
          {wakes.map((a) => (
            <VLink key={a.name} v={{ kind: "agent", name: a.name }} className="chip mono">
              {a.name}{!a.enabled && <span className="help"> · off</span>}</VLink>))}
          {others.map(({ a, sub }) => (
            <span key={sub.subscription_id} className="chip mono">
              {a.kind === "slack" ? `Slack ${ctx.channelLabel(a.name)}` : a.name}</span>))}
        </p>
      ) : (
        <p className="help" style={{ margin: 0 }}>
          Nothing yet: it fires, but nobody is woken. Add an agent to act on it, or subscribe a
          channel under <VLink v={{ kind: "settings", name: "subscribers" }}>Settings, Subscribers</VLink>.
        </p>
      )}

      <h3 style={{ margin: "20px 0 8px" }}>Recent firings</h3>
      {firings.length ? (
        <table>
          <thead><tr><th>when</th><th>entity</th><th>delivered</th><th aria-label="open" /></tr></thead>
          <tbody>
            {firings.map((d) => (
              <tr key={d.dispatch_id}>
                <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={d.fired_at} /></td>
                <td className="mono">{d.kind === "schedule" ? <span className="help">all, on its schedule</span> : d.key}</td>
                <td>
                  {d.subscribers
                    ? <>{d.delivered} of {d.subscribers}</>
                    : <span className="help">nobody subscribed</span>}
                  {d.error && <span style={{ color: "var(--err)" }}> · {d.error}</span>}
                </td>
                <td style={{ textAlign: "right" }}><Link to={`/dispatches/${encodeURIComponent(d.dispatch_id)}`}>the firing</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="help" style={{ margin: 0 }}>It has not fired recently. Firings show up here, and on Activity with what they led to.</p>}

      {confirmDel && (
        <ConfirmDialog title={`Delete trigger ${t.name}?`}
          message="It leaves every project and stops waking agents. The agents it woke stay; a handoff still starts them, and you can give them another trigger. This can't be undone."
          confirmLabel="Delete" danger
          onConfirm={async () => {
            setConfirmDel(false);
            try { await api.deleteTrigger(t.name); ctx.refresh(); ctx.go({ kind: "triggers" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }}
          onCancel={() => setConfirmDel(false)} />
      )}
    </>
  );
}

function TriggerConfig({ t, ctx }: { t: Trigger; ctx: Ctx }) {
  const fired = lastFired(ctx, t.name);
  return (
    <Facts rows={[
      ["in plain words", t.description ? <span>{t.description}</span> : <span className="help">none; the project page says it from the condition</span>],
      ["sources", (t.sources ?? []).map((x) => (
        ctx.mySources.some((m) => m.name === x)
          ? <VLink key={x} v={{ kind: "source", name: x }} className="chip mono">{x}</VLink>
          : <Link key={x} to={`/sources/${encodeURIComponent(x)}`} className="chip mono">{x}</Link>))],
      ["filters", (t.filters ?? []).length
        ? (t.filters ?? []).map((f, i) => <span className="chip mono" key={i}>{f.field} {f.op} {String(f.value)}</span>)
        : <span className="help">none; every event of these sources counts</span>],
      ["entity label", t.key_field ? <span className="mono">{t.key_field}</span> : <span className="help">the first source's main label</span>],
      ["condition", <span className="mono">{conditionText(t.condition)}</span>],
      ["context window", <><span className="mono">{String(t.emit?.context_window ?? "15m")}</span>
        <span className="help"> · timeline the woken agent receives</span></>],
      ["cooldown", t.condition.every ? <span className="mono">none (fires on its schedule)</span>
        : <><span className="mono">{t.cooldown}</span><span className="help"> · minimum gap between firings per entity</span></>],
      ["last fired", fired ? <TimeAgo ts={fired} /> : <span className="dim">never</span>],
    ]} />
  );
}
