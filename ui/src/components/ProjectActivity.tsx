import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import { ErrorState, Picker, TimeAgo, fmtCost, usePolling } from "./bits";
import { ResultChips, SkillChips, statusBadge } from "../pages/AgentDetail";
import type { AgentRun } from "../types";

// The project's Activity tab (TR-331): one row per thread, a firing or a run nothing fired,
// expandable in place to what it carried and everything it led to. Runs nest what came of them:
// a rerun under the run it repeats, a handoff under the run that handed off, and a firing its
// finding tripped under that run, with the runs that firing woke.

export type TimelineRun = AgentRun & {
  woken_by?: "trigger" | "schedule" | "manual" | "rerun" | "bootstrap" | "handoff" | null;
  parent_run_id?: string | null;
  children: TimelineRun[];
  firings: TimelineThread[];
};

export interface TimelineThread {
  id: string;
  kind: "firing" | "run";
  at: string;
  trigger?: string | null;
  entity?: string | null;
  fired?: {
    dispatch_id: string; trigger: string; kind: "condition" | "schedule"; key: string;
    fired_at: string; payload_excerpt: string; subscribers: number | null;
  } | null;
  runs: TimelineRun[];
  deliveries: { kind: "tares" | "slack" | "webhook"; target: string; ok: boolean | null;
                error: string | null; at: string | null }[];
}

const OUTCOMES = ["", "finding", "no_op", "failed", "capped", "empty", "exhausted", "running", "none"];
const OUTCOME_LABELS: Record<string, string> = {
  "": "all outcomes", finding: "found something", no_op: "nothing to report", failed: "failed",
  capped: "stopped by a cap", empty: "no conclusion", exhausted: "out of rounds",
  running: "running", none: "woke no agent",
};
// why a run started, where the thread around it does not already say
const WOKEN: Record<string, string> = {
  manual: "started by hand", bootstrap: "first look at a new project", rerun: "rerun",
  handoff: "handed off",
};

/** Every run in a thread, nested ones included, for the row summary. */
function allRuns(runs: TimelineRun[]): TimelineRun[] {
  return runs.flatMap((r) => [r, ...allRuns(r.children), ...r.firings.flatMap((f) => allRuns(f.runs))]);
}

