import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, fmtCost, fmtTokens, usePolling } from "./bits";
import type { ClaudeCodeUsage, ClaudePlan, ContextReading, ProjectUsage, StationContext, StationDetail, UsageTotals } from "../types";

// What the Claude Code sessions reporting here spend (tares/claude_usage.py). The crew's machine
// sends three readings: each station's status line (its context window, and on a Pro or Max login
// the plan's 5-hour and weekly limits), /context of a station when someone asks, and /usage every
// few minutes from the factory watch. Token counts come from the transcripts the plugin ships. Cost
// is at API list prices: on a subscription it compares projects, it is not a bill.

const HOT = 80;

function pctOf(n: number, of: number): number {
  return of > 0 ? Math.round((n * 1000) / of) / 10 : 0;
}

/** "in 2h 10m, at 19:09" or "in 3 days, Tue 02:59" */
function resetIn(iso: string | null): string {
  if (!iso) return "at a time not reported";
  const t = new Date(iso);
  const mins = Math.max(0, Math.round((t.getTime() - Date.now()) / 60000));
  const clock = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (mins < 60) return `in ${mins} min, at ${clock}`;
  if (mins < 24 * 60) return `in ${Math.floor(mins / 60)}h ${mins % 60}m, at ${clock}`;
  return `in ${Math.round(mins / 1440)} days, ${t.toLocaleDateString([], { weekday: "short" })} ${clock}`;
}

function Meter({ pct, title }: { pct: number | null; title?: string }) {
  const w = Math.min(100, Math.max(0, pct ?? 0));
  return (
    <div className="usage-bar" title={title} role="img" aria-label={title}>
      <span className={pct != null && pct >= HOT ? "hot" : undefined} style={{ width: `${w}%` }} />
    </div>
  );
}

function tokensLine(u: UsageTotals): string {
  return `${u.replies.toLocaleString()} replies · ${fmtTokens(u.cache_read_tokens)} read from cache · ` +
    `${fmtTokens(u.cache_write_tokens + u.input_tokens)} new input · ${fmtTokens(u.output_tokens)} output`;
}

// ── the plan ───────────────────────────────────────────────────────────────

export function PlanLimits({ plan }: { plan: ClaudePlan }) {
  return (
    <>
      <ul className="cc-limits">
        {plan.limits.map((l) => {
          const pct = l.reset ? 0 : Math.round(l.percent);
          return (
            <li key={l.kind}>
              <div className="cc-limit-head">
                <span>{l.label}</span>
                <b>{l.reset ? "started over" : `${pct}% used`}</b>
              </div>
              <Meter pct={pct} title={`${l.label}: ${l.reset ? "started over" : `${pct}% used, ${100 - pct}% left`}`} />
              <small className="dim">
                {l.reset ? <>Reset {resetIn(l.resets_at).replace(/^in [^,]+, /, "")}; nothing has read it since.</>
                  : <>{100 - pct}% left, resets {resetIn(l.resets_at)}.</>}
              </small>
            </li>
          );
        })}
      </ul>
      {plan.extra_usage?.is_enabled && (
        <p className="help" style={{ margin: "8px 0 0" }}>
          Extra usage is on{plan.extra_usage.monthly_limit != null && <>, up to {plan.extra_usage.monthly_limit} {plan.extra_usage.currency ?? ""} a month</>}: work goes on past a limit, billed.
        </p>
      )}
    </>
  );
}

function projectName(p: ProjectUsage) {
  return p.kind === "project" && p.project
    ? <Link to={`/projects/${encodeURIComponent(p.project)}`}>{p.name}</Link>
    : <span>{p.name}</span>;
}

const TOP = 5;

/** Each project's share of the current 5-hour window, by estimated cost: the largest few, the rest summed. */
function WindowShares({ rows }: { rows: ProjectUsage[] }) {
  if (!rows.length) return <p className="dim" style={{ margin: 0, fontSize: 13 }}>No replies in this window yet.</p>;
  const rest = rows.slice(TOP);
  const restShare = Math.round(rest.reduce((n, p) => n + (p.share ?? 0), 0) * 100);
  return (
    <ul className="cc-shares">
      {rest.length > 0 && <li className="dim cc-rest" style={{ order: 1 }}>{rest.length} more, {restShare}% together: {rest.map((p) => p.name).join(", ")}</li>}
      {rows.slice(0, TOP).map((p) => {
        const pct = Math.round((p.share ?? 0) * 100);
        return (
          <li key={`${p.kind}:${p.project ?? ""}`}>
            <div className="cc-share-head">{projectName(p)}<span className="dim">{pct}% · {fmtCost(p.cost_usd)}</span></div>
            <Meter pct={pct} title={`${p.name}: ${pct}% of this window's estimated cost, ${tokensLine(p)}`} />
          </li>
        );
      })}
    </ul>
  );
}

