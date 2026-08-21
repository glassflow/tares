import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import AgentForm from "../components/AgentForm";
import ConfirmDialog from "../components/ConfirmDialog";
import UsecaseBadge from "../components/UsecaseBadge";
import { ErrorState, TimeAgo, fmtCost, fmtTokens, usePolling } from "../components/bits";
import type { AgentRun, BuiltinAgent } from "../types";

// Everything about one Tares agent, run like an operational surface rather than a config sheet:
// what it has cost and how it has performed (the stat cards), what it produced (Runs), and how it
// is wired (Configuration). External (connected) agents are not managed here; they live in the
// roster under Deliveries.

type Tab = "overview" | "runs" | "configuration";

function statusBadge(r: AgentRun) {
  if (r.status === "ok") return <span className="badge ok">ok</span>;
  if (r.status === "running") return <span className="badge starting">running</span>;
  // "empty"/"capped"/"exhausted" ran and declined to conclude, hit the daily cap, or ran out of
  // rounds. Not failures.
  const cls = r.status === "failed" ? "error" : "";
  return <span className={`badge ${cls}`}>{r.status}</span>;
}

export default function AgentDetail() {
  // A dispatch page links here as ?dispatch=<id> (and a use case as ?run=<id>): the Runs tab
  // opens with that run expanded, highlighted, and scrolled into view.

  const { name = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const focusDispatch = search.get("dispatch") ?? undefined;
  const focusRun = search.get("run") ?? undefined;
  const tabParam = search.get("tab");
  const tab: Tab = focusDispatch || focusRun
    ? "runs"
    : tabParam === "runs" || tabParam === "configuration" ? tabParam : "overview";
  const nav = useNavigate();
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [openRun, setOpenRun] = useState<string>();
  const [err, setErr] = useState<string>();

  const { data, error, reload } = usePolling(() => api.builtinAgents(), 10000);
  const { data: runs, error: runsError } = usePolling(() => api.builtinAgentRuns(name, 50), 10000);
  const { data: triggers } = usePolling(() => api.triggers(), 30000);

  if (error) return <div className="alert error">{error}</div>;
  if (!data) return <div className="dim">loading…</div>;

  const agent = data.agents.find((a) => a.name === name);
  if (!agent) {
    return (
      <div className="alert error">
        no Tares agent named <span className="mono">{name}</span>. Connected (external) agents are
        listed under <Link to="/deliveries">Deliveries</Link>. See <Link to="/agents">Agents</Link>.
      </div>
    );
  }

  const setTab = (t: Tab) => {
    // Switching tabs drops the run/dispatch focus params, or they would pin the Runs tab forever.
    const next = new URLSearchParams();
    if (t !== "overview") next.set("tab", t);
    setSearch(next, { replace: true });
    if (t !== "configuration") setEditing(false);
  };

  const toggle = async () => {
    setErr(undefined);
    try {
      if (agent.enabled) await api.disableBuiltinAgent(agent.name);
      else await api.enableBuiltinAgent(agent.name);
      reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  };

  const stats = agent.stats;
  const success = stats && stats.finished > 0 ? Math.round((100 * stats.ok) / stats.finished) : null;
  const lastRun = runs?.[0];
  const latestFinding = runs?.find((r) => r.finding && r.status === "ok");

  return (
    <>
      <div className="pagehead">
        <div>
          <h1><span className="mono">{agent.name}</span>{" "}
            <span className="badge">Tares agent</span></h1>
          <p className="subtitle">a prompt that takes a first look when its trigger fires
            {agent.owned_by && <> · <UsecaseBadge ownedBy={agent.owned_by} customized={agent.customized} /></>}
          </p>
        </div>
        {!editing && (
          <span className="btnrow">
            <button className="primary" onClick={toggle}>{agent.enabled ? "Disable" : "Enable"}</button>
            <button onClick={() => { setTab("configuration"); setEditing(true); }}>Edit</button>
            <button className="danger" onClick={() => setConfirmDel(true)}>Delete</button>
          </span>
        )}
      </div>

      {err && <div className="alert error">{err}</div>}

      <div className="cards">
        <div className="card">
          <div className="k">total cost</div>
          <div className="v">{fmtCost(stats?.cost_usd)}
            {!!stats?.uncosted_runs && <small title="runs from before cost tracking, or on a model without a known price">+{stats.uncosted_runs} uncosted</small>}
          </div>
        </div>
        <div className="card"><div className="k">runs</div><div className="v">{stats?.runs ?? 0}</div></div>
        <div className="card">
          <div className="k">success</div>
          <div className="v">{success != null ? `${success}%` : "—"}
            {stats && stats.finished > 0 && <small>{stats.ok} of {stats.finished} concluded</small>}
          </div>
        </div>
        <div className="card">
          <div className="k">avg duration</div>
          <div className="v">{stats?.avg_duration_ms != null ? `${(stats.avg_duration_ms / 1000).toFixed(1)}s` : "—"}</div>
        </div>
      </div>

      <div className="tabs" style={{ marginTop: 16 }}>
        <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>Overview</button>
        <button className={tab === "runs" ? "active" : ""} onClick={() => setTab("runs")}>Runs</button>
        <button className={tab === "configuration" ? "active" : ""} onClick={() => setTab("configuration")}>Configuration</button>
      </div>

      {tab === "overview" && (
        <>
          <div className="panel">
            <table>
              <tbody>
                <tr>
                  <td className="help" style={{ width: 150 }}>status</td>
                  <td>
                    {agent.enabled ? <span className="badge ok">enabled</span> : <span className="badge">disabled</span>}
                    {!data.key_configured
                      ? <span className="help"> · no Anthropic key: set one under <Link to="/settings?tab=anthropic">Settings</Link> to run</span>
                      : <span className="help"> · key from <span className="mono">{data.key_source}</span></span>}
                  </td>
                </tr>
                <tr><td className="help">wakes on</td>
                    <td><Link to={`/triggers/${encodeURIComponent(agent.trigger)}`} className="mono">{agent.trigger}</Link>
                        <span className="help"> · the trigger that runs this agent</span></td></tr>
                <tr><td className="help">writes to</td>
                    <td><Link to="/sources/findings" className="mono">findings</Link>
                        <span className="help"> · one finding per run, on the entity's timeline</span></td></tr>
                <tr><td className="help">last woken</td>
                    <td>{lastRun
                      ? <><TimeAgo ts={lastRun.started_at} /> for <span className="mono">{lastRun.key}</span>
                          {lastRun.dispatch_id && <> · <Link to={`/dispatches/${encodeURIComponent(lastRun.dispatch_id)}`}>the firing</Link></>}</>
                      : <span className="dim">never</span>}</td></tr>
                <tr><td className="help">model</td>
                    <td><span className="mono">{agent.model || data.default_model}</span>
                        {!agent.model && <span className="help"> · instance default</span>}</td></tr>
                <tr><td className="help">tokens used</td>
                    <td>{stats && (stats.input_tokens || stats.output_tokens)
                      ? <><span className="mono">{fmtTokens(stats.input_tokens)}</span> in
                          {" · "}<span className="mono">{fmtTokens(stats.output_tokens)}</span> out</>
                      : <span className="dim">—</span>}</td></tr>
                <tr><td className="help">Slack</td>
                    <td>{agent.slack_channel
                      ? <><span className="badge ok">channel</span> <span className="help">posted by the workspace bot</span></>
                      : agent.slack_configured
                        ? <><span className="badge ok">webhook</span> <span className="help">legacy per-agent webhook</span></>
                        : <span className="dim">—</span>}</td></tr>
              </tbody>
            </table>
          </div>

          {latestFinding && (
            <>
              <h2>Latest finding</h2>
              <div className="panel">
                <p className="help" style={{ margin: "0 0 8px" }}>
                  for <span className="mono">{latestFinding.key}</span> · <TimeAgo ts={latestFinding.started_at} />
                  {" · "}<a onClick={(e) => { e.preventDefault(); setTab("runs"); }} href="?tab=runs">all runs</a>
                </p>
                <div className="md">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{latestFinding.finding!}</ReactMarkdown>
                </div>
              </div>
            </>
          )}
        </>
      )}

      {tab === "runs" && (
        <RunsTable runs={runs} runsError={runsError} agent={agent}
                   focusDispatch={focusDispatch} focusRun={focusRun}
                   openRun={openRun} setOpenRun={setOpenRun} />
      )}

      {tab === "configuration" && (
        editing ? (
          <AgentForm
            initial={agent}
            triggers={(triggers ?? []).map((t) => t.name)}
            presets={data.presets}
            models={data.models}
            defaultModel={data.default_model}
            slackWorkspace={data.slack_workspace}
            defaultMaxRounds={data.default_max_rounds}
            defaultMaxRoundsWithMcp={data.default_max_rounds_with_mcp}
            maxRoundsLimit={data.max_rounds_limit}
            onSaved={() => { setEditing(false); reload(); }}
            onCancel={() => setEditing(false)}
          />
        ) : (
          <div className="panel">
            <table>
              <tbody>
                <tr><td className="help" style={{ width: 150 }}>trigger</td>
                    <td><Link to={`/triggers/${encodeURIComponent(agent.trigger)}`} className="mono">{agent.trigger}</Link></td></tr>
                <tr><td className="help">model</td>
                    <td><span className="mono">{agent.model || data.default_model}</span>
                        {!agent.model && <span className="help"> · instance default</span>}</td></tr>
                <tr><td className="help">max rounds</td>
                    <td><span className="mono">{agent.effective_max_rounds}</span>
                        {!agent.max_rounds && <span className="help"> · default{agent.mcp_servers.length ? " for an agent with external MCP servers" : ""}</span>}</td></tr>
                <tr><td className="help">external tools</td>
                    <td>{agent.mcp_servers.length
                      ? agent.mcp_servers.map((m) => <span key={m} className="chip mono" style={{ marginRight: 4 }}>{m}</span>)
                      : <span className="dim">—</span>}</td></tr>
                <tr><td className="help">Slack</td>
                    <td>{agent.slack_channel
                      ? <><span className="badge ok">channel</span> <span className="help">posted by the workspace bot</span></>
                      : agent.slack_configured
                        ? <><span className="badge ok">webhook</span> <span className="help">legacy per-agent webhook</span></>
                        : <span className="dim">—</span>}</td></tr>
                <tr><td className="help">write-back</td>
                    <td>{agent.webhook_url
                      ? <><span className="mono">{agent.webhook_url}</span>
                          {agent.webhook_token_configured
                            ? <span className="badge ok" style={{ marginLeft: 8 }}>bearer auth</span>
                            : <span className="help"> · no auth</span>}</>
                      : <span className="dim">—</span>}</td></tr>
                <tr><td className="help">prompt</td>
                    <td><pre className="mono" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{agent.prompt}</pre></td></tr>
              </tbody>
            </table>
            <button style={{ marginTop: 10 }} onClick={() => setEditing(true)}>Edit</button>
          </div>
        )
      )}

      {confirmDel && (
        <ConfirmDialog
          title={`Delete agent ${agent.name}?`}
          message="Findings already written stay on their timelines; this stops new ones. This can't be undone."
          confirmLabel="Delete"
          danger
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            try { await api.deleteBuiltinAgent(agent.name); nav("/agents"); }
            catch (e) { setErr(String((e as Error).message ?? e)); setConfirmDel(false); }
          }}
        />
      )}
    </>
  );
}

