import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import { ProjectKeysPanel } from "../ProjectKeys";
import { Picker, TimeAgo } from "../bits";
import { SlackPick } from "../SlackPick";
import { ServerForm } from "../../pages/McpServers";
import { VLink, ViewHead, type Ctx } from "./common";

/** MCP servers the project's agents can use. */
export function McpView({ ctx }: { ctx: Ctx }) {
  const [adding, setAdding] = useState(false);
  return (
    <>
      <ViewHead title="MCP servers" sub="External tools this project's agents can call. An agent uses the ones picked on its form.">
        {!adding && <button type="button" onClick={() => setAdding(true)}>Add MCP server</button>}
      </ViewHead>
      {adding && (
        <div style={{ marginBottom: 12 }}>
          <ServerForm project={ctx.id}
                      onSaved={() => { setAdding(false); ctx.refresh(); }}
                      onCancel={() => setAdding(false)} />
        </div>
      )}
      {ctx.mcpNames.length > 0 ? (
        <p style={{ margin: "4px 0 0" }}>
          {ctx.mcpNames.map((n) => <span key={n} className="chip mono">{n}</span>)}
        </p>
      ) : !adding && (
        <div className="empty">None in this project. Add an MCP server to give its agents tools beyond Tares.</div>
      )}
    </>
  );
}

/** Who the project's triggers deliver to: Tares agents, Slack channels and webhooks. One row per
 *  subscription, so remove is exact. */
