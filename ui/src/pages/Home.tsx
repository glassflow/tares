import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, fmtCost, fmtTokens, formatBytes, usePolling } from "../components/bits";
import type { ModelUsage, Usage, Usecase } from "../types";

// The instance at a glance. This exists because the numbers that describe the WHOLE instance —
// how full the disk is, how much has been ingested, how hard the agents are working — were living
// at the bottom of the Sources page, under a list that is 10 rows on a real cell and pushes them
// below the fold. They aren't source data; they're instance data, and the source list is now just
// a source list.
//
// Numbers only, deliberately: no activity feed. A feed makes the page busier than the question it
// answers ("is this instance healthy and how full is it?"), and Activity already exists for the
// narrative view.
//
// Degraded state is NOT surfaced here — AuthGate already renders a global banner when /health is
// anything but ok, and a second copy would just be one more thing to keep in sync.

// An instance with no sources has nothing for Overview to say — every number is zero. On the
// first landing of a page load we send that user to Sources, where the next step actually is.
// Once per page load only (module flag, not state): clicking Overview in the nav afterwards must
// still show the page, and polling must not bounce you away if sources later drop to zero.
let emptyRedirectDone = false;

export default function Home() {
  const { data: u, error: usageError, reload: reloadUsage } = usePolling(() => api.usage(), 30000);
  const { data: sources, error: sourcesError } = usePolling(() => api.sources(), 30000);
  const { data: uc } = usePolling(() => api.usecases(), 30000);
  const { data: mu, error: muError, reload: reloadMu } = usePolling(() => api.modelUsage(30), 30000);
  const navigate = useNavigate();

  useEffect(() => {
    if (emptyRedirectDone || !sources) return;   // a failed load never counts as "no sources"
    emptyRedirectDone = true;
    if (sources.length === 0) navigate("/sources", { replace: true });
  }, [sources, navigate]);

  const onDisk = u ? u.db_bytes + u.wal_bytes : 0;
  // Both are null together, but it's the denominator that decides whether a percentage means
  // anything — an instance with no TARES_MAX_DB_SIZE has nothing to be a percentage of.
  const pct = u && u.max_bytes != null && u.pct_used != null ? u.pct_used : null;
  const erroring = sources?.filter((s) => s.health?.status === "error").length ?? 0;

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Overview</h1>
          <p className="subtitle">
            what this instance holds; <em>and how much room is left</em>
          </p>
        </div>
      </div>

      {/* One row for the whole instance. `events` comes from the metering endpoint rather than
          summing the source list: it is the same number, but maintained as a counter rather than
          recomputed here, and it still reads correctly while /api/sources is failing. */}
      <div className="cards">
        <div className="card">
          <div className="k">sources</div>
          <div className="v">{sources ? sources.length : <span className="dim">—</span>}</div>
        </div>
        <div className="card">
          <div className="k">events stored</div>
          <div className="v">{u ? u.events.toLocaleString() : <span className="dim">—</span>}</div>
        </div>
        <div className="card">
          <div className="k">erroring</div>
          <div className="v" style={erroring ? { color: "var(--err)" } : undefined}>
            {sources ? erroring : <span className="dim">—</span>}
          </div>
        </div>
        <div className="card">
          <div className="k">agent runs</div>
          <div className="v">{u ? u.agent_runs.toLocaleString() : <span className="dim">—</span>}</div>
        </div>
        <div className="card">
          <div className="k">dispatch deliveries</div>
          <div className="v">
            {u ? u.dispatch_deliveries.toLocaleString() : <span className="dim">—</span>}
          </div>
        </div>
      </div>

      {/* A failed load is never rendered as a zero: the cards above fall back to "—", and the
          reason is said out loud here. See the rule in components/bits.tsx. */}
      {sourcesError && <ErrorState error={sourcesError} what="the source list" />}

      {uc && uc.usecases.length > 0 && <UsecasesPanel usecases={uc.usecases} />}

      <ModelSpendPanel usage={mu} error={muError} reload={reloadMu} />

      <StoragePanel
        usage={u}
        error={usageError}
        reload={reloadUsage}
        onDisk={onDisk}
        pct={pct}
      />

      {sources && sources.length === 0 && !sourcesError && (
        <div className="empty">
          Nothing is being ingested yet; <Link to="/sources/new">add a source</Link> to start
          filling the timeline.
        </div>
      )}
    </>
  );
}

// What has this instance spent on its Anthropic key? — the whole-instance counterpart of the
// per-agent cost cards. Covers everything that burns the key inside the cell (Tares agent runs
// and Ask); external agents run on their own keys and are deliberately absent. The total is a
// floor: runs from before cost tracking, and models without a known price, carry no cost —
// said out loud via the uncosted note rather than silently folded into $0.
function ModelSpendPanel({ usage, error, reload }: {
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
      <p className="help" style={{ marginTop: 0 }}>
        What this instance has spent on its Anthropic key; Tares agent runs plus Ask. External
        agents run on their own keys and are not counted here.
      </p>

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
function StoragePanel({ usage, error, reload, onDisk, pct }: {
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
      <p className="help" style={{ marginTop: 0 }}>
        What this instance is using on disk; the DuckDB file plus its write-ahead log. Nothing is
        pruned today, so agent runs and dispatch deliveries grow with every firing.
      </p>

      {error && <ErrorState error={error} what="storage usage" onRetry={reload} />}
      {!u && !error && <div className="muted">loading…</div>}

      {u && (
        <>
          {pct !== null && pct >= 80 && (
            <div className="alert warn">
              <strong>Storage {pct}% full</strong> · {formatBytes(onDisk)} of the{" "}
              {formatBytes(u.max_bytes)} limit for this instance. Ingest keeps working until it
              runs out; free space or raise <code>TARES_MAX_DB_SIZE</code> before it does.
            </div>
          )}

          {pct !== null ? (
            <>
              <div className="usage-bar" aria-hidden="true">
                <span className={pct >= 80 ? "hot" : undefined}
                      style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
              </div>
              <p className="help" style={{ marginBottom: 0 }}>
                <strong>{formatBytes(onDisk)}</strong> of {formatBytes(u.max_bytes)} used ({pct}%)
                {u.disk_free != null && <> · {formatBytes(u.disk_free)} free on the volume</>}
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

// Use cases at a glance: one line per instance, because a use case is the thing a user set up on
// purpose and the first thing they will ask "is it running?" about.
function UsecasesPanel({ usecases }: { usecases: Usecase[] }) {
  return (
    <div className="panel">
      <div className="pagehead" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Use cases</h2>
        <Link className="btn" to="/usecases">All use cases</Link>
      </div>
      <table>
        <tbody>
          {usecases.map((u) => {
            const missing = u.objects.filter((o) => o.missing).length;
            return (
              <tr key={u.id}>
                <td><Link to={`/usecases/${encodeURIComponent(u.id)}`}><strong>{u.name}</strong></Link></td>
                <td className="help">{u.recipe_title}</td>
                <td>
                  <span className={`badge ${u.status === "active" ? "ok" : u.status === "paused" ? "paused" : "error"}`}>{u.status}</span>
                  {missing > 0 && <span className="help" style={{ marginLeft: 6 }}>{missing} missing</span>}
                </td>
                <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={u.updated_at} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