function RunsTable({ runs, runsError, agent, focusDispatch, focusRun, openRun, setOpenRun }: {
  runs: AgentRun[] | undefined;
  runsError: string | undefined;
  agent: BuiltinAgent;
  focusDispatch?: string;
  focusRun?: string;
  openRun: string | undefined;
  setOpenRun: (id: string | undefined) => void;
}) {
  if (runsError) return <ErrorState error={runsError} what="this agent’s runs" />;
  if (!runs?.length) {
    return <p className="help">none yet; this agent runs when <span className="mono">{agent.trigger}</span> fires</p>;
  }
  const isFocused = (r: AgentRun) =>
    (!!focusDispatch && r.dispatch_id === focusDispatch) || (!!focusRun && r.id === focusRun);
  return (
    <table>
      <thead>
        <tr>
          <th>status</th><th>when</th><th>entity</th><th>model</th>
          <th className="num">rounds</th><th className="num">tokens</th>
          <th className="num">cost</th><th className="num">duration</th><th />
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => {
          const focused = isFocused(r);
          // A deep-linked run starts open; clicking any row toggles it like the roster table.
          const open = openRun !== undefined ? openRun === r.id : focused;
          return (
            <RunRow key={r.id} r={r} open={open} focused={focused}
                    onToggle={() => setOpenRun(open ? "" : r.id)} />
          );
        })}
      </tbody>
    </table>
  );
}

