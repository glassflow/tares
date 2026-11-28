import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, fmtCost, fmtTokens, usePolling } from "./bits";
import type { ClaudePlan, ProjectUsage } from "../types";

// What the Claude Code sessions reporting here spend (tares/claude_usage.py). The crew's machine
// sends three readings: each station's status line (its context window, and on a Pro or Max login
// the plan's 5-hour and weekly limits), /context of a station when someone asks, and /usage every
// few minutes from the factory watch. Token counts come from the transcripts the plugin ships. Cost
// is at API list prices: on a subscription it compares projects, it is not a bill.

const HOT = 80;

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

const PLAN_EMPTY = <>No reading yet. Each station reports the plan's 5-hour and weekly limits after its first reply (on a Pro or Max login), and the factory watch reads <span className="mono">/usage</span> every 10 minutes. Read it now with <span className="mono">tares factory plan</span>.</>;

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
