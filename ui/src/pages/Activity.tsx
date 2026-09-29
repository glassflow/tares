import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, auth } from "../api";
import { Search } from "../components/icons";
import { Picker, TimeAgo, usePolling } from "../components/bits";
import type { DispatchLogEntry } from "../types";

// Two pages share this module: Connect (how an agent hooks up — one tab per integration mode)
// and AgentActivity (what agents have been doing — reads + trigger dispatches).
type ConnectTab = "mcp" | "push" | "rest" | "tools";
const CONNECT_LABELS: Record<ConnectTab, string> = {
  mcp: "MCP (pull)", push: "Webhook (push)", rest: "REST", tools: "Tools",
};

// The MCP tab is a client picker, mirroring the docs page (docs.glassflow.ai/tares/agents): the
// user picks their harness and gets the exact command with this instance's endpoint and token
// filled in, instead of translating a generic snippet themselves.
type McpClient = "Claude Code" | "Codex CLI" | "Cursor" | "Claude Desktop" | "Other (JSON)" | "stdio";
const MCP_CLIENTS: McpClient[] = ["Claude Code", "Codex CLI", "Cursor", "Claude Desktop", "Other (JSON)", "stdio"];

export function ConnectPage() {
  // Deep-linkable: the trigger page links here as /connect?tab=push, straight to the webhook
  // contract. State stays local after that; only the initial tab comes from the URL.
  const [params] = useSearchParams();
  const initial = params.get("tab");
  const [tab, setTab] = useState<ConnectTab>(
    initial && initial in CONNECT_LABELS ? (initial as ConnectTab) : "mcp");

  return (
    <>
      <h1>Connect</h1>
      <p className="subtitle">
        hook an agent up to this Tares; <em>it pulls (MCP), gets pushed (webhook), or calls
        plain REST</em>
      </p>

      <div className="tabs">
        {(Object.keys(CONNECT_LABELS) as ConnectTab[]).map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{CONNECT_LABELS[t]}</button>
        ))}
      </div>

      <Connect tab={tab} />
    </>
  );
}

// Reads: the one feed left on this page. Trigger dispatches moved to Deliveries (with the
// subscriber roster they belong to); the connected-agent roster went the same way (TR-137).
export default function AgentActivity() {
  return (
    <>
      <h1>Reads</h1>
      <p className="subtitle">every read agents made over MCP or the API, newest first</p>
      <Queries />
    </>
  );
}

/** Shared filter toolbar: a search box + a live "N of M" count. */
function Toolbar({ q, setQ, placeholder, shown, total }: {
  q: string; setQ: (s: string) => void; placeholder: string; shown: number; total: number;
}) {
  return (
    <div className="toolbar">
      <div className="search-box">
        <Search />
        <input type="text" className="search" placeholder={placeholder} value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <span className="grow" />
      <span className="count">{shown} of {total}</span>
    </div>
  );
}

function Copy({ text, label = "copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="copybtn" onClick={() => { navigator.clipboard?.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); }}>
      {done ? "copied" : label}
    </button>
  );
}

// `copyText` lets a snippet DISPLAY the masked token (••••••••) while the copy button carries the
// real one. Without it, a credentialed snippet either exposes the token on screen or copies the
// mask — both wrong.
function CodeBlock({ title, code, copyText }: { title: string; code: string; copyText?: string }) {
  return (
    <div className="codeblock">
      <div className="codeblock-head"><span>{title}</span><Copy text={copyText ?? code} /></div>
      <pre className="payload">{code}</pre>
    </div>
  );
}

