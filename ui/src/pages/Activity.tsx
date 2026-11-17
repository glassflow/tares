import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { Search } from "../components/icons";
import { Picker, TimeAgo, usePolling } from "../components/bits";

// Reads: the one feed left on this page (Connect moved to pages/Connect.tsx). Trigger dispatches moved
// to Deliveries (with the subscriber roster they belong to); the connected-agent roster went the same
// way (TR-137).
export default function AgentActivity() {
  return (
    <>
      <h1>Reads</h1>
      <p className="subtitle">Every read agents made over MCP or the API, newest first.</p>
      <Queries />
    </>
  );
}

// Exported so the Agents page can show connected agents (only="connected") beneath Tares agents.
export function AgentsRoster({ only }: { only?: "connected" | "tares" }) {
  const nav = useNavigate();
  const { data, error } = usePolling(() => api.agents(), 10000);
  const [params] = useSearchParams();
  const [open, setOpen] = useState<string | undefined>(params.get("agent") ?? undefined);
  const [err, setErr] = useState<string>();

  if (error) return <div className="alert error">{error}</div>;
  if (!data) return <div className="dim">loading…</div>;
  // "connected" means everything that isn't a Tares agent — an external webhook or a Slack
  // channel. Both are things the operator wired to a trigger from outside; splitting them into a
  // third table would say less than the badge on the row does.
  const agents = only
    ? data.agents.filter((a) => (only === "connected" ? a.kind !== "tares" : a.kind === only))
    : data.agents;
  if (!agents.length) {
    return (
      <div className="empty">
        no external agents connected; connect one, or a Slack channel, from a{" "}
        <Link to="/triggers">trigger's</Link> page
      </div>
    );
  }
  return (
    <>
      {err && <div className="alert error">{err}</div>}
      <table>
        <thead><tr><th>agent</th><th>endpoint</th><th>wakes on</th><th className="num">delivered (24h)</th><th className="num">failed (24h)</th><th>last woken</th><th></th></tr></thead>
        <tbody>
          {agents.map((a) => (
            <>
              <tr key={a.name} className="clickable" onClick={() => setOpen(open === a.name ? undefined : a.name)}>
                <td>
                  <strong>{a.name}</strong>
                  {a.kind === "slack" && <span className="badge" style={{ marginLeft: 8 }} title="a Slack channel subscribed to this trigger">Slack</span>}
                  {a.unhealthy && <span className="badge error" style={{ marginLeft: 8 }} title={a.last_error ?? "last delivery failed"}>failing</span>}
                </td>
                <td className="mono">{a.endpoint}</td>
                <td>{a.triggers.map((t) => <span className="chip mono" key={t}>{t}</span>)}</td>
                <td className="num" title={`${a.delivered_ok_total} delivered all time`}>
                  {a.delivered_ok_24h}
                  {a.delivered_ok_total !== a.delivered_ok_24h && <span className="dim"> / {a.delivered_ok_total}</span>}
                </td>
                <td className="num" style={a.delivered_fail_24h ? { color: "var(--err)" } : undefined}
                    title={`${a.delivered_fail_total} failed all time`}>
                  {a.delivered_fail_24h}
                  {a.delivered_fail_total !== a.delivered_fail_24h && <span className="dim"> / {a.delivered_fail_total}</span>}
                </td>
                <td style={{ whiteSpace: "nowrap" }}>{a.last_woken ? <TimeAgo ts={a.last_woken} /> : <span className="help">never</span>}</td>
                <td className="dim">{open === a.name ? "▾" : "▸"}</td>
              </tr>
              {open === a.name && (
                <tr key={a.name + "-detail"}>
                  <td colSpan={7} style={{ background: "var(--wash, transparent)" }}>
                    <div style={{ padding: "8px 4px" }}>
                      <p className="help" style={{ margin: "0 0 8px", whiteSpace: "normal" }}>
                        first seen <TimeAgo ts={a.first_seen} />
                        {a.created_by.length > 0 && <> · wired by <span className="mono">{a.created_by.join(", ")}</span></>}
                      </p>
                      {a.subscriptions.map((sub) => (
                        <div key={sub.subscription_id} className="btnrow" style={{ marginBottom: 6, alignItems: "center" }}>
                          <span className="chip mono">{sub.trigger}</span>
                          <span className="help mono">{sub.subscription_id}</span>
                          <button className="danger" onClick={async (e) => {
                            e.stopPropagation();
                            try { await api.unsubscribe(sub.subscription_id); }
                            catch (ex) { setErr(String((ex as Error).message ?? ex)); }
                          }}>unsubscribe</button>
                        </div>
                      ))}
                      {a.recent.length > 0 && (
                        <>
                          <p className="help" style={{ margin: "10px 0 4px" }}>recent wakes; open one for the full dispatch</p>
                          <table>
                            <thead><tr><th>when</th><th>trigger</th><th>entity</th><th>delivery</th></tr></thead>
                            <tbody>
                              {a.recent.map((r, i) => (
                                <tr key={i} className={r.dispatch_id ? "clickable" : undefined}
                                    onClick={r.dispatch_id ? () => nav(`/dispatches/${encodeURIComponent(r.dispatch_id!)}`) : undefined}>
                                  <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={r.at} /></td>
                                  <td className="mono">{r.dispatch_id ? <Link to={`/dispatches/${encodeURIComponent(r.dispatch_id)}`}>{r.trigger}</Link> : r.trigger}</td>
                                  <td className="mono">{r.key}</td>
                                  <td>{r.ok ? <span className="badge ok">delivered</span> : <span className="badge error" title={r.error ?? undefined}>failed{r.error ? `: ${r.error}` : ""}</span>}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              )}
            </>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Queries() {
  const { data, error } = usePolling(() => api.queries(150));
  const [q, setQ] = useState("");
  const [client, setClient] = useState("mcp");  // default to agent reads; the interesting traffic

  // Filter options: "all" + mcp (always, the important one) + whatever else appears (ui, http).
  const clientOpts = useMemo(
    () => ["all", ...Array.from(new Set(["mcp", ...(data ?? []).map((r) => r.client)]))],
    [data],
  );

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data ?? []).filter((row) =>
      (client === "all" || row.client === client) &&
      (!needle || row.key.toLowerCase().includes(needle) ||
        row.client.toLowerCase().includes(needle)));
  }, [data, q, client]);

  if (error) return <div className="alert error">{error}</div>;
  if (!data?.length) return <div className="empty">no reads yet; connect an agent in Connect and its reads show up here.</div>;
  return (
    <>
      <div className="toolbar">
        <div className="search-box">
          <Search />
          <input type="text" className="search" placeholder="Filter by what was read, client…"
                 value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="seg small" aria-label="client">
          {clientOpts.map((c) => (
            <button key={c} className={client === c ? "active" : ""} onClick={() => setClient(c)}>{c}</button>
          ))}
        </div>
        <span className="grow" />
        <span className="count">{shown.length} of {data.length}</span>
      </div>
      {!shown.length ? (
        <div className="empty">
          no <span className="mono">{client}</span> reads{q && <> matching “{q}”</>} in the last {data.length}
          {client === "mcp" && <> · connect an agent in Connect, or switch the client filter above.</>}
        </div>
      ) : (
        <table>
          <thead><tr><th>when</th><th>client</th><th>call</th><th>what</th><th>window</th><th className="num">rows</th></tr></thead>
          <tbody>
            {shown.map((row) => (
              <tr key={row.id}>
                <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={row.queried_at} /></td>
                <td><span className={`badge ${row.client === "mcp" ? "agent" : "starting"}`}>{row.client}</span></td>
                <td className="mono">{row.id.startsWith("s_") ? "stats" : "read"}</td>
                <td className="mono">{row.key}</td>
                <td className="mono">{row.window}</td>
                <td className="num">{row.rows_returned}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export function Dispatches() {
  const nav = useNavigate();
  const { data, error } = usePolling(() => api.dispatches(150));
  const { data: roster } = usePolling(() => api.agents(), 15000);
  const { data: slack } = usePolling(() => api.slackChannels(), 60000);
  const [q, setQ] = useState("");
  const [trigger, setTrigger] = useState("all");

  const triggerOptions = useMemo(
    () => Array.from(new Set((data ?? []).map((d) => d.trigger))).sort(), [data]);
  const channelLabel = (raw: string) => {
    const cid = raw.replace(/^#/, "");
    const hit = (slack?.channels ?? []).find((c) => c.id === cid);
    return hit ? (hit.is_private ? `🔒 ${hit.name}` : `#${hit.name}`) : raw;
  };
  const recipients = (t: string) => (roster?.agents ?? []).filter((a) => a.triggers.includes(t));

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data ?? []).filter((d) =>
      (trigger === "all" || d.trigger === trigger) &&
      (!needle || d.trigger.toLowerCase().includes(needle) || d.key.toLowerCase().includes(needle)));
  }, [data, q, trigger]);

  if (error) return <div className="alert error">{error}</div>;
  if (!data?.length) return <div className="empty">no trigger firings yet</div>;
  return (
    <>
      <div className="toolbar">
        <div className="search-box">
          <Search />
          <input type="text" className="search" placeholder="Filter by trigger or entity…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <Picker value={trigger} onChange={setTrigger} ariaLabel="filter by trigger"
                style={{ width: 220 }}
                options={["all", ...triggerOptions]}
                labels={{ all: "All triggers" }} />
        <span className="grow" />
        <span className="count">{shown.length} of {data.length}</span>
      </div>
      <table>
        <thead><tr><th>fired</th><th>trigger</th><th>entity</th><th>delivered to</th></tr></thead>
        <tbody>
          {shown.map((d) => {
            const partial = d.subscribers > 0 && d.delivered < d.subscribers;
            const names = recipients(d.trigger);
            return (
              <tr key={d.dispatch_id} className="clickable"
                  onClick={() => nav(`/dispatches/${encodeURIComponent(d.dispatch_id)}`)}>
                <td style={{ whiteSpace: "nowrap" }}><Link to={`/dispatches/${encodeURIComponent(d.dispatch_id)}`}><TimeAgo ts={d.fired_at} /></Link></td>
                <td className="mono">{d.trigger}</td>
                <td className="mono">{d.key}</td>
                <td>
                  {d.subscribers === 0
                    ? <span className="badge error">nobody subscribed</span>
                    : names.length === 0
                    ? <span className={`badge ${partial ? "error" : "ok"}`}>{d.delivered}/{d.subscribers} delivered</span>
                    : names.map((a) => (
                        <span key={a.name} className="chip mono"
                              title={partial ? (d.error ?? "not every delivery succeeded") : "delivered"}
                              style={{ marginRight: 4, color: partial ? "var(--err)" : "var(--ok)" }}>
                          {a.kind === "slack" ? channelLabel(a.name) : a.name}
                        </span>
                      ))}
                </td>
              </tr>
            );
          })}
          {!shown.length && <tr><td colSpan={4} className="dim" style={{ textAlign: "center", padding: 24 }}>no firings match the filter</td></tr>}
        </tbody>
      </table>
    </>
  );
}

