import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { AgentsRoster } from "./Activity";
import { TimeAgo, fmtCost, usePolling } from "../components/bits";
import type { AgentRun } from "../types";

// Tares agents: the prompts you author that run in-process when a trigger fires. Subscribers
// (external webhooks, Slack channels) are runtime state and live under Deliveries (TR-137).

function runBadge(r: AgentRun | null | undefined) {
  if (!r) return <span className="dim">never run</span>;
  if (r.status === "ok") return <span className="badge ok">ok</span>;
  if (r.status === "running") return <span className="badge starting">running</span>;
  const cls = r.status === "failed" ? "error" : "";
  return <span className={`badge ${cls}`} title={r.error ?? undefined}>{r.status}</span>;
}

export default function Agents() {
  const nav = useNavigate();
  const { data, error } = usePolling(() => api.builtinAgents(), 10000);

  if (error) return <div className="alert error">{error}</div>;

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Tares agents</h1>
          <p className="subtitle">
            prompts that run inside Tares when a trigger fires and write a finding back
          </p>
        </div>
        <button className="primary" onClick={() => nav("/agents/new")}>Create Tares agent</button>
      </div>

      {data && !data.key_configured && (
        <div className="alert">
          Model access is not configured: set ANTHROPIC_API_KEY before <span className="mono">tares up</span>, or add a
          key under <Link to="/settings?tab=anthropic">Settings</Link>. Agents can be created but not enabled until then.
        </div>
      )}

      {!data ? <div className="dim">loading…</div>
        : data.agents.length === 0 ? (
          <div className="panel">
            <p className="help" style={{ whiteSpace: "normal", marginTop: 0 }}>
              No Tares agents yet. Create one here, or from a trigger's page; it reads the same
              correlated timeline your external agents receive and writes what it found back into
              Tares, so the next agent to read that entity already has the conclusion.
            </p>
            <button className="primary" onClick={() => nav("/agents/new")}>Create Tares agent</button>
          </div>
        ) : (
          <table>
            <thead><tr><th>agent</th><th>trigger</th><th>status</th><th>last run</th>
              <th className="num">runs</th><th className="num">cost</th><th>finding</th></tr></thead>
            <tbody>
              {data.agents.map((a) => (
                <tr key={a.name} className="clickable"
                    onClick={() => nav(`/agents/${encodeURIComponent(a.name)}`)}>
                  <td><Link to={`/agents/${encodeURIComponent(a.name)}`}><strong>{a.name}</strong></Link></td>
                  <td><Link to={`/triggers/${encodeURIComponent(a.trigger)}`} className="mono">{a.trigger}</Link></td>
                  <td>{a.enabled ? <span className="badge ok">enabled</span> : <span className="badge">disabled</span>}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {runBadge(a.last_run)}{a.last_run && <> <TimeAgo ts={a.last_run.started_at} /></>}
                  </td>
                  <td className="num">{a.stats?.runs ?? 0}</td>
                  <td className="num"
                      title={a.stats?.uncosted_runs ? `plus ${a.stats.uncosted_runs} run(s) with no recorded cost` : undefined}>
                    {fmtCost(a.stats?.cost_usd)}
                  </td>
                  <td style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {a.last_run?.finding ?? <span className="dim">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

      <h2 style={{ marginTop: 28 }}>External subscribers</h2>
      <AgentsRoster only="connected" />
    </>
  );
}