function RunRow({ r, open, focused, onToggle }: {
  r: AgentRun; open: boolean; focused: boolean; onToggle: () => void;
}) {
  // A dash on an old run means unknown, not zero: usage was not recorded before cost tracking.
  const hasTokens = r.input_tokens != null || r.output_tokens != null;
  const cache = (r.cache_creation_input_tokens ?? 0) + (r.cache_read_input_tokens ?? 0);
  const exhaustedNote = r.status === "exhausted" && (
    <p className="help" style={{ margin: "0 0 8px", whiteSpace: "normal" }}>
      ran out of rounds before concluding ({r.rounds}{r.max_rounds ? `/${r.max_rounds}` : ""});
      raise max rounds under Configuration, Advanced.
      {r.finding ? " What it had so far:" : ""}
    </p>
  );
  return (
    <>
      <tr className="clickable" onClick={onToggle}
          style={focused ? { outline: "2px solid var(--accent)", outlineOffset: -2 } : undefined}
          ref={(el) => { if (el && focused) el.scrollIntoView({ block: "center" }); }}>
        <td>{statusBadge(r)}</td>
        <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={r.started_at} /></td>
        <td className="mono">{r.key}</td>
        <td className="mono">{r.model ?? <span className="dim">—</span>}</td>
        <td className="num">{r.rounds}{r.max_rounds ? <span className="dim">/{r.max_rounds}</span> : null}</td>
        <td className="num" style={{ whiteSpace: "nowrap" }}
            title={hasTokens ? `${(r.input_tokens ?? 0).toLocaleString()} in · ${(r.output_tokens ?? 0).toLocaleString()} out` : "this run predates usage tracking"}>
          {hasTokens
            ? <>{fmtTokens(r.input_tokens)} <span className="dim">in</span> {fmtTokens(r.output_tokens)} <span className="dim">out</span></>
            : <span className="dim">—</span>}
        </td>
        <td className="num">{fmtCost(r.cost_usd)}</td>
        <td className="num" style={{ whiteSpace: "nowrap" }}>
          {r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}
        </td>
        <td className="dim">{open ? "▾" : "▸"}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={9} style={{ background: "var(--wash, transparent)" }}>
            <div style={{ padding: "8px 4px" }}>
              <p className="help" style={{ margin: "0 0 8px", whiteSpace: "normal" }}>
                {r.dispatch_id
                  ? <><Link to={`/dispatches/${encodeURIComponent(r.dispatch_id)}`}>the firing</Link> that woke it</>
                  : "run without a firing (manual or bootstrap)"}
                {r.tool_calls ? <> · {r.tool_calls} tool call{r.tool_calls === 1 ? "" : "s"}</> : null}
                {cache > 0 && <> · cache: {fmtTokens(r.cache_creation_input_tokens)} written, {fmtTokens(r.cache_read_input_tokens)} read</>}
              </p>
              {(r.external_tools ?? []).length > 0 && (
                <p style={{ margin: "0 0 8px" }}>
                  {[...new Set(r.external_tools)].map((t) => (
                    <span key={t} className="chip mono" title="external MCP tool this run called"
                          style={{ marginRight: 4 }}>{t}</span>
                  ))}
                </p>
              )}
              {exhaustedNote}
              {r.finding
                ? <div className="md">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.finding}</ReactMarkdown>
                  </div>
                : !exhaustedNote && (
                    <p className="help" style={{ margin: 0, whiteSpace: "normal" }}>
                      {r.status === "running" ? "investigating…" : (r.error ?? "no finding")}
                    </p>
                  )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