export default function ProjectActivity({ id, triggers, agents, onShowTrigger }: {
  id: string;
  triggers: string[];
  agents: string[];
  onShowTrigger: (name: string) => void;
}) {
  const [trigger, setTrigger] = useState("");
  const [agent, setAgent] = useState("");
  const [outcome, setOutcome] = useState("");
  const [entity, setEntity] = useState("");
  const [older, setOlder] = useState<TimelineThread[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null | undefined>(undefined);
  const [loadingMore, setLoadingMore] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const filters = { trigger, agent, outcome, entity };
  const { data: head, error, reload } = usePolling(() => api.projectTimeline(id, filters), 10000);
  useEffect(() => { setOlder([]); setOlderCursor(undefined); reload(); },
    [trigger, agent, outcome, entity]);   // eslint-disable-line react-hooks/exhaustive-deps

  const seen = new Set<string>();
  const threads = head === undefined ? undefined
    : [...head.threads, ...older].filter((t) => (seen.has(t.id) ? false : (seen.add(t.id), true)));
  // the next page starts where the last one loaded ended
  const cursor = olderCursor === undefined ? head?.next_before : olderCursor;
  const more = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await api.projectTimeline(id, { ...filters, before: cursor });
      setOlder((o) => [...o, ...page.threads]);
      setOlderCursor(page.next_before);
    } catch { setOlderCursor(null); }
    setLoadingMore(false);
  };
  const toggle = (tid: string) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(tid)) n.delete(tid); else n.add(tid);
    return n;
  });
  const entities = [...new Set([...(threads ?? []).map((t) => t.entity ?? ""), entity].filter(Boolean))].sort();
  const filtered = !!(trigger || agent || outcome || entity);

  return (
    <>
      {error && <ErrorState error={error} what="the activity" onRetry={reload} />}
      <div className="btnrow" style={{ marginBottom: 10 }}>
        <Picker value={trigger} ariaLabel="trigger" style={{ width: 200 }}
                options={["", ...triggers]} labels={{ "": "all triggers" }} onChange={setTrigger} />
        <Picker value={agent} ariaLabel="agent" style={{ width: 200 }}
                options={["", ...agents]} labels={{ "": "all agents" }} onChange={setAgent} />
        <Picker value={outcome} ariaLabel="outcome" style={{ width: 190 }}
                options={OUTCOMES} labels={OUTCOME_LABELS} onChange={setOutcome} />
        <Picker value={entity} ariaLabel="entity" style={{ width: 200 }}
                options={["", ...entities]} labels={{ "": "all entities" }} onChange={setEntity} />
        {filtered && (
          <button type="button" className="linklike"
                  onClick={() => { setTrigger(""); setAgent(""); setOutcome(""); setEntity(""); }}>clear</button>
        )}
      </div>
      {threads?.length ? (
        <table>
          <thead><tr><th>when</th><th>what</th><th>entity</th><th>agents</th><th /></tr></thead>
          <tbody>
            {threads.map((t) => (
              <ThreadRow key={t.id} t={t} open={open.has(t.id)} onToggle={() => toggle(t.id)}
                         onShowTrigger={onShowTrigger} knownTriggers={triggers} />
            ))}
          </tbody>
        </table>
      ) : threads && !error && (
        <div className="empty">{filtered ? "nothing matches these filters"
          : "nothing has happened in this project yet: activity shows up here when a trigger fires or an agent runs"}</div>
      )}
      {threads && threads.length > 0 && cursor && (
        <div className="btnrow" style={{ marginTop: 8 }}>
          <button onClick={more} disabled={loadingMore}>{loadingMore ? "loading…" : "Load older"}</button>
        </div>
      )}
    </>
  );
}

