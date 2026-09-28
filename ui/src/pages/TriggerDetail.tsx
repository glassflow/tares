import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api, type SlackChannels } from "../api";
import type { AgentInfo, DispatchDetail } from "../types";
import ConfirmDialog from "../components/ConfirmDialog";
import ProjectBadge from "../components/ProjectBadge";
import TriggerEditor from "../components/TriggerEditor";
import { ErrorState, Picker, TimeAgo, conditionText, usePolling } from "../components/bits";

// The home of one trigger: condition, where it delivers (wire more here), recent firings.
// Read-only by default; Edit swaps in the editor in place (?edit=1 opens it directly).
export default function TriggerDetail() {
  const { name = "" } = useParams();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [editing, setEditing] = useState(params.get("edit") === "1");
  const [confirmDel, setConfirmDel] = useState(false);
  // The row awaiting an unsubscribe confirmation, or null. Holds the row rather than a
  // boolean: the dialog names what it is about to disconnect.
  const [unsub, setUnsub] = useState<AgentInfo | null>(null);
  const [delErr, setDelErr] = useState<string>();
  const { data: triggers, error, reload } = usePolling(() => api.triggers(), 10000);
  // Both errors are kept, not dropped: `agents` decides what the DELETE dialog tells you is at
  // stake, and a failed load would otherwise silently read as "nothing depends on this".
  const { data: agents, error: agentsError, reload: reloadAgents } = usePolling(() => api.agents(), 10000);
  const { data: dispatches, error: dispatchesError } = usePolling(() => api.dispatches(100), 10000);
  const { data: views } = usePolling(() => api.views(), 30000);
  // Per-firing recipients with outcomes: the list endpoint carries only counts, the detail has
  // names; five small fetches for the five rows shown.
  const [details, setDetails] = useState<Record<string, DispatchDetail>>({});
  useEffect(() => {
    let live = true;
    const ids = (dispatches ?? []).filter((d) => d.trigger === name).slice(0, 5).map((d) => d.dispatch_id);
    Promise.all(ids.map((id) => api.dispatch(id).catch(() => null))).then((rows) => {
      if (!live) return;
      const m: Record<string, DispatchDetail> = {};
      for (const r of rows) if (r) m[r.dispatch_id] = r;
      setDetails(m);
    });
    return () => { live = false; };
  }, [dispatches, name]);
  // Which "add a subscriber" form is open, if any. The forms are closed by default: this page
  // is first a view of the running system (who receives this trigger, is it healthy, what fired),
  // and setup gets in the way when it is always on screen (TR-138).
  const [adding, setAdding] = useState<null | "webhook" | "slack">(null);
  const [url, setUrl] = useState("");
  const [channel, setChannel] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const { data: caps } = usePolling(() => api.capabilities(), 60000);
  // Fetched once, not polled: a workspace's channel list doesn't move while you're on this page.
  // It does move when you invite the bot to a channel in Slack, which is what Refresh below is for.
  const [slack, setSlack] = useState<SlackChannels>();
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => {
    let live = true;
    api.slackChannels()
      .then((r) => { if (live) setSlack(r); })
      .catch(() => { if (live) setSlack({ channels: [], reason: "error" }); });
    return () => { live = false; };
  }, []);
  // Re-lists channels without reloading the page — you're mid-way through wiring up a trigger and
  // a reload would throw the rest of that away. A failed refresh keeps the list we already have,
  // so trying is never worse than not trying; a *successful* one is followed even when it comes
  // back worse (a token revoked meanwhile drops us to the text box, which is the truth).
  const refreshChannels = async () => {
    setRefreshing(true);
    try { setSlack(await api.slackChannels()); } catch { /* keep the current list */ }
    setRefreshing(false);
  };
  const channels = slack?.reason === null ? slack.channels : [];

  if (error) return <div className="alert error">{error}</div>;
  if (!triggers) return <div className="dim">loading…</div>;
  const trigger = triggers.find((t) => t.name === name);
  if (!trigger) {
    return (
      <div className="alert error">
        no trigger named <span className="mono">{name}</span> · see <Link to="/triggers">Triggers</Link>
      </div>
    );
  }
  const wired = (agents?.agents ?? []).filter((a) => a.triggers.includes(name));
  /** A Slack row's identity is `#C0BNV121CRX` — Slack's id, which no human recognises. The channel
   *  list is already loaded for the picker, so resolve it to `#alerts` when we can; falling back to
   *  the id matters, because the bot may since have been removed from the channel. */
  const channelLabel = (raw: string) => {
    const id = raw.replace(/^#/, "");
    const hit = (slack?.channels ?? []).find((c) => c.id === id);
    return hit ? (hit.is_private ? `🔒 ${hit.name}` : `#${hit.name}`) : raw;
  };
  const firings = (dispatches ?? []).filter((d) => d.trigger === name).slice(0, 10);

  return (
    <>
      <div className="pagehead">
        <div>
          <h1><span className="mono">{trigger.name}</span></h1>
        </div>
        {!editing && (
          <span className="btnrow">
            <button className="primary" onClick={() => setEditing(true)}>Edit</button>
            <button className="danger" onClick={() => setConfirmDel(true)}>Delete</button>
          </span>
        )}
      </div>

      {delErr && <div className="alert error">{delErr}</div>}

      {editing && (
        <TriggerEditor initial={trigger}
                       onSaved={() => { setEditing(false); reload(); }}
                       onCancel={() => setEditing(false)} />
      )}

      {!editing && (
        <div className="panel">
          <table>
            <tbody>
              <tr><td className="help" style={{ width: 150 }}>watches</td>
                  <td><Link to={`/views/${encodeURIComponent(trigger.view)}`} className="mono">{trigger.view}</Link></td></tr>
              {trigger.owned_by && (
                <tr><td className="help">part of</td>
                    <td><ProjectBadge ownedBy={trigger.owned_by} customized={trigger.customized} compact /></td></tr>
              )}
              <tr><td className="help" style={{ width: 150 }}>condition</td>
                  <td className="mono">
                    {conditionText(trigger.condition)}
                  </td></tr>
              <tr><td className="help">context window</td>
                  <td className="mono">{String(trigger.emit?.context_window ?? "15m")}</td></tr>
              <tr><td className="help">cooldown</td>
                  <td className="mono">{trigger.cooldown}</td></tr>
            </tbody>
          </table>
        </div>
      )}

      <div className="pagehead" style={{ marginTop: 20 }}>
        <h2 style={{ margin: 0 }}>Where this trigger delivers</h2>
        <span className="btnrow">
          <button type="button" onClick={() => { setAdding(adding === "webhook" ? null : "webhook"); setMsg(undefined); }}>
            Add webhook
          </button>
          <button type="button" onClick={() => { setAdding(adding === "slack" ? null : "slack"); setMsg(undefined); }}>
            Add Slack channel
          </button>
          <Link className="btn primary" to={`/agents/new?trigger=${encodeURIComponent(name)}`}>
            Add a Tares agent
          </Link>
        </span>
      </div>
      {wired.length > 0 ? (
        <table style={{ marginBottom: 10 }}>
          <thead><tr><th>subscriber</th><th>delivered</th><th>status</th><th aria-label="actions" /></tr></thead>
          <tbody>
            {wired.map((a) => (
              <tr key={a.name}>
                <td>
                  {a.kind === "tares"
                    ? <Link to={`/agents/${encodeURIComponent(a.name)}`}><strong>{a.name}</strong></Link>
                    : a.kind === "slack"
                    // A raw C0BNV121CRX is the identity Slack uses, not one a human recognises. The
                    // channel list is already loaded on this page, so resolve it when we can and fall
                    // back to the id when the bot has since been removed from the channel.
                    ? <strong>{channelLabel(a.name)}</strong>
                    : <Link to={`/agents?agent=${encodeURIComponent(a.name)}`}><strong>{a.name}</strong></Link>}
                  <span className="chip" style={{ marginLeft: 8 }}>
                    {a.kind === "tares" ? "Tares agent" : a.kind === "slack" ? "Slack" : "webhook"}</span>
                  {a.kind === "connected" && <span className="mono dim" style={{ marginLeft: 8 }}>{a.endpoint}</span>}
                </td>
                <td style={{ whiteSpace: "nowrap" }}
                    title={a.delivered_fail_total ? `${a.delivered_fail_total} failed all time` : undefined}>
                  {a.delivered_ok_total
                    ? <>{a.delivered_ok_total} · last <TimeAgo ts={a.last_woken} /></>
                    : <span className="dim">none yet</span>}
                  {a.delivered_fail_total > 0 && <span style={{ color: "var(--err)" }}> · {a.delivered_fail_total} failed</span>}
                </td>
                <td>
                  {a.pending
                    ? <span className="badge starting">running</span>
                    : a.unhealthy
                    ? <span className="badge error" title={a.last_error ?? "last delivery failed"}>failing{a.last_error ? `: ${a.last_error}` : ""}</span>
                    : a.delivered_ok_total > 0 ? <span className="badge ok">ok</span> : <span className="dim">—</span>}
                </td>
                <td>
                  <div className="btnrow" style={{ justifyContent: "flex-end" }}>
                    <button className="danger" disabled={busy}
                            onClick={() => setUnsub(a)}>remove</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="help" style={{ whiteSpace: "normal" }}>
          none yet; add a Tares agent, a webhook for your own agent, or a Slack channel above
        </p>
      )}
      {adding === "webhook" && (<div className="panel">
      <p className="help" style={{ margin: "0 0 6px", whiteSpace: "normal" }}>
        Connect your own agent: its webhook gets POSTed the timeline on every firing.
        What your endpoint receives and how to acknowledge:{" "}
        <Link to="/connect?tab=push">Connect → Webhook (push)</Link>.
      </p>
      <div className="btnrow" style={{ alignItems: "center", maxWidth: 720 }}>
        <input type="text" className="mono" style={{ flex: 1 }} autoFocus
               placeholder="https://your-agent.example.com/hook" value={url}
               onChange={(e) => setUrl(e.target.value)} />
        <button className="primary" disabled={!url.trim() || busy} onClick={async () => {
          setBusy(true); setMsg(undefined);
          try {
            const r = await api.subscribe(name, url.trim());
            setMsg(`✓ subscribed (${r.subscription_id})`);
            setUrl(""); setAdding(null); reloadAgents();
          } catch (e) { setMsg(`⚠️ ${String((e as Error).message ?? e)}`); }
          setBusy(false);
        }}>Subscribe</button>
        <button type="button" onClick={() => setAdding(null)}>Cancel</button>
      </div>
      {msg && <p className="help" style={{ margin: "6px 0 0" }}>{msg}</p>}
      </div>)}

      {/* A Slack channel is the same thing as the webhook above — one more subscription row —
          so it lives here rather than in a Slack-shaped corner of the app. */}
      {adding === "slack" && (<div className="panel">
      <p className="help" style={{ margin: "0 0 6px" }}>
        Post every firing to a Slack channel · retried and logged like any other delivery:
      </p>
      <div className="btnrow" style={{ alignItems: "center", maxWidth: 720 }}>
        {channels.length > 0 ? (
          // One flex child, not two: .btnrow wraps, and a bare Refresh button beside the picker
          // would drop the Subscribe button onto its own line at narrow widths.
          <div style={{ flex: 1, minWidth: 0, display: "flex", gap: 8, alignItems: "center" }}>
            {/* Shows the name but submits the ID: a channel renamed later keeps its ID, so the
                subscription survives the rename instead of quietly pointing at nothing. */}
            <Picker value={channel} onChange={setChannel} ariaLabel="Slack channel"
                    style={{ flex: 1, minWidth: 0 }}
                    options={channels.map((c) => c.id)}
                    labels={{ "": "choose a channel…",
                              ...Object.fromEntries(channels.map(
                                (c) => [c.id, c.is_private ? `🔒 ${c.name}` : `#${c.name}`])) }} />
            <button type="button" disabled={refreshing} onClick={refreshChannels}
                    style={{ flexShrink: 0 }}>
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        ) : (
          <input type="text" className="mono" style={{ flex: 1 }}
                 placeholder="C0123456789 (the channel ID, from Slack's “Copy link”) or its lowercase name"
                 value={channel} onChange={(e) => setChannel(e.target.value)} />
        )}
        <button className="primary" disabled={!channel.trim() || busy} onClick={async () => {
          setBusy(true); setMsg(undefined);
          // The picker already hands us a bare ID; only typed-in text can carry a leading "#".
          const target = channels.length > 0 ? channel : channel.trim().replace(/^#/, "");
          try {
            const r = await api.subscribe(name, `slack://channel/${target}`);
            setMsg(`✓ subscribed (${r.subscription_id})`);
            setChannel(""); setAdding(null); reloadAgents();
          } catch (e) { setMsg(`⚠️ ${String((e as Error).message ?? e)}`); }
          setBusy(false);
        }}>Subscribe channel</button>
        <button type="button" onClick={() => setAdding(null)}>Cancel</button>
      </div>
      {/* The list only contains channels the bot has been invited to, so a missing one almost
          always means exactly this; a 10-second fix in Slack, but only if we say so. */}
      {channels.length > 0 && (
        <p className="help" style={{ margin: "4px 0", whiteSpace: "normal" }}>
          not seeing a channel? add the bot to it in Slack, then hit Refresh.
        </p>
      )}
      {slack?.reason === "missing_scope" && (
        <p className="help" style={{ margin: "4px 0", whiteSpace: "normal" }}>
          this Slack token predates the <span className="mono">channels:read</span> and{" "}
          <span className="mono">groups:read</span> scopes; reinstall the Slack app to pick a
          channel from a list instead of typing one.
        </p>
      )}
      {caps && caps.slack_configured === false && (
        <p className="help" style={{ margin: "4px 0", whiteSpace: "normal" }}>
          no Slack bot token is configured yet; add one under{" "}
          <Link to="/settings">Settings</Link>, and invite the bot to the channel.
        </p>
      )}
      {msg && <p className="help" style={{ margin: "6px 0 0" }}>{msg}</p>}
      </div>)}
      {/* Result of a remove (the forms show their own messages while open). */}
      {adding === null && msg && <p className="help">{msg}</p>}

      <h2>Recent firings</h2>
      {dispatchesError && <ErrorState error={dispatchesError} what="recent firings" />}
      {!dispatchesError && firings.length === 0 && <p className="help">none yet</p>}
      {firings.length > 0 && (
        <>
        <table>
          <thead><tr><th style={{ width: 110 }}>fired</th>
            <th>{(views ?? []).find((v) => v.name === trigger.view)?.key_field || "entity"}</th>
            <th>delivered to</th></tr></thead>
          <tbody>
            {firings.slice(0, 5).map((d) => {
              const dvs = details[d.dispatch_id]?.deliveries;
              return (
                <tr key={d.dispatch_id} className="clickable"
                    onClick={() => nav(`/dispatches/${encodeURIComponent(d.dispatch_id)}`)}>
                  <td style={{ whiteSpace: "nowrap" }}><Link to={`/dispatches/${encodeURIComponent(d.dispatch_id)}`}><TimeAgo ts={d.fired_at} /></Link></td>
                  <td className="mono">{d.key}</td>
                  <td>
                    {dvs === undefined
                      ? (d.subscribers === 0 ? <span className="dim">nobody was subscribed</span> : <span className="dim">…</span>)
                      : dvs.length === 0
                      ? <span className="dim">nobody was subscribed</span>
                      : dvs.map((dv, i) => (
                          <span key={i} className="chip mono"
                                title={dv.ok === false ? (dv.error ?? "delivery failed") : dv.ok === null ? "still running" : "delivered"}
                                style={{ marginRight: 4,
                                         color: dv.ok === false ? "var(--err)" : dv.ok === null ? undefined : "var(--ok, #2e7d43)" }}>
                            {dv.kind === "slack" ? channelLabel(dv.agent) : dv.agent}
                          </span>
                        ))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {firings.length > 5 && (
          <p className="help">the last 5 of {firings.length} recent · every firing is on <Link to="/firings">Firings</Link></p>
        )}
        </>
      )}

      {unsub && (
        <ConfirmDialog
          title={`Stop delivering to ${unsub.kind === "slack" ? channelLabel(unsub.name) : unsub.name}?`}
          // Names the trigger, because a subscriber can be wired to several: this removes ONE
          // subscription, not the subscriber.
          message={`This trigger stops delivering to it. Any other trigger it is subscribed to is `
                   + `unaffected, and its delivery history is kept. You can wire it up again.`}
          confirmLabel="Remove"
          danger
          onCancel={() => setUnsub(null)}
          onConfirm={async () => {
            // A subscriber may hold subscriptions to several triggers — remove only this trigger's.
            const sub = unsub.subscriptions.find((x) => x.trigger === name);
            setUnsub(null);
            if (!sub) return;
            setBusy(true);
            try { await api.unsubscribe(sub.subscription_id); reloadAgents(); }
            catch (e) { setMsg(`⚠️ ${String((e as Error).message ?? e)}`); }
            setBusy(false);
          }}
        />
      )}

      {confirmDel && (
        <ConfirmDialog
          title={`Delete trigger ${trigger.name}?`}
          message={agentsError
            // Never reassure from a failed load. "No agents use this" and "we couldn't find out"
            // are different facts, and only one of them is safe to delete on.
            ? `Couldn’t check what this trigger delivers to (${agentsError}); deleting may break more than is shown here. This can’t be undone.`
            : wired.length
              ? `${wired.length} subscriber(s) receive this trigger and will stop. This can't be undone.`
              : "This stops the condition from being evaluated. This can't be undone."}
          confirmLabel="Delete"
          danger
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setDelErr(undefined);
            try { await api.deleteTrigger(trigger.name); nav("/triggers"); }
            catch (e) { setDelErr(String((e as Error).message ?? e)); setConfirmDel(false); }
          }}
        />
      )}
    </>
  );
}
