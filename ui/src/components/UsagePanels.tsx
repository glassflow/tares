import { api } from "../api";
import { ErrorState, fmtCost, fmtTokens, formatBytes, usePolling } from "./bits";
import type { ModelUsage, Usage } from "../types";

// The whole instance's numbers: what it spent on model providers and how full its storage is.
// They lived on Overview; they are in Settings now (Usage when self-hosted, Workspace on Tares
// Cloud), next to the limits and keys they are about. UsagePanels loads its own data.

/** Model spend and storage, loading their own data. `storage: false` leaves storage out (Tares
 *  Cloud shows the workspace's storage from the control plane instead). */
export function UsagePanels({ storage = true }: { storage?: boolean }) {
  const { data: u, error: usageError, reload: reloadUsage } = usePolling(() => api.usage(), 30000);
  const { data: mu, error: muError, reload: reloadMu } = usePolling(() => api.modelUsage(30), 30000);
  const onDisk = u ? u.db_bytes + u.wal_bytes : 0;
  // Both are null together, but it's the denominator that decides whether a percentage means
  // anything: an instance with no TARES_MAX_DB_SIZE has nothing to be a percentage of.
  const pct = u && u.max_bytes != null && u.pct_used != null ? u.pct_used : null;
  return (
    <>
      <ModelSpendPanel usage={mu} error={muError} reload={reloadMu} />
      {storage && <StoragePanel usage={u} error={usageError} reload={reloadUsage} onDisk={onDisk} pct={pct} />}
    </>
  );
}

// What has this instance spent on its model providers? The whole-instance counterpart of the
// per-agent cost cards. Covers everything that burns the key inside the cell (Tares agent runs
// and Ask); external agents run on their own keys and are deliberately absent. The total is a
// floor: runs from before cost tracking, and models without a known price, carry no cost —
// said out loud via the uncosted note rather than silently folded into $0.
export function ModelSpendPanel({ usage, error, reload }: {
  usage: ModelUsage | undefined;
  error?: string;
  reload: () => void;
}) {
  const m = usage;
  const today = m?.days.length ? m.days[m.days.length - 1] : null;
  const windowCost = m?.days.reduce((s, d) => s + (d.cost_usd ?? 0), 0) ?? 0;
  const agent = m?.by_surface.agent;
  const ask = m?.by_surface.ask;
  const uncosted = m?.total.uncosted_calls ?? 0;
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Model spend</h2>

      {error && <ErrorState error={error} what="model spend" onRetry={reload} />}
      {!m && !error && <div className="muted">loading…</div>}

      {m && (m.total.calls === 0 ? (
        <p className="help" style={{ marginBottom: 0 }}>
          Nothing spent yet; the meter starts counting with the first agent run or Ask question.
        </p>
      ) : (
        <>
          <div className="cards" style={{ marginBottom: 8 }}>
            <div className="card">
              <div className="k">all time</div>
              <div className="v">{fmtCost(m.total.cost_usd)}</div>
            </div>
            <div className="card">
              <div className="k">last {m.window_days} days</div>
              <div className="v">{fmtCost(windowCost)}</div>
            </div>
            <div className="card">
              <div className="k">today</div>
              <div className="v">{today ? fmtCost(today.cost_usd ?? 0) : fmtCost(0)}</div>
            </div>
          </div>
          <p className="help" style={{ marginBottom: 0 }}>
            {agent && <>agents {fmtCost(agent.cost_usd)} over {agent.calls.toLocaleString()} model call{agent.calls === 1 ? "" : "s"}</>}
            {agent && ask && " · "}
            {ask && <>Ask {fmtCost(ask.cost_usd)} over {ask.calls.toLocaleString()} call{ask.calls === 1 ? "" : "s"}</>}
            {" · "}{fmtTokens(m.total.input_tokens)} tokens in, {fmtTokens(m.total.output_tokens)} out
            {uncosted > 0 && <> · {uncosted} call{uncosted === 1 ? "" : "s"} on an unpriced model, not in the total</>}
          </p>
          {m.by_provider && Object.keys(m.by_provider).length > 1 && (
            <p className="help" style={{ margin: "6px 0 0" }}>
              by provider: {Object.entries(m.by_provider).map(([id, b], i) => (
                <span key={id}>{i > 0 && " · "}<span className="mono">{id}</span> {fmtCost(b.cost_usd)} over {b.calls.toLocaleString()} call{b.calls === 1 ? "" : "s"}</span>
              ))}
            </p>
          )}
        </>
      ))}
    </div>
  );
}

