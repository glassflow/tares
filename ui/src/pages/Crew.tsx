import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, usePolling } from "../components/bits";
import type { CrewSettings, CrewStation } from "../types";

// The crew of a tares-factory (M3, TR-422): one always-on crew (orchestrator, reviewer, releaser)
// serves every factory project, and builders come and go per project. Tares runs none of it: the
// sessions run on the person's machine (`factory crew up`), and this page shows what Tares hears
// from them. A station is a role with a stable name; the sessions that played it before the
// current one are folded under it as replaced.

const ROLE_TEXT: Record<string, string> = {
  orchestrator: "The only one you talk to: hands out work, answers the others, brings you what needs you.",
  reviewer: "Reviews every pull request to a fixed bar and posts the verdict.",
  releaser: "Ships milestones: tags, deploys, verifies, rolls back.",
  builder: "Takes a project's tickets to merged pull requests.",
  helper: "Started by a station for one job.",
  comms: "Talks to people (not in the first crew).",
};

const AUTONOMY_TEXT: Record<string, string> = {
  "L3-review": "you approve each merge",
  "L4-ship": "a builder merges after the reviewer's pass and green CI",
  "L5-dark": "the releaser may deploy to prod after verifying, and tells you",
};

export function CrewState({ s }: { s: CrewStation }) {
  const title = s.state === "quiet" ? `nothing sent for ${s.quiet_minutes} min` : s.state_reason ?? undefined;
  const cls = s.state === "working" ? "fo-st-in_progress" : s.state === "waiting" ? "fo-st-wait"
    : s.state === "quiet" ? "fo-st-quiet" : "";
  return <span className={`fo-st ${cls}`} title={title}>{s.state}</span>;
}

function StationRow({ s, all, depth = 0 }: { s: CrewStation; all: CrewStation[]; depth?: number }) {
  const kids = all.filter((x) => x.parent === s.name);
  return (
    <>
      <li className="crew-row" style={{ paddingLeft: 16 + depth * 22 }}>
        <div className="crew-main">
          <span className="crew-name">{s.name}</span>
          <span className="crew-role">{s.role ?? "station"}</span>
          {s.project_name && <Link className="crew-proj" to={`/projects/${encodeURIComponent(s.project ?? "")}`}>{s.project_name}</Link>}
        </div>
        <div className="crew-side">
          <small>{s.last_at ? <>last spoke <TimeAgo ts={s.last_at} /></> : "silent"}</small>
          <CrewState s={s} />
        </div>
        {depth === 0 && s.role && ROLE_TEXT[s.role] && <p className="crew-what">{ROLE_TEXT[s.role]}</p>}
        {s.earlier.length > 0 && (
          <details className="crew-earlier">
            <summary>{s.earlier.length} earlier {s.earlier.length === 1 ? "session" : "sessions"}, replaced</summary>
            <ul>
              {s.earlier.map((e) => (
                <li key={e.session}><span className="mono">{e.session.slice(0, 8)}</span> · {e.started_at ? <TimeAgo ts={e.started_at} /> : "?"} · {e.lines} lines · replaced</li>
              ))}
            </ul>
          </details>
        )}
      </li>
      {kids.map((k) => <StationRow key={k.name} s={k} all={all} depth={depth + 1} />)}
    </>
  );
}

function Settings({ st, grants }: { st: CrewSettings; grants: string }) {
  const lines = grants.split("\n").filter((l) => l.startsWith("- ")).map((l) => l.slice(2));
  return (
    <div className="fo-panel crew-settings">
      <h2>How far the crew may go</h2>
      <dl>
        <dt>Autonomy</dt>
        <dd>{st.autonomy ? <><b>{st.autonomy}</b>: {AUTONOMY_TEXT[st.autonomy]}</> : <span className="dim">not set: the first <span className="mono">factory crew up</span> asks</span>}</dd>
        <dt>Release</dt><dd>{st.release_profile ?? <span className="dim">not set</span>}</dd>
        <dt>Counts as prod</dt><dd>{st.prod_pattern ?? <span className="dim">not set</span>}</dd>
        <dt>Codex challenger on builders</dt><dd>{st.challenger === null ? <span className="dim">not set</span> : st.challenger ? "on" : "off"}</dd>
      </dl>
      <h2>Grants for every project</h2>
      {lines.length ? <ul className="crew-grants">{lines.map((g, i) => <li key={i}>{g}</li>)}</ul>
        : <p className="dim">None yet. A grant is kept only in your own words, with the day you said it.</p>}
    </div>
  );
}

