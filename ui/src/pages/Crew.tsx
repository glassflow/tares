import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, usePolling } from "../components/bits";
import type { CrewProject, CrewSettings, CrewStation, DeskItem } from "../types";

// The crew of a tares-factory (M3, M8): one always-on crew (orchestrator, reviewer, and a releaser
// when the person asked for one) serves every project handed to it, and builders come and go per
// project and repo. Tares runs none of it: the sessions run on the person's machine
// (`tares factory crew up`), and this page shows what Tares hears from them. The page always lists
// who is on the crew and the projects it holds; a member's details are a click away, and the
// notices at the top can be dismissed until something new happens.

const ROLE_TEXT: Record<string, string> = {
  orchestrator: "The only one you talk to: hands out work, answers the others, brings you what needs you.",
  reviewer: "Reviews every pull request to a fixed bar and posts the verdict.",
  releaser: "Ships finished milestones: tags, deploys, verifies, rolls back.",
  builder: "Takes a project's tickets in one repo to merged pull requests.",
  helper: "Started by a station for one job.",
};

const AUTONOMY_TEXT: Record<string, string> = {
  "L3-review": "you approve each merge",
  "L4-ship": "a builder merges after the reviewer's pass and green CI",
};

const CREW_ROLES = ["orchestrator", "reviewer", "releaser"];

export function CrewState({ s }: { s: CrewStation }) {
  const title = s.state === "quiet" ? `nothing sent for ${s.quiet_minutes} min` : s.state_reason ?? undefined;
  const cls = s.state === "working" ? "fo-st-in_progress" : s.state === "waiting" ? "fo-st-wait"
    : s.state === "quiet" ? "fo-st-quiet" : "";
  return <span className={`fo-st ${cls}`} title={title}>{s.state}</span>;
}

/** A capital first letter and a full stop, for a phrase a station wrote as a reason. */
function sentence(s: string): string {
  const t = (s || "").trim();
  if (!t) return "";
  const c = t[0].toUpperCase() + t.slice(1);
  return /[.!?]$/.test(c) ? c : `${c}.`;
}

function attachCmd(name: string): string {
  return `tares factory crew attach ${name.startsWith("crew-") ? name.slice(5) : name}`;
}

// ── dismissed notices: per viewer, in this browser; a notice comes back when what it says changes ──
const DISMISS_KEY = "tares.crew.dismissed";
function readDismissed(): Record<string, string> {
  try { return JSON.parse(localStorage.getItem(DISMISS_KEY) || "{}") || {}; } catch { return {}; }
}
function useDismissed() {
  const [d, setD] = useState<Record<string, string>>(readDismissed);
  const dismiss = (id: string, sig: string) => {
    const next = { ...d, [id]: sig };
    setD(next);
    try { localStorage.setItem(DISMISS_KEY, JSON.stringify(next)); } catch { /* private window: for this visit only */ }
  };
  return { shown: (id: string, sig: string) => d[id] !== sig, dismiss };
}

interface Notice { id: string; sig: string; tone: "waiting" | "silent"; title: React.ReactNode; body: React.ReactNode }