export function SubscribersView({ ctx }: { ctx: Ctx }) {
  const [adding, setAdding] = useState<null | "webhook" | "slack">(null);
  const [subTrigger, setSubTrigger] = useState("");
  const [subUrl, setSubUrl] = useState("");
  const [subChannel, setSubChannel] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const [unsub, setUnsub] = useState<{ id: string; label: string } | null>(null);
  const triggerNames = new Set(ctx.triggers.map((t) => t.name));
  const rows = (ctx.roster?.agents ?? []).flatMap((a) =>
    a.subscriptions.filter((sub) => triggerNames.has(sub.trigger)).map((sub) => ({ a, sub })));
  const subscribe = async (url: string) => {
    const t = subTrigger || ctx.triggers[0]?.name;
    if (!t) return;
    setBusy(true); setMsg(undefined);
    try { await api.subscribe(t, url); setSubUrl(""); setSubChannel(""); setAdding(null); ctx.refresh(); }
    catch (e) { setMsg(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const noTriggers = !ctx.triggers.length;
  return (
    <>
      <ViewHead title="Subscribers" sub="Everything this project's triggers deliver to when they fire.">
        <button type="button" disabled={noTriggers}
                onClick={() => ctx.go({ kind: "agents" }, { add: "1", trigger: subTrigger || ctx.triggers[0]?.name || "" })}>Add a Tares agent</button>
        <button type="button" disabled={noTriggers} onClick={() => { setAdding(adding === "slack" ? null : "slack"); setMsg(undefined); }}>Add Slack channel</button>
        <button type="button" disabled={noTriggers} onClick={() => { setAdding(adding === "webhook" ? null : "webhook"); setMsg(undefined); }}>Add webhook</button>
      </ViewHead>
      {adding && (
        <div className="panel" style={{ marginBottom: 12 }}>
          {ctx.triggers.length > 1 && (
            <label className="field">
              <span className="lbl">trigger</span>
              <Picker value={subTrigger || ctx.triggers[0]?.name || ""} onChange={setSubTrigger}
                      ariaLabel="trigger to subscribe to" options={ctx.triggers.map((t) => t.name)} />
            </label>
          )}
          {adding === "webhook" ? (
            <label className="field">
              <span className="lbl">webhook URL</span>
              <input type="text" className="mono" autoFocus placeholder="https://your-agent.example.com/hook"
                     value={subUrl} onChange={(e) => setSubUrl(e.target.value)} />
              <span className="help">your agent's endpoint; it gets POSTed the timeline on every firing · <Link to="/connect?tab=push">what it receives</Link></span>
            </label>
          ) : (
            <div className="field">
              <span className="lbl">channel</span>
              <SlackPick value={subChannel} onChange={setSubChannel} emptyLabel="Choose a channel" />
              <span className="help">Tares posts every firing there.</span>
            </div>
          )}
          <div className="btnrow">
            <button className="primary" disabled={busy || (adding === "webhook" ? !subUrl.trim() : !subChannel.trim())}
                    onClick={() => subscribe(adding === "webhook"
                      ? subUrl.trim()
                      : `slack://channel/${subChannel}`)}>
              {busy ? "…" : "Subscribe"}
            </button>
            <button type="button" onClick={() => setAdding(null)}>Cancel</button>
          </div>
          {msg && <p className="help" style={{ margin: "6px 0 0" }}>{msg}</p>}
        </div>
      )}
      {rows.length ? (
        <table>
          <thead><tr><th>subscriber</th><th>wakes on</th><th>delivered</th><th aria-label="actions" /></tr></thead>
          <tbody>
            {rows.map(({ a, sub }) => (
              <tr key={sub.subscription_id}>
                <td>
                  {a.kind === "tares"
                    ? ctx.agentNames.includes(a.name)
                      ? <VLink v={{ kind: "agent", name: a.name }}><strong>{a.name}</strong></VLink>
                      : <Link to={`/agents/${encodeURIComponent(a.name)}`}><strong>{a.name}</strong></Link>
                    : <strong>{a.kind === "slack" ? ctx.channelLabel(a.name) : a.name}</strong>}
                  <span className="chip" style={{ marginLeft: 8 }}>
                    {a.kind === "tares" ? "Tares agent" : a.kind === "slack" ? "Slack" : "webhook"}</span>
                  {a.kind === "connected" && <span className="mono dim" style={{ marginLeft: 8 }}>{a.endpoint}</span>}
                </td>
                <td><VLink v={{ kind: "trigger", name: sub.trigger }} className="chip mono">{sub.trigger}</VLink></td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {a.delivered_ok_total
                    ? <>{a.delivered_ok_total} · last <TimeAgo ts={a.last_woken} /></>
                    : <span className="dim">none yet</span>}
                  {a.delivered_fail_total > 0 && <span style={{ color: "var(--err)" }}> · {a.delivered_fail_total} failed</span>}
                </td>
                <td style={{ textAlign: "right" }}>
                  <button className="danger"
                          onClick={() => setUnsub({ id: sub.subscription_id, label: a.kind === "slack" ? ctx.channelLabel(a.name) : a.name })}>
                    remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : !adding && (
        <div className="empty">
          {noTriggers
            ? <>Nothing to subscribe to yet. <VLink v={{ kind: "triggers" }} extra={{ add: "1" }}>Add a trigger</VLink> first.</>
            : "Nobody is subscribed to this project's triggers yet. Add an agent, a Slack channel or a webhook."}
        </div>
      )}
      {unsub && (
        <ConfirmDialog title={`Stop delivering to ${unsub.label}?`}
          message="This trigger stops delivering to it; anything else it is subscribed to is unaffected, and its delivery history is kept."
          confirmLabel="Remove" danger
          onConfirm={async () => {
            const u = unsub; setUnsub(null);
            try { await api.unsubscribe(u.id); ctx.refresh(); }
            catch (e) { ctx.fail(e); }
          }}
          onCancel={() => setUnsub(null)} />
      )}
    </>
  );
}

export function KeysView({ ctx }: { ctx: Ctx }) {
  return <div className="pv-keys"><ProjectKeysPanel project={ctx.id} /></div>;
}

/** What was done to the project and when. */
export function HistoryView({ ctx }: { ctx: Ctx }) {
  const log = ctx.s.log ?? [];
  return (
    <>
      <ViewHead title="Change history" sub={<>Created <TimeAgo ts={ctx.s.created_at} />.</>} />
      {log.length ? (
        <table>
          <tbody>
            {log.map((l, i) => (
              <tr key={i}>
                <td style={{ whiteSpace: "nowrap", width: 90, verticalAlign: "top" }}><TimeAgo ts={l.at} /></td>
                <td className="mono" style={{ whiteSpace: "nowrap", width: 90, verticalAlign: "top" }}>{l.action}</td>
                <td className="help" style={{ overflowWrap: "anywhere" }}>{l.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <div className="empty">No changes recorded yet. Edits to the project show up here.</div>}
    </>
  );
}