export default function Crew() {
  const { data, error, reload } = usePolling(() => api.crew(), 10000);
  if (error && !data) return <ErrorState error={error} what="the crew" onRetry={reload} />;
  const stations = data?.stations ?? [];
  const roots = stations.filter((s) => !s.parent || !stations.some((x) => x.name === s.parent));
  // the orchestrator waiting is you being asked something; another station waiting between jobs
  // is its normal state. Any station gone quiet may have been retired or crashed.
  const attention = stations.filter((s) =>
    s.state === "quiet" || (s.state === "waiting" && s.role === "orchestrator"));
  return (
    <div className="crew">
      <div className="pagehead">
        <div>
          <h1>Crew</h1>
          <p className="subtitle">One crew serves every factory project. It runs on your machine; Tares shows what it hears from it.</p>
        </div>
      </div>

      <Desk />

      {attention.map((s) => (
        <section key={s.name} className={`fo-attn fo-attn-${s.state === "waiting" ? "waiting" : "silent"}`} role="status">
          <h2>{s.state === "waiting" ? <>{s.name} is waiting{s.state_at && <> since <TimeAgo ts={s.state_at} /></>}</>
            : <>No word from {s.name} for {s.quiet_minutes} min</>}</h2>
          <p className="help" style={{ margin: 0 }}>
            {s.state === "waiting"
              ? <>{sentence(s.state_reason ?? "it waits for input")} {s.role === "orchestrator" ? <>Answer it with <span className="mono">factory attach orchestrator</span>.</> : null}</>
              : <>It may have been retired by Claude Code or crashed. <span className="mono">factory watch</span> brings it back; Tares only shows it.</>}
          </p>
        </section>
      ))}

      {data === undefined ? <div className="dim">loading…</div> : stations.length ? (
        <div className="fo-panel">
          <ul className="crew-list">
            {roots.map((s) => <StationRow key={s.name} s={s} all={stations} />)}
          </ul>
        </div>
      ) : (
        <section className="fo-next fo-next-empty">
          <h2>No crew has reported yet</h2>
          <p className="help">Bring the crew up once, in a terminal, from a folder of its own:</p>
          <div className="fo-cmd"><code>factory crew up</code></div>
          <p className="help">It asks how far the crew may go alone, saves your answers here, and starts the orchestrator, the reviewer and the releaser. They show here as they start.</p>
        </section>
      )}

      <ReviewQueue />
      <RuleProposals />

      {data && <Settings st={data.settings} grants={data.grants} />}
    </div>
  );
}

/** A capital first letter and a full stop, for a phrase a station wrote as a reason. */
function sentence(s: string): string {
  const t = (s || "").trim();
  if (!t) return "";
  const c = t[0].toUpperCase() + t.slice(1);
  return /[.!?]$/.test(c) ? c : `${c}.`;
}