function Notices({ stations, desk }: { stations: CrewStation[]; desk: DeskItem[] }) {
  const { shown, dismiss } = useDismissed();
  const notices: Notice[] = [];
  if (desk.length) {
    notices.push({
      id: "desk", sig: desk.map((d) => d.label).sort().join(","), tone: "waiting",
      title: <>{desk.length} {desk.length === 1 ? "question" : "questions"} waiting for you</>,
      body: (
        <>
          <ul className="crew-desk">
            {desk.map((d) => (
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
          <p className="help" style={{ margin: 0 }}>Answer in the orchestrator's session: <span className="mono">tares factory crew attach orchestrator</span>.</p>
        </>
      ),
    });
  }
  // The orchestrator rests between turns all the time; what it needs from you comes as the desk above.
  for (const s of stations) {
    if (s.state === "quiet") {
      notices.push({
        id: `quiet:${s.name}`, sig: String(s.last_at ?? ""), tone: "silent",
        title: <>No word from {s.name} for {s.quiet_minutes} min</>,
        body: <p className="help" style={{ margin: 0 }}>It may have been stopped by Claude Code or crashed. <span className="mono">tares factory crew watch</span> brings it back; Tares only shows it.</p>,
      });
    }
  }
  const visible = notices.filter((n) => shown(n.id, n.sig));
  if (!visible.length) return null;
  return (
    <div className="crew-notices">
      {visible.map((n) => (
        <section key={n.id} className={`fo-attn fo-attn-${n.tone}`} role="status">
          <div className="crew-notice-head">
            <h2>{n.title}</h2>
            <button type="button" className="crew-dismiss" aria-label="Dismiss until something new happens"
              title="Dismiss until something new happens" onClick={() => dismiss(n.id, n.sig)}>Dismiss</button>
          </div>
          {n.body}
        </section>
      ))}
    </div>
  );
}

// ── the member list ─────────────────────────────────────────────────────────

function MemberRow({ s, all, selected, depth = 0 }: { s: CrewStation; all: CrewStation[]; selected: string; depth?: number }) {
  const [, setParams] = useSearchParams();
  const kids = all.filter((x) => x.parent === s.name && x.role === "helper");
  return (
    <>
      <li className={`crew-member${selected === s.name ? " is-selected" : ""}`} style={{ paddingLeft: 16 + depth * 20 }}>
        <button type="button" className="crew-pick" onClick={() => setParams({ member: s.name }, { replace: true })}
          aria-current={selected === s.name ? "true" : undefined}>
          <span className="crew-name">{s.name}</span>
          <span className="crew-role">{s.role ?? "station"}</span>
        </button>
        <span className="crew-side">
          <small>{s.last_at ? <TimeAgo ts={s.last_at} /> : "silent"}</small>
          <CrewState s={s} />
        </span>
      </li>
      {kids.map((k) => <MemberRow key={k.name} s={k} all={all} selected={selected} depth={depth + 1} />)}
    </>
  );
}

function ProjectBlock({ p, builders, all, selected }: { p: CrewProject; builders: CrewStation[]; all: CrewStation[]; selected: string }) {
  const left = p.tickets_total - p.tickets_done;
  return (
    <li className="crew-project">
      <div className="crew-project-head">
        <Link to={`/projects/${encodeURIComponent(p.id)}`} className="crew-project-name">{p.name}</Link>
        <span className="dim">
          {p.tickets_total === 0 ? "no tickets yet"
            : left === 0 ? `all ${p.tickets_total} tickets merged`
            : <>{p.tickets_done} of {p.tickets_total} merged{p.milestone && <> · on {p.milestone.name} ({p.milestone.done}/{p.milestone.total})</>}</>}
        </span>
      </div>
      {p.repos.length > 0 && (
        <div className="crew-project-repos dim">
          {p.repos.map((r) => r.name ?? r.path).filter(Boolean).join(" · ")}
          {p.autonomy && <> · {p.autonomy}</>}
        </div>
      )}
      {builders.length > 0 ? (
        <ul className="crew-list">
          {builders.map((b) => <MemberRow key={b.name} s={b} all={all} selected={selected} depth={1} />)}
        </ul>
      ) : (
        left > 0 && <p className="crew-project-none dim">No builder yet: the orchestrator starts one on its next round.</p>
      )}
    </li>
  );
}

// ── one member's details ────────────────────────────────────────────────────

function MemberDetail({ name }: { name: string }) {
  const { data, error, reload } = usePolling(() => api.station(name), 10000);
  if (error && !data) return <div className="fo-panel crew-detail"><ErrorState error={error} what={name} onRetry={reload} /></div>;
  if (!data) return <div className="fo-panel crew-detail"><p className="dim" style={{ padding: 16 }}>loading…</p></div>;
  const s = data;
  // newest first, without the bookkeeping rows a transcript carries (titles, names, empty system lines)
  const lines = s.recent.filter((l) => ["user", "assistant", "tool_use", "tool_result", "session_state"].includes(l.type ?? "")
    && l.text.trim() && l.text.trim() !== l.type).reverse();
  return (
    <div className="fo-panel crew-detail">
      <header>
        <h3><span className="mono">{s.name}</span> <span className="crew-role">{s.role ?? "station"}</span></h3>
        <CrewState s={s} />
      </header>
      <div className="crew-detail-body">
        {s.role && ROLE_TEXT[s.role] && <p className="dim" style={{ margin: 0 }}>{ROLE_TEXT[s.role]}</p>}
        <p style={{ margin: 0 }}>
          {s.state === "working" ? "Working" : s.state === "waiting" ? "Waiting" : s.state === "quiet" ? `No word for ${s.quiet_minutes} min` : "Ended"}
          {s.state_reason && <>: {s.state_reason.replace(/[.!]+$/, "")}</>}
          {s.last_at && <>; last spoke <TimeAgo ts={s.last_at} /></>}.
          {s.project_name && <> Works on <Link to={`/projects/${encodeURIComponent(s.project ?? "")}`}>{s.project_name}</Link>.</>}
        </p>
        <div>
          <div className="crew-detail-label">Open it in a terminal</div>
          <div className="fo-cmd"><code>{s.attach}</code></div>
        </div>
        <div>
          <div className="crew-detail-label">Tickets it holds</div>
          {s.tickets.length ? (
            <ul className="crew-detail-tickets">
              {s.tickets.map((t) => (
                <li key={`${t.project}-${t.ticket}`}>
                  <Link to={`/projects/${encodeURIComponent(t.project)}?view=ticket:${encodeURIComponent(t.ticket)}`}>{t.ticket} {t.title}</Link>
                  <span className="fo-st">{t.stage ?? "todo"}</span>
                  {t.repo && <span className="dim">{t.repo}</span>}
                  {t.pr && <a href={t.pr} target="_blank" rel="noreferrer">PR</a>}
                </li>
              ))}
            </ul>
          ) : <p className="dim" style={{ margin: 0 }}>{s.role === "builder" ? "None right now." : "Crew stations don't hold tickets; they serve every project."}</p>}
        </div>
        <div>
          <div className="crew-detail-label">Its last lines, newest first</div>
          {lines.length ? (
            <ol className="crew-recent">
              {lines.map((l, i) => (
                <li key={i}><span className="dim">{l.type === "session_state" ? "state" : l.type?.replace("_", " ")}</span> {l.text.replace(/^\[tool_result\]\s*/, "")}</li>
              ))}
            </ol>
          ) : <p className="dim" style={{ margin: 0 }}>Nothing recorded yet.</p>}
        </div>
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
      </div>
    </div>
  );
}

function Settings({ st, grants }: { st: CrewSettings; grants: string }) {
  const lines = grants.split("\n").filter((l) => l.startsWith("- ")).map((l) => l.slice(2));
  return (
    <details className="fo-panel crew-fold">
      <summary>How far the crew may go{st.autonomy && <span className="dim"> · {st.autonomy}{st.releaser ? ", with a releaser" : ", no releaser"}</span>}</summary>
      <div className="crew-settings">
        <dl>
          <dt>Autonomy</dt>
          <dd>{st.autonomy ? <><b>{st.autonomy}</b>: {AUTONOMY_TEXT[st.autonomy]}</> : <span className="dim">not set: the first <span className="mono">tares factory crew up</span> asks</span>}</dd>
          <dt>Releaser</dt>
          <dd>{st.releaser === null ? <span className="dim">not set</span> : st.releaser ? <>yes, release profile {st.release_profile ?? "not set"}</> : "no: tickets end at merged on main"}</dd>
          <dt>Codex challenger on builders</dt><dd>{st.challenger === null ? <span className="dim">not set</span> : st.challenger ? "on" : "off"}</dd>
          <dt>Builders at once</dt><dd>{st.builders_max ?? <span className="dim">not set</span>}</dd>
        </dl>
        <h2>Grants for every project</h2>
        {lines.length ? <ul className="crew-grants">{lines.map((g, i) => <li key={i}>{g}</li>)}</ul>
          : <p className="dim">None yet. A grant is kept only in your own words, with the day you said it.</p>}
      </div>
    </details>
  );
}

export default function Crew() {
  const { data, error, reload } = usePolling(() => api.crew(), 10000);
  const { data: pdata, error: perror } = usePolling(() => api.crewProjects(), 15000);
  const { data: ddata } = usePolling(() => api.desk(), 15000);
  const [params] = useSearchParams();
  if (error && !data) return <ErrorState error={error} what="the crew" onRetry={reload} />;
  const stations = data?.stations ?? [];
  const projects = pdata?.projects ?? [];
  const finished = pdata?.finished ?? [];
  const desk = ddata?.desk ?? [];
  const crew = stations.filter((s) => CREW_ROLES.includes(s.role ?? ""));
  const projectIds = new Set(projects.map((p) => p.id));
  const builders = stations.filter((s) => s.role === "builder");
  const stray = pdata ? builders.filter((b) => !b.project || !projectIds.has(b.project)) : [];
  const selected = params.get("member") ?? crew.find((s) => s.role === "orchestrator")?.name ?? "";
  const known = stations.some((s) => s.name === selected);

  return (
    <div className="crew">
      <div className="pagehead">
        <div>
          <h1>Crew</h1>
          <p className="subtitle">One crew serves every factory project. It runs on your machine; Tares shows what it hears from it.</p>
        </div>
      </div>

      <Notices stations={stations} desk={desk} />

      {data === undefined ? <div className="dim">loading…</div> : stations.length === 0 && projects.length === 0 && finished.length === 0 ? (
        <section className="fo-next fo-next-empty">
          <h2>No crew has reported yet</h2>
          <p className="help">Bring the crew up once, in a terminal, from a folder of its own:</p>
          <div className="fo-cmd"><code>tares factory crew up</code></div>
          <p className="help">It asks how far the crew may go alone, saves your answers here, and starts the orchestrator and the reviewer. They show here as they start. Then plan a project with <span className="mono">tares factory spec</span> in its repo.</p>
        </section>
      ) : (
        <div className="crew-split">
          <div className="crew-col">
            <div className="fo-panel">
              <header><h3>The crew</h3>{data.settings.releaser === false && <span className="dim">no releaser: tickets end at merged</span>}</header>
              {crew.length ? (
                <ul className="crew-list">
                  {crew.map((s) => <MemberRow key={s.name} s={s} all={stations} selected={selected} />)}
                </ul>
              ) : <p className="dim" style={{ padding: "10px 16px", margin: 0 }}>No crew station has reported yet: <span className="mono">tares factory crew up</span>.</p>}
            </div>
            <div className="fo-panel">
              <header><h3>Projects</h3><span className="dim">{pdata ? `${projects.length} open${finished.length ? ` · ${finished.length} finished` : ""}` : ""}</span></header>
              {perror && !pdata ? <div style={{ padding: "10px 16px" }}><ErrorState error={perror} what="the crew's projects" /></div> : projects.length ? (
                <ul className="crew-list">
                  {projects.map((p) => (
                    <ProjectBlock key={p.id} p={p} all={stations} selected={selected}
                      builders={builders.filter((b) => b.project === p.id)} />
                  ))}
                  {stray.length > 0 && (
                    <li className="crew-project">
                      <div className="crew-project-head"><span className="crew-project-name">Other builders</span></div>
                      <ul className="crew-list">{stray.map((b) => <MemberRow key={b.name} s={b} all={stations} selected={selected} depth={1} />)}</ul>
                    </li>
                  )}
                </ul>
              ) : <p className="dim" style={{ padding: "10px 16px", margin: 0 }}>{finished.length ? "None open." : <>None yet. Plan one with <span className="mono">tares factory spec</span> in its repo; the spec ends with the hand-over.</>}</p>}
              {finished.length > 0 && (
                <details className="crew-finished">
                  <summary>{finished.length} finished</summary>
                  <ul>
                    {finished.map((p) => (
                      <li key={p.id}>
                        <Link to={`/projects/${encodeURIComponent(p.id)}`}>{p.name}</Link>
                        <span className="dim"> · {p.tickets_done} tickets{p.finished && <> · done <TimeAgo ts={p.finished.at} /></>}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          </div>
          <div className="crew-col">
            {known ? <MemberDetail key={selected} name={selected} />
              : <div className="fo-panel crew-detail"><p className="dim" style={{ padding: 16, margin: 0 }}>Pick a member to see what it is doing.</p></div>}
          </div>
        </div>
      )}

      <ReviewQueue />
      <RuleProposals />

      {data && <Settings st={data.settings} grants={data.grants} />}
    </div>
  );
}

/** The questions waiting for you (TR-453), for a project's page. Read-only: you answer in the
 *  orchestrator's session. */
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
      <p className="help" style={{ margin: 0 }}>Answer in the orchestrator's session: <span className="mono">tares factory crew attach orchestrator</span>.</p>
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
      <header><h3>Waiting for review</h3><span className="dim">{q.length}</span></header>
      <ul className="crew-list">
        {q.map((x) => (
          <li key={`${x.project}-${x.ticket}`} className="crew-row">
            <div className="crew-main">
              <span className="crew-name">{x.ticket}</span>
              <Link to={`/projects/${encodeURIComponent(x.project)}?view=ticket:${encodeURIComponent(x.ticket)}`}>{x.title}</Link>
              <span className="crew-role">{x.project_name}{x.repo && <> · {x.repo}</>}</span>
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
  const [err, setErr] = useState<string | null>(null);
  const ps = data?.proposals ?? [];
  if (!ps.length) return null;
  const decide = async (id: string, d: "accept" | "reject") => {
    setBusy(id); setErr(null);
    try { await api.decideRule(id, d); reload(); }
    catch (e) { setErr(String((e as Error)?.message ?? e)); }
    finally { setBusy(null); }
  };
  return (
    <div className="fo-panel">
      <header><h3>Rules offered from repeated findings</h3></header>
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