function ThreadRow({ t, open, onToggle, onShowTrigger, knownTriggers }: {
  t: TimelineThread; open: boolean; onToggle: () => void;
  onShowTrigger: (name: string) => void; knownTriggers: string[];
}) {
  const runs = allRuns(t.runs);
  const first = t.runs[0];
  return (
    <>
      <tr className="clickable" onClick={onToggle}>
        <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={t.at} /></td>
        <td>
          {t.kind === "firing" && t.fired
            ? <>{knownTriggers.includes(t.fired.trigger)
                  ? <a href={`#trigger-${t.fired.trigger}`} className="mono" title="the trigger, on the Setup tab"
                       onClick={(e) => { e.preventDefault(); e.stopPropagation(); onShowTrigger(t.fired!.trigger); }}>{t.fired.trigger}</a>
                  : <span className="mono">{t.fired.trigger}</span>}
                <span className="help"> {t.fired.kind === "schedule" ? "ticked" : "fired"}</span></>
            : first && <><span className="mono">{first.agent}</span>
                <span className="help"> {WOKEN[first.woken_by ?? ""] ?? "ran, cause unknown"}</span></>}
        </td>
        <td className="mono">{t.kind === "firing" && t.fired?.kind === "schedule" ? <span className="help">all</span> : t.entity}</td>
        <td>
          {runs.length
            ? runs.map((r) => <span key={r.id} style={{ marginRight: 6, whiteSpace: "nowrap" }}>
                <span className="mono">{r.agent}</span> {statusBadge(r)}</span>)
            : t.fired && (t.fired.subscribers ?? 0) === 0
              ? <span className="help">nobody subscribed</span>
              : t.deliveries.length > 0 &&
                <span className="help">sent to {t.deliveries.map((d) => d.kind === "slack" ? `Slack ${d.target}` : d.target).join(", ")}</span>}
        </td>
        <td className="dim">{open ? "▾" : "▸"}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} style={{ background: "var(--wash, transparent)" }}>
            <div style={{ padding: "8px 4px" }}>
              {t.fired && <FiringBody t={t} />}
              {t.runs.map((r) => <RunBlock key={r.id} r={r} />)}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function FiringBody({ t }: { t: TimelineThread }) {
  const f = t.fired!;
  return (
    <div style={{ marginBottom: 10 }}>
      <p className="help" style={{ margin: "0 0 6px", whiteSpace: "normal" }}>
        <Link to={`/dispatches/${encodeURIComponent(f.dispatch_id)}`}>the firing</Link>
        {f.kind === "schedule" ? " on its schedule" : <> on <span className="mono">{f.key}</span></>}
        {" · "}<TimeAgo ts={f.fired_at} />
      </p>
      {f.payload_excerpt && (
        <pre className="mono" style={{ whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto", margin: "0 0 8px", fontSize: 12 }}>
          {f.payload_excerpt}{f.payload_excerpt.length >= 600 ? "\n…" : ""}
        </pre>
      )}
      {t.deliveries.length > 0 && (
        <p style={{ margin: "0 0 6px" }}>
          <span className="help">delivered to:</span>
          {t.deliveries.map((d, i) => (
            <span key={i} className="chip mono" style={{ marginLeft: 4, color: d.ok === false ? "var(--err)" : undefined }}
                  title={d.ok === null ? "still running" : d.ok ? "delivered" : (d.error ?? "not delivered")}>
              {d.kind === "slack" ? `Slack ${d.target}` : d.target}
              {d.ok === false && d.error ? `: ${d.error}` : ""}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function RunBlock({ r, depth = 0 }: { r: TimelineRun; depth?: number }) {
  const woken = WOKEN[r.woken_by ?? ""];
  return (
    <div style={{ marginLeft: depth ? 18 : 0, paddingLeft: depth ? 12 : 0,
                  borderLeft: depth ? "2px solid var(--line)" : undefined, marginBottom: 10 }}>
      <p style={{ margin: "0 0 6px" }}>
        <span className="mono">{r.agent}</span> {statusBadge(r)}
        <ResultChips r={r} /><SkillChips r={r} />
        <span className="help">
          {woken && <> · {woken}</>}
          {r.key && <> · on <span className="mono">{r.key}</span></>}
          {" · "}<TimeAgo ts={r.started_at} />
          {r.duration_ms != null && <> · {(r.duration_ms / 1000).toFixed(1)}s</>}
          {r.cost_usd != null && <> · {fmtCost(r.cost_usd)}</>}
          {" · "}<Link to={`/agents/${encodeURIComponent(r.agent)}?run=${encodeURIComponent(r.id)}`}>the run</Link>
        </span>
      </p>
      {r.finding
        ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{r.finding}</ReactMarkdown></div>
        : <p className="help" style={{ margin: 0, whiteSpace: "normal" }}>
            {r.status === "running" ? "investigating…" : (r.error ?? "no finding")}</p>}
      {r.children.map((c) => <RunBlock key={c.id} r={c} depth={depth + 1} />)}
      {r.firings.map((f) => (
        <div key={f.id} style={{ marginLeft: 18, paddingLeft: 12, borderLeft: "2px solid var(--line)", marginTop: 8 }}>
          <p style={{ margin: "0 0 6px" }}>
            <span className="help">its finding fired </span>
            <span className="mono">{f.trigger}</span>
            <span className="help"> · <Link to={`/dispatches/${encodeURIComponent(f.id)}`}>the firing</Link>
              {" · "}<TimeAgo ts={f.at} /></span>
          </p>
          {f.runs.length
            ? f.runs.map((c) => <RunBlock key={c.id} r={c} />)
            : <p className="help" style={{ margin: 0 }}>it woke no Tares agent</p>}
        </div>
      ))}
    </div>
  );
}