/** The questions waiting for you (TR-453). Read-only: you answer in the orchestrator's session. */
export function Desk({ project }: { project?: string }) {
  const { data } = usePolling(() => api.desk(project ?? ""), 15000);
  const items = data?.desk ?? [];
  if (!items.length) return null;
  return (
    <section className="fo-attn fo-attn-waiting" role="status" aria-labelledby="desk-h">
      <h2 id="desk-h">{items.length} {items.length === 1 ? "question" : "questions"} waiting for you</h2>
      <ul className="crew-desk">
        {items.map((d) => (
          <li key={d.id}>
            <b>{d.label}</b>{d.blocking && <span className="fo-st fo-st-quiet" style={{ marginLeft: 6 }}>blocks work</span>}
            {d.project_name && <span className="dim"> · {d.project_name}</span>}
            <div>{d.question}</div>
            <div className="dim" style={{ fontSize: 13 }}>
              Recommended: {d.recommendation.replace(/[.!]+$/, "")}. {sentence(d.why)}
              {d.blocks && <> Blocks: {d.blocks.replace(/[.!]+$/, "")}.</>}
              {d.assumptions.length > 0 && <> Confirms {d.assumptions.join(", ")}.</>}
            </div>
          </li>
        ))}
      </ul>
      <p className="help" style={{ margin: 0 }}>Answer in the orchestrator's session: <span className="mono">factory attach orchestrator</span>.</p>
    </section>
  );
}

/** The pull requests waiting for the reviewer across projects, in the order it takes them. */
function ReviewQueue() {
  const { data } = usePolling(() => api.reviewQueue(), 20000);
  const q = data?.queue ?? [];
  if (!q.length) return null;
  return (
    <div className="fo-panel">
      <h2 style={{ fontSize: 14, margin: 0, padding: "12px 16px 4px" }}>Waiting for review <span className="dim">· {q.length}</span></h2>
      <ul className="crew-list">
        {q.map((x) => (
          <li key={`${x.project}-${x.ticket}`} className="crew-row">
            <div className="crew-main">
              <span className="crew-name">{x.ticket}</span>
              <Link to={`/projects/${encodeURIComponent(x.project)}?view=ticket:${encodeURIComponent(x.ticket)}`}>{x.title}</Link>
              <span className="crew-role">{x.project_name}</span>
            </div>
            <div className="crew-side">
              {x.blocks_milestone && <span className="fo-st fo-st-wait" title="its milestone waits for it">blocks milestone</span>}
              {!x.layer1_clean && <span className="fo-st fo-st-quiet" title="Codex findings still open on the builder's commits">Codex open</span>}
              <small>{x.waiting_minutes != null ? `waiting ${x.waiting_minutes} min` : ""}</small>
              {x.pr && <a href={x.pr} target="_blank" rel="noreferrer">PR</a>}
            </div>
            {x.verdict && !x.verdict.current && <p className="crew-what fo-warn">{x.verdict.note}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Rules offered from findings that keep coming back: nothing is kept until you accept one, and
 *  a rejected one is never offered again (TR-443). */
function RuleProposals() {
  const { data, reload } = usePolling(() => api.ruleProposals(), 30000);
  const [busy, setBusy] = useState<string | null>(null);
  const ps = data?.proposals ?? [];
  if (!ps.length) return null;
  const [err, setErr] = useState<string | null>(null);
  const decide = async (id: string, d: "accept" | "reject") => {
    setBusy(id); setErr(null);
    try { await api.decideRule(id, d); reload(); }
    catch (e) { setErr(String((e as Error)?.message ?? e)); }
    finally { setBusy(null); }
  };
  return (
    <div className="fo-panel">
      <h2 style={{ fontSize: 14, margin: 0, padding: "12px 16px 4px" }}>Rules offered from repeated findings</h2>
      {err && <p className="fo-warn" style={{ margin: "0 16px" }}>{err}</p>}
      <ul className="crew-list">
        {ps.map((p) => (
          <li key={p.id} className="crew-row">
            <div className="crew-main" style={{ display: "block" }}>
              <div>{p.text}</div>
              <small className="dim">{p.repo} · {p.kind} · {p.tickets.join(", ")} · goes into {p.scope === "all" ? "the shared AGENTS.md" : "the project's AGENTS.md"}</small>
            </div>
            <div className="crew-side">
              <button type="button" className="primary" disabled={busy === p.id} onClick={() => decide(p.id, "accept")}>Add the rule</button>
              <button type="button" disabled={busy === p.id} onClick={() => decide(p.id, "reject")}>No</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