function Connect({ tab }: { tab: ConnectTab }) {
  const origin = window.location.origin;
  const [authReq, setAuthReq] = useState<boolean>();
  const [status, setStatus] = useState<"checking" | "live" | "absent">("checking");
  const [url, setUrl] = useState(`${origin}/mcp`);
  const [tools, setTools] = useState<{ name: string; description: string }[]>();
  const [triggers, setTriggers] = useState<{ name: string }[]>();
  const [trig, setTrig] = useState<string>();
  const [reveal, setReveal] = useState(false);
  const [toolQ, setToolQ] = useState("");
  const [mcpClient, setMcpClient] = useState<McpClient>("Claude Code");
  const token = auth.get();

  useEffect(() => {
    api.health().then((h) => setAuthReq(h.auth_required)).catch(() => setAuthReq(false));
    api.mcpTools().then(setTools).catch(() => setTools([]));
    api.triggers().then((t) => { setTriggers(t); if (t.length) setTrig(t[0].name); })
      .catch(() => setTriggers([]));

    // The MCP endpoint sits in one of two places: same host/port behind a reverse proxy
    // (the compose deployment — Caddy routes /mcp to the MCP server), or on its own port
    // when `tares mcp` runs as a separate process locally (the documented default, :8788).
    const loc = window.location;
    const sameOrigin = `${origin}/mcp`;
    const localPort = `${loc.protocol}//${loc.hostname}:8788/mcp`;

    const probe = async (target: string, ms = 2500): Promise<boolean> => {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), ms);
      try {
        if (target === sameOrigin) {
          // same-origin: a real MCP server answers non-HTML; the SPA catch-all returns HTML.
          const r = await fetch(target, { headers: { Accept: "application/json, text/event-stream" }, signal: ctl.signal });
          return !(r.headers.get("content-type") || "").includes("text/html");
        }
        // cross-origin: the MCP server sends no CORS headers, so we can't read the response —
        // but a no-cors fetch that resolves (rather than failing on connection refused) means
        // something is listening on that port.
        await fetch(target, { mode: "no-cors", signal: ctl.signal });
        return true;
      } catch {
        return false;
      } finally {
        clearTimeout(timer);
      }
    };

    (async () => {
      if (await probe(sameOrigin)) { setUrl(sameOrigin); setStatus("live"); return; }
      if (localPort !== sameOrigin && (await probe(localPort))) { setUrl(localPort); setStatus("live"); return; }
      setStatus("absent");
    })();
  }, [origin]);
  const tok = authReq ? (token || "<your-access-token>") : "";
  const shownTok = authReq && !reveal && token ? "••••••••" : tok;

  const claudeCode = (t: string) =>
    `claude mcp add --transport http tares ${url}` + (authReq ? ` --header "Authorization: Bearer ${t}"` : "");
  const codexCmd = (t: string) => authReq
    ? `export TARES_API_KEY=${t}\ncodex mcp add tares --url ${url} --bearer-token-env-var TARES_API_KEY`
    : `codex mcp add tares --url ${url}`;
  const httpJson = (t: string) => JSON.stringify({
    mcpServers: { tares: { type: "http", url, ...(authReq ? { headers: { Authorization: `Bearer ${t}` } } : {}) } },
  }, null, 2);
  // Cursor and Claude Desktop take the bare-url shape (no "type" field).
  const urlJson = (t: string) => JSON.stringify({
    mcpServers: { tares: { url, ...(authReq ? { headers: { Authorization: `Bearer ${t}` } } : {}) } },
  }, null, 2);
  const stdioJson = (t: string) => JSON.stringify({
    mcpServers: { tares: { command: "tares-mcp", env: { TARESD_URL: origin, ...(authReq ? { TARES_AUTH_TOKEN: t } : {}) } } },
  }, null, 2);
  const subscribeCurl = (t: string) =>
    `curl -X POST ${origin}/subscribe \\\n` +
    `  -H 'Content-Type: application/json' \\\n` +
    (authReq ? `  -H 'Authorization: Bearer ${t}' \\\n` : "") +
    `  -d '{"trigger": "${trig ?? "<trigger>"}", "url": "https://your-agent.example.com/hook"}'`;
  const queryCurl = (t: string) =>
    `curl -X POST ${origin}/read \\\n` +
    `  -H 'Content-Type: application/json' \\\n` +
    (authReq ? `  -H 'Authorization: Bearer ${t}' \\\n` : "") +
    `  -d '{"selector": {"service": "<entity>"}, "window": "15m"}'`;

  const shownTools = useMemo(() => {
    const needle = toolQ.trim().toLowerCase();
    return (tools ?? []).filter((t) =>
      !needle || t.name.toLowerCase().includes(needle) || t.description.toLowerCase().includes(needle));
  }, [tools, toolQ]);

  return (
    <div className="connect">
      {tab === "mcp" && (
        <div className={`mcp-status ${status}`}>
          {status === "checking" && "checking the MCP endpoint…"}
          {status === "live" && <span>MCP endpoint is live at <span className="mono">{url}</span></span>}
          {status === "absent" && <span>No MCP endpoint found; neither at <span className="mono">{origin}/mcp</span> (reverse-proxied) nor on the default port <span className="mono">:8788</span> (separate process). Run <span className="mono">tares mcp</span> alongside the daemon (the compose deployment includes it), or use the <strong>stdio</strong> proxy below.</span>}
        </div>
      )}

      {tab === "mcp" && authReq && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <div><span className="lbl">access token</span> <span className="mono">{shownTok}</span></div>
            <div className="btnrow">
              <button onClick={() => setReveal((r) => !r)}>{reveal ? "hide" : "reveal"}</button>
              {token && <Copy text={token} label="copy token" />}
            </div>
          </div>
          <p className="help" style={{ marginTop: 6, whiteSpace: "normal" }}>
            This is your console access token; it works in the snippets below, but it carries{" "}
            <strong>full admin rights</strong>. For an agent, prefer a{" "}
            <span className="mono">read</span>-scoped API key from the{" "}
            <Link to="/settings">Settings page</Link> (add <span className="mono">ingest</span> if
            the agent also writes memories) and paste it in place of the token.
          </p>
        </div>
      )}

      {tab === "push" && (
        <>
          <p className="help" style={{ whiteSpace: "normal", marginTop: 14 }}>
            Flip the loop: Tares watches, the agent sleeps. When a trigger&rsquo;s condition
            trips, Tares POSTs the <strong>correlated timeline</strong> to a URL your agent
            listens on, so the investigation starts with the evidence already attached.
          </p>

          <h3 style={{ marginBottom: 4 }}>1 · Give your agent an HTTP endpoint</h3>
          <p className="help" style={{ whiteSpace: "normal" }}>
            This is for <strong>your own agent, running on your infrastructure</strong>. It needs
            an HTTP endpoint that accepts a POST and starts the agent with the request body as
            context, reachable <em>from this Tares server</em>. For example:
          </p>
          <CodeBlock title="a minimal receiver (FastAPI, runs on your side)" code={[
            "from fastapi import FastAPI, Request",
            "app = FastAPI()",
            "",
            "@app.post(\"/hook\")",
            "async def hook(req: Request):",
            "    dispatch = await req.json()",
            "    run_my_agent(dispatch[\"payload\"])   # the correlated timeline",
            "    return {\"ok\": True}",
          ].join("\n")} />
          <p className="help" style={{ whiteSpace: "normal" }}>
            (Don&rsquo;t have an agent of your own? A built-in{" "}
            <Link to="/agents">Tares agent</Link> runs inside Tares on a trigger firing, no
            endpoint needed.) Every delivery is JSON shaped like:
          </p>
          <CodeBlock title="what your endpoint receives on each firing" code={JSON.stringify({
            dispatch_id: "9f2c81d4…",
            trigger: trig ?? "error_spike",
            kind: "alert",
            key: "api-server",
            fired_at: "2026-07-17T09:14:03+00:00",
            payload: "…the correlated timeline for this entity, ready to hand to the model…",
          }, null, 2)} />
          <p className="help" style={{ whiteSpace: "normal" }}>
            Delivery is <strong>at-least-once</strong> · dedupe on{" "}
            <span className="mono">dispatch_id</span>. Answer <strong>2xx</strong> to acknowledge;
            5xx and transport errors are retried with backoff, a 4xx is recorded as failed and not
            retried.
          </p>

          <h3 style={{ marginBottom: 4 }}>2 · Subscribe that endpoint to a trigger</h3>
          {triggers && triggers.length > 0 ? (
            <>
              <p className="help" style={{ whiteSpace: "normal" }}>
                Wire it on the trigger&rsquo;s page: open <Link to="/triggers">Triggers</Link>,
                pick the trigger, and paste your endpoint there. From a script, the same action is
                (needs a <span className="mono">read</span>-scoped{" "}
                <Link to="/settings">API key</Link>):
              </p>
              <CodeBlock title="subscribe your agent's endpoint" code={subscribeCurl(shownTok)} copyText={subscribeCurl(tok)} />
            </>
          ) : (
            <p className="help" style={{ whiteSpace: "normal" }}>
              This instance has no triggers yet, so there is nothing to subscribe to; create one
              under <Link to="/triggers">Triggers</Link> (a condition over some of a project's sources), then come back
              here.
            </p>
          )}

          <p className="help" style={{ whiteSpace: "normal" }}>
            Every wired endpoint shows up under <Link to="/agents">Agents</Link> with its delivery
            history. Full walkthrough:{" "}
            <a href="https://docs.glassflow.ai/tares/guides/triggers" target="_blank" rel="noreferrer">
              docs.glassflow.ai/tares/guides/triggers</a>.
          </p>

        </>
      )}

      {tab === "rest" && (
        <>
          <p className="help" style={{ whiteSpace: "normal", marginTop: 14 }}>
            No MCP client? The same read surface is plain HTTP; one call returns the correlated
            timeline. Agents can also write conclusions back with{" "}
            <span className="mono">POST /remember</span> (needs <span className="mono">ingest</span>),
            so the next timeline arrives with prior findings attached.
          </p>
          <CodeBlock title="read a correlated timeline" code={queryCurl(shownTok)} copyText={queryCurl(tok)} />
        </>
      )}

      {tab === "mcp" && (
        <>
      <p className="help" style={{ whiteSpace: "normal", marginTop: 14 }}>
        The agent connects as an MCP client and reads on demand: <span className="mono">read</span>,{" "}
        <span className="mono">query</span>, <span className="mono">catalog_*</span> · one correlated
        timeline per call instead of fanning out across your systems. Needs a{" "}
        <span className="mono">read</span>-scoped key. Pick your client:
      </p>

      <div className="seg small" aria-label="MCP client" style={{ marginBottom: 10 }}>
        {MCP_CLIENTS.map((c) => (
          <button key={c} className={mcpClient === c ? "active" : ""} onClick={() => setMcpClient(c)}>{c}</button>
        ))}
      </div>

      {mcpClient === "Claude Code" && (
        <>
          <CodeBlock title="run in your terminal" code={claudeCode(shownTok)} copyText={claudeCode(tok)} />
          <p className="help" style={{ whiteSpace: "normal" }}>
            Or install the{" "}
            <a href="https://docs.glassflow.ai/tares/connectors/claude-code" target="_blank" rel="noreferrer">
              Tares plugin</a>{" "}
            instead: same connection, plus session capture into the{" "}
            <span className="mono">claude_code</span> source.
          </p>
          <CodeBlock title="plugin install (run inside Claude Code)"
                     code={"/plugin marketplace add glassflow/tares\n/plugin install tares@tares"} />
        </>
      )}
      {mcpClient === "Codex CLI" && (
        <CodeBlock title="run in your terminal (keep --url; a bare URL registers a stdio server)"
                   code={codexCmd(shownTok)} copyText={codexCmd(tok)} />
      )}
      {mcpClient === "Cursor" && (
        <CodeBlock title="add to ~/.cursor/mcp.json (or .cursor/mcp.json per project), then approve under Settings → MCP"
                   code={urlJson(shownTok)} copyText={urlJson(tok)} />
      )}
      {mcpClient === "Claude Desktop" && (
        <CodeBlock title="add to claude_desktop_config.json (Settings → Developer → Edit Config), then restart"
                   code={urlJson(shownTok)} copyText={urlJson(tok)} />
      )}
      {mcpClient === "Other (JSON)" && (
        <>
          <CodeBlock title="the generic shape most clients accept" code={httpJson(shownTok)} copyText={httpJson(tok)} />
          <p className="help" style={{ whiteSpace: "normal" }}>
            Per-client config files and field names (VS Code, Windsurf, Zed, Gemini CLI, …):{" "}
            <a href="https://docs.glassflow.ai/tares/agents" target="_blank" rel="noreferrer">
              docs.glassflow.ai/tares/agents</a>.
          </p>
        </>
      )}
      {mcpClient === "stdio" && (
        <>
          <p className="help" style={{ whiteSpace: "normal" }}>
            For an agent on the same machine, or when no <span className="mono">/mcp</span> endpoint
            is running. Requires <span className="mono">pip install tares</span> where the agent
            runs; it proxies to this daemon.
          </p>
          <CodeBlock title="MCP config (stdio)" code={stdioJson(shownTok)} copyText={stdioJson(tok)} />
        </>
      )}

      <p className="help" style={{ whiteSpace: "normal" }}>
        Then verify: ask the client <em>&ldquo;Use tares: what are you ingesting right
        now?&rdquo;</em>. It should call <span className="mono">catalog_list</span> and answer with
        your sources, triggers and projects.
      </p>
        </>
      )}

      {tab === "tools" && (
        <>
      <h3 style={{ marginTop: 14 }}>Tools the agent gets {tools && <span className="dim">· {tools.length}</span>}</h3>
      <p className="help" style={{ whiteSpace: "normal" }}>
        Exactly what a connected agent can call; read straight from this instance&rsquo;s MCP
        surface, so it always matches what MCP (pull) serves. The same read/query operations back
        the REST endpoints.
      </p>
      {tools && tools.length > 0 && (
        <>
          <div className="toolbar">
            <div className="search-box">
              <Search />
              <input type="text" className="search" placeholder="Filter tools…" value={toolQ} onChange={(e) => setToolQ(e.target.value)} />
            </div>
            <span className="grow" />
            <span className="count">{shownTools.length} of {tools.length}</span>
          </div>
          <table>
            <thead><tr><th>tool</th><th>what it does</th></tr></thead>
            <tbody>
              {shownTools.map((t) => (
                <tr key={t.name}><td className="mono">{t.name}</td><td className="help" style={{ whiteSpace: "normal" }}>{t.description}</td></tr>
              ))}
              {!shownTools.length && (
                <tr><td colSpan={2} className="dim" style={{ textAlign: "center", padding: 24 }}>no tools match “{toolQ}”</td></tr>
              )}
            </tbody>
          </table>
        </>
      )}
        </>
      )}
    </div>
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
                              style={{ marginRight: 4, color: partial ? "var(--err)" : "var(--ok, #2e7d43)" }}>
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