const PLAN_EMPTY = <>No reading yet. Each station reports the plan's 5-hour and weekly limits after its first reply (on a Pro or Max login), and the factory watch reads <span className="mono">/usage</span> every 10 minutes. Read it now with <span className="mono">tares factory plan</span>.</>;

/** The Crew page's view of the plan: how much of each limit is left, and who used this window. */
export function PlanPanel({ data, error, reload }: { data: ClaudeCodeUsage | undefined; error?: string; reload: () => void }) {
  return (
    <div className="fo-panel">
      <header><h3>Your Claude plan</h3>{data?.plan?.at && <span className="dim" title="from /usage, or a station's status line when that is newer">read <TimeAgo ts={data.plan.at} /></span>}</header>
      <div className="cc-body">
        {error && !data ? <ErrorState error={error} what="the plan's limits" onRetry={reload} />
          : !data ? <p className="dim" style={{ margin: 0 }}>loading…</p>
          : data.plan ? <PlanLimits plan={data.plan} />
          : <p className="help" style={{ margin: 0 }}>{PLAN_EMPTY}</p>}
        {data && (
          <div>
            <div className="crew-detail-label">This 5-hour window, by project (share of estimated cost)</div>
            <WindowShares rows={data.window.projects} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Settings, Usage: the plan, and what each project spent over the last week. */
export function ClaudeCodeUsagePanel() {
  const { data, error, reload } = usePolling(() => api.claudeCodeUsage(7), 30000);
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Claude Code</h2>
      {error && !data && <ErrorState error={error} what="Claude Code usage" onRetry={reload} />}
      {!data && !error && <div className="muted">loading…</div>}
      {data && (
        <>
          {data.plan ? <>{data.plan.at && <p className="help" style={{ marginTop: 0 }}>As the crew's machine last read it, <TimeAgo ts={data.plan.at} />.</p>}<PlanLimits plan={data.plan} /></>
            : <p className="help">{PLAN_EMPTY}</p>}
          <h3 className="cc-sub">Last {data.days} days, by project</h3>
          {data.projects.length === 0 ? (
            <p className="help" style={{ marginBottom: 0 }}>
              Nothing yet. The Tares plugin sends each session's replies with their token counts; install it in Claude Code to see them here.
            </p>
          ) : (
            <>
              <div className="cc-table-wrap">
                <table className="cc-table">
                  <thead>
                    <tr><th>Project</th><th className="num">Sessions</th><th className="num">Replies</th><th className="num">Read from cache</th><th className="num">New input</th><th className="num">Output</th><th className="num">Est. API cost</th><th className="num">Share</th></tr>
                  </thead>
                  <tbody>
                    {data.projects.map((p) => (
                      <tr key={`${p.kind}:${p.project ?? ""}`}>
                        <td>{projectName(p)}</td>
                        <td className="num">{p.sessions}</td>
                        <td className="num">{p.replies.toLocaleString()}</td>
                        <td className="num">{fmtTokens(p.cache_read_tokens)}</td>
                        <td className="num">{fmtTokens(p.cache_write_tokens + p.input_tokens)}</td>
                        <td className="num">{fmtTokens(p.output_tokens)}</td>
                        <td className="num">{fmtCost(p.cost_usd)}{p.unpriced_replies > 0 && <span className="dim" title="replies from a model without a known price, not in the cost"> +{p.unpriced_replies}</span>}</td>
                        <td className="num">{p.share != null ? `${Math.round(p.share * 100)}%` : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="help" style={{ margin: "8px 0 0" }}>
                Cost is what the tokens would cost on the API at list prices, so projects can be compared; on a subscription the plan's limits above are what runs out. A reply counts for the project its session was working in; the crew's own stations serve every project, so what they spent outside one is the crew's.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}

// ── a station's context window ─────────────────────────────────────────────

function contextTitle(ctx: StationContext): string {
  const win = `${fmtTokens(ctx.used)} of a ${fmtTokens(ctx.size)} window (${ctx.pct}%)`;
  return ctx.compact_at != null ? `context ${win}; it compacts at ${fmtTokens(ctx.compact_at)}, ${ctx.compact_pct}% of the way` : `context ${win}`;
}

/** A station's context in the member list: how near it is to compacting, else how full its window is. */
export function ContextBadge({ ctx }: { ctx: StationContext | null }) {
  const pct = ctx?.compact_pct ?? ctx?.pct;
  if (!ctx || pct == null) return null;
  return <span className={`cc-ctx${pct >= HOT ? " is-hot" : ""}`} title={contextTitle(ctx)}>ctx {pct}%</span>;
}

const KIND_ORDER: Record<string, number> = { used: 0, deferred: 1, buffer: 2, free: 3 };

function Breakdown({ r, size }: { r: ContextReading; size: number }) {
  const rows = [...r.categories].sort((a, b) => (KIND_ORDER[a.kind ?? ""] ?? 4) - (KIND_ORDER[b.kind ?? ""] ?? 4));
  return (
    <>
      <table className="cc-cats">
        <tbody>
          {rows.map((c) => {
            const pct = pctOf(c.tokens, size);
            return (
              <tr key={c.name} className={c.kind === "used" ? undefined : "dim"}>
                <td title={c.kind === "deferred" ? "listed, loaded only when used" : undefined}>{c.name}</td>
                <td className="num">{fmtTokens(c.tokens)}</td>
                <td className="num">{pct}%</td>
                <td className="cc-cat-bar"><Meter pct={c.kind === "used" ? pct : 0} title={`${c.name}: ${fmtTokens(c.tokens)} tokens, ${pct}% of the window`} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <details className="cc-loaded">
        <summary>What is loaded</summary>
        <dl>
          {r.mcp_servers.length > 0 && <><dt>MCP servers</dt><dd>{r.mcp_servers.map((m) => `${m.name} ${fmtTokens(m.tokens)} (${m.tools} tools)`).join(" · ")}</dd></>}
          {r.memory_files.length > 0 && <><dt>Memory files</dt><dd>{r.memory_files.map((m) => `${m.path} ${fmtTokens(m.tokens)}`).join(" · ")}</dd></>}
          <dt>Skills</dt><dd>{r.skills.count} skills, {fmtTokens(r.skills.tokens)}{r.skills.largest.length > 0 && <>; largest: {r.skills.largest.slice(0, 5).map((s) => `${s.name} ${fmtTokens(s.tokens)}`).join(" · ")}</>}</dd>
          <dt>Custom agents</dt><dd>{r.agents.count}, {fmtTokens(r.agents.tokens)}</dd>
        </dl>
      </details>
    </>
  );
}

/** A station's context window and what it spent, on its detail. */
export function StationUsage({ s, onAsked }: { s: StationDetail; onAsked: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ctx = s.context;
  const r = s.context_detail;
  const asked = s.context_asked_at ? new Date(s.context_asked_at).getTime() : 0;
  const waiting = asked > 0 && (!s.context_at || new Date(s.context_at).getTime() < asked)
    && Date.now() - asked < 15 * 60000;
  const ask = async () => {
    setBusy(true); setErr(null);
    try { await api.askStationContext(s.name); onAsked(); }
    catch (e) { setErr(String((e as Error)?.message ?? e)); }
    finally { setBusy(false); }
  };
  const size = r?.max_tokens ?? ctx?.size ?? 0;
  return (
    <div>
      <div className="crew-detail-label">Context window</div>
      {ctx ? (
        <>
          <p style={{ margin: "0 0 4px" }}>
            {ctx.compact_at != null
              ? <><b>{fmtTokens(ctx.used)}</b> tokens, {ctx.compact_pct}% of the way to compacting at {fmtTokens(ctx.compact_at)} <span className="dim">(window {fmtTokens(ctx.size)}, {ctx.pct}% full)</span></>
              : <><b>{fmtTokens(ctx.used)}</b> of {fmtTokens(ctx.size)} tokens ({ctx.pct}%)</>}
            {ctx.model && <> · {ctx.model}</>}
            <span className="dim"> · reported <TimeAgo ts={ctx.at} /></span>
          </p>
          <Meter pct={ctx.compact_pct ?? ctx.pct} title={contextTitle(ctx)} />
        </>
      ) : (
        <p className="dim" style={{ margin: 0 }}>No report yet. A station reports after each reply once it runs on this version of the plugin; the watch moves idle stations onto it.</p>
      )}
      <div className="cc-ctx-read">
        {r ? <span className="dim">By category, read <TimeAgo ts={s.context_at} /> with <span className="mono">/context</span>:</span>
          : <span className="dim">The window by category comes from <span className="mono">/context</span>, read on the crew's machine without a model call.</span>}
        <button type="button" disabled={busy || waiting} onClick={ask}>{waiting ? "Reading…" : r ? "Read again" : "Read it"}</button>
      </div>
      {waiting && (
        <p className="help" style={{ margin: "4px 0 0", fontSize: 13 }}>
          Asked <TimeAgo ts={s.context_asked_at} />. The factory watch reads it within seconds; if the watch is not running, run <span className="mono">tares factory context {s.name}</span>.
        </p>
      )}
      {err && <p className="fo-warn" style={{ margin: "4px 0 0" }}>{err}</p>}
      {r && size > 0 && <Breakdown r={r} size={size} />}
      {s.usage && (
        <p className="dim" style={{ margin: "8px 0 0", fontSize: 13 }}>
          Its sessions so far: {tokensLine(s.usage)} · about {fmtCost(s.usage.cost_usd)} at API prices.
        </p>
      )}
    </div>
  );
}