// How full is my database? — asked *before* it breaks, not after. Which story you get depends on
// whether an operator configured a cap (TARES_MAX_DB_SIZE; the Helm chart sets it for hosted
// cells, a self-hosted install usually has not):
//   · cap set  → the percentage of it, a bar, and a warning from 80% up. The daemon only flips
//                /health to `degraded` at TARES_DEGRADED_PCT (90 by default); the console warns
//                earlier, while there is still room to act.
//   · no cap   → no percentage and no bar, because there is no denominator to measure against.
//                Absolute size, with headroom taken from the free space on the volume instead.
// pct_used is on a 0-100 scale, so the threshold is 80, not 0.8. Per-source bytes are deliberately
// absent: DuckDB keeps every source in one events table and cannot attribute storage per source.
export function StoragePanel({ usage, error, reload, onDisk, pct }: {
  usage: Usage | undefined;
  error?: string;
  reload: () => void;
  onDisk: number;
  pct: number | null;
}) {
  const u = usage;
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Storage</h2>

      {error && <ErrorState error={error} what="storage usage" onRetry={reload} />}
      {!u && !error && <div className="muted">loading…</div>}

      {u && (
        <>
          {u.ingest_paused ? (
            <div className="alert error">
              <strong>Ingest is paused: storage {pct}% full.</strong> Producers get 507 and poll
              connectors wait, so the database never fills its disk. Reads, agents and the console
              keep working. Grow the volume or delete a source's events; ingest resumes on its own
              once there is room.
            </div>
          ) : pct !== null && pct >= 80 && (
            <div className="alert warn">
              <strong>Storage {pct}% full</strong> · {formatBytes(onDisk)} of the{" "}
              {formatBytes(u.max_bytes)} {u.max_bytes_source === "volume" ? "volume" : "limit"} for this
              instance. At 95% ingest pauses; grow the volume or free space before it does.
            </div>
          )}

          {pct !== null ? (
            <>
              <div className="usage-bar" aria-hidden="true">
                <span className={pct >= 80 ? "hot" : undefined}
                      style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
              </div>
              <p className="help" style={{ marginBottom: 0 }}>
                <strong>{formatBytes(onDisk)}</strong> of {formatBytes(u.max_bytes)}
                {u.max_bytes_source === "volume" ? " on the volume" : " limit"} used ({pct}%)
                {u.max_bytes_source === "env" && u.disk_free != null && <> · {formatBytes(u.disk_free)} free on the volume</>}
              </p>
            </>
          ) : (
            // No cap configured: no percentage, no bar — there is nothing to be a percentage of.
            <p className="help" style={{ marginBottom: 0 }}>
              <strong>{formatBytes(onDisk)}</strong> on disk. No size limit is configured for this
              instance, so there is no percentage to show.{" "}
              {u.disk_free != null
                ? <>The volume it sits on has <strong>{formatBytes(u.disk_free)}</strong> free
                   {u.disk_total != null && <> of {formatBytes(u.disk_total)}</>}.</>
                : <>Free space on its volume could not be read.</>}{" "}
              Set <code>TARES_MAX_DB_SIZE</code> to be warned against a budget instead.
            </p>
          )}
        </>
      )}
    </div>
  );
}
