import { useState } from "react";

import { api } from "../api";
import ConfirmDialog from "./ConfirmDialog";
import { ErrorState, TimeAgo, usePolling } from "./bits";
import { CopySecret } from "../pages/Security";
import type { ApiKey, ExternalAgent } from "../types";

// Joining a project from outside (TR-335, TR-336). A project key reads this project only (its
// sources, skills, findings and activity) and records findings in it; an agent holding one can
// subscribe a webhook to every trigger of the project. Keys live under the project's Settings, the
// agents that joined under its Agents, next to the Tares agents.

export function ProjectKeysPanel({ project }: { project: string }) {
  const { data, error, reload } = usePolling(() => api.projectKeys(project), 30000);
  const [name, setName] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [minted, setMinted] = useState<{ name: string; secret: string }>();
  const [revoking, setRevoking] = useState<ApiKey>();

  const create = async () => {
    setBusy(true); setErr(undefined);
    try {
      const r = await api.createProjectKey(project, name.trim());
      setMinted({ name: r.name, secret: r.secret }); setName(""); setAdding(false); reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <>
      <div className="pagehead" style={{ marginTop: 24 }}>
        <h2 style={{ margin: 0 }}>Keys</h2>
        {!adding && <button type="button" onClick={() => { setAdding(true); setErr(undefined); }}>New key</button>}
      </div>
      <p className="help" style={{ marginTop: 0, whiteSpace: "normal" }}>
        A key for an agent outside Tares, Claude Code with the Tares MCP server for one. It reads
        this project only, loads its skills, and records findings here; it can subscribe a webhook
        to the project's triggers.
      </p>
      {error && <ErrorState error={error} what="the keys" onRetry={reload} />}
      {data && !data.enforced && (
        <div className="alert">
          This instance has no <code>TARES_AUTH_TOKEN</code>, so keys are not checked: anyone who
          can reach it reads everything. Set a root auth token to make a key the limit.
        </div>
      )}
      {err && <div className="alert error">{err}</div>}
      {adding && (
        <div className="panel" style={{ marginBottom: 10 }}>
          <label className="field" style={{ maxWidth: 360 }}>
            <span className="lbl">name</span>
            <input type="text" autoFocus placeholder="e.g. claude-code" value={name}
                   onChange={(e) => setName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) create(); }} />
            <span className="help">who holds it; the findings it records carry this name</span>
          </label>
          <div className="btnrow">
            <button className="primary" disabled={busy || !name.trim()} onClick={create}>{busy ? "…" : "Create key"}</button>
            <button type="button" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {minted && (
        <div className="alert ok">
          Key <strong>{minted.name}</strong> created. Copy it now: it is not shown again.
          <div className="ingest-url" style={{ marginTop: 8 }}>
            <code className="mono">{minted.secret}</code>
            <CopySecret text={minted.secret} />
          </div>
          <button style={{ marginTop: 8 }} onClick={() => setMinted(undefined)}>Done</button>
        </div>
      )}
      {data && (data.keys.length ? (
        <table>
          <thead><tr><th>name</th><th>key</th><th>created</th><th>last used</th><th aria-label="actions" /></tr></thead>
          <tbody>
            {data.keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td className="mono">{k.prefix}…</td>
                <td className="help"><TimeAgo ts={k.created_at} /></td>
                <td className="help">{k.last_used_at ? <TimeAgo ts={k.last_used_at} /> : "never"}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="danger" onClick={() => setRevoking(k)}>Revoke</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : !adding && <p className="help">no keys for this project yet</p>)}
      {revoking && (
        <ConfirmDialog title={`Revoke ${revoking.name}?`}
          message="The key stops working at once and its webhook subscriptions to this project are removed. Findings it recorded stay."
          confirmLabel="Revoke" danger
          onCancel={() => setRevoking(undefined)}
          onConfirm={async () => {
            const k = revoking; setRevoking(undefined);
            try { await api.revokeKey(k.id); } catch (e) { setErr(String((e as Error).message ?? e)); }
            reload();
          }} />
      )}
    </>
  );
}

export function ExternalAgentsPanel({ project, onOpenKeys }: { project: string; onOpenKeys: () => void }) {
  const { data, error, reload } = usePolling(() => api.externalAgents(project), 15000);
  const [revoking, setRevoking] = useState<ExternalAgent>();
  const [err, setErr] = useState<string>();
  return (
    <div style={{ marginBottom: 28 }}>
      <h2 style={{ margin: "0 0 8px" }}>External agents</h2>
      {error && <ErrorState error={error} what="the external agents" onRetry={reload} />}
      {err && <div className="alert error">{err}</div>}
      {data && (data.agents.length ? (
        <table>
          <thead><tr><th>agent</th><th>delivers to</th><th>key</th><th>last delivery</th><th aria-label="actions" /></tr></thead>
          <tbody>
            {data.agents.map((a) => (
              <tr key={a.subscription_id}>
                <td><strong>{a.name}</strong></td>
                <td className="mono" style={{ wordBreak: "break-all" }}>{a.url}</td>
                <td>{a.key_name ?? <span className="dim">none</span>}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {a.last_delivery
                    ? <><TimeAgo ts={a.last_delivery.at} />
                        {a.last_delivery.ok === false &&
                          <span style={{ color: "var(--err)" }}> · failed{a.last_delivery.error ? `: ${a.last_delivery.error}` : ""}</span>}</>
                    : <span className="dim">none yet</span>}
                </td>
                <td style={{ textAlign: "right" }}>
                  <button className="danger" onClick={() => setRevoking(a)}>Revoke</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="help">
          no external agent has joined this project yet; give one a key under{" "}
          <a href="#keys" onClick={(e) => { e.preventDefault(); onOpenKeys(); }}>Settings, Keys</a>
        </p>
      ))}
      {revoking && (
        <ConfirmDialog title={`Stop delivering to ${revoking.name}?`}
          message="The project's triggers stop delivering to this webhook. The key keeps working; revoke it under Settings, Keys to cut the agent off entirely."
          confirmLabel="Revoke" danger
          onCancel={() => setRevoking(undefined)}
          onConfirm={async () => {
            const a = revoking; setRevoking(undefined);
            try { await api.unsubscribeProject(project, a.subscription_id); } catch (e) { setErr(String((e as Error).message ?? e)); }
            reload();
          }} />
      )}
    </div>
  );
}
