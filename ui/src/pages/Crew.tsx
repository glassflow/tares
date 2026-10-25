import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { CopyButton, ErrorState, TimeAgo, usePolling } from "../components/bits";
import { ContextBadge, PlanPanel, StationUsage } from "../components/ClaudeUsage";
import ConfirmDialog from "../components/ConfirmDialog";
import { ProgressRing } from "../components/ProgressRing";
import StandupSheet from "../components/Standup";
import type { CrewProject, CrewSettings, CrewStation, DeskAllowed, DeskItem, ProjectUsage } from "../types";

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

/** The command that opens a station's session in a terminal. The crew's own stations go by their
 *  role (crew-reviewer is reviewer); any other, a helper of one among them too, by its full name.
 *  The same rule as the daemon's attach_command. */
export function attachCmd(name: string): string {
  const role = name.startsWith("crew-") ? name.slice(5) : "";
  return `tares factory crew attach ${CREW_ROLES.includes(role) ? role : name}`;
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
  const d = useDesk(desk);
  const notices: Notice[] = [];
  if (d.items.length) {
    notices.push({
      id: "desk", sig: desk.map((x) => x.label).sort().join(","), tone: "waiting",
      title: <DeskTitle open={desk.filter((x) => !d.allowed[x.id]).length} />,
      body: <DeskList {...d} />,
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

// ── folds: per viewer, in this browser; a project's builders start shut, the Projects pane open ──
const OPEN_KEY = "tares.crew.open";
const PANE_KEY = "tares.crew.projects.folded";
function readOpen(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(OPEN_KEY) || "{}") || {}; } catch { return {}; }
}
/** Keeps only the folds the person opened: a project they closed is back to the default. */
function rememberOpen(id: string, open: boolean) {
  try {
    const next = { ...readOpen() };
    if (open) next[id] = true; else delete next[id];
    localStorage.setItem(OPEN_KEY, JSON.stringify(next));
  } catch { /* private window: for this visit only */ }
}
function readPaneFolded(): boolean {
  try { return localStorage.getItem(PANE_KEY) === "1"; } catch { return false; }
}
function rememberPaneFolded(folded: boolean) {
  try { localStorage.setItem(PANE_KEY, folded ? "1" : "0"); } catch { /* private window: for this visit only */ }
}
/** Whether the fold a clicked summary belongs to is open, before the click toggles it. */
const wasOpen = (e: React.MouseEvent<HTMLElement>) => !!(e.currentTarget.parentElement as HTMLDetailsElement | null)?.open;

/** A station and the helpers under it at any depth, the rows MemberRow draws for it. */
function withHelpers(s: CrewStation, all: CrewStation[]): CrewStation[] {
  return [s, ...all.filter((x) => x.parent === s.name && x.role === "helper").flatMap((k) => withHelpers(k, all))];
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
          <ContextBadge ctx={s.context} />
          <small>{s.last_at ? <TimeAgo ts={s.last_at} /> : "silent"}</small>
          <CrewState s={s} />
        </span>
      </li>
      {kids.map((k) => <MemberRow key={k.name} s={k} all={all} selected={selected} depth={depth + 1} />)}
    </>
  );
}

/** A share as a whole percent; one too small to round up still says it is there. */
const pctOf = (x: number) => (x > 0 && x < 0.005 ? "under 1%" : `${Math.round(x * 100)}%`);

function ProjectBlock({ p, builders, all, selected, week, now }: { p: CrewProject; builders: CrewStation[]; all: CrewStation[]; selected: string; week?: ProjectUsage; now?: ProjectUsage }) {
  // the ring's counts when the row has them (canceled tickets kept out), so the text matches it
  const done = p.progress ? p.progress.done : p.tickets_done;
  const total = p.progress ? p.progress.total : p.tickets_total;
  const left = total - done;
  const shares = [
    now?.share != null ? `${pctOf(now.share)} of this 5-hour window` : "",
    week?.share != null ? `${pctOf(week.share)} of this week` : "",
  ].filter(Boolean);
  return (
    <li className="crew-project">
      <div className="crew-project-head">
        <Link to={`/projects/${encodeURIComponent(p.id)}`} className="crew-project-name">{p.name}</Link>
        <span className="crew-project-progress dim">
          {p.progress && <ProgressRing progress={p.progress} size={14} />}
          {total === 0 ? (p.progress?.canceled ? "no open tickets" : "no tickets yet")
            : left === 0 ? `all ${total} tickets merged`
            : <>{done} of {total} merged{p.milestone && <> · on {p.milestone.name} ({p.milestone.done}/{p.milestone.total})</>}</>}
        </span>
      </div>
      {p.repos.length > 0 && (
        <div className="crew-project-repos dim">
          {p.repos.map((r) => r.name ?? r.path).filter(Boolean).join(" · ")}
          {p.autonomy && <> · {p.autonomy}</>}
        </div>
      )}
      {shares.length > 0 && (
        <div className="crew-project-repos dim" title="its share of what every Claude Code session reporting here used, in the current 5-hour window and over the last 7 days">
          {shares.join(" · ")}
        </div>
      )}
      {builders.length > 0 ? (
        <BuildersFold id={p.id} builders={builders} all={all} selected={selected} />
      ) : (
        left > 0 && <p className="crew-project-none dim">No builder yet: the orchestrator starts one on its next round.</p>
      )}
    </li>
  );
}

/** A project's builders behind a fold that starts shut. Its line counts them, with the helpers
 *  under them, and names any that wait or went quiet, so a shut fold hides nothing that needs you.
 *  It opens by itself when the member picked is in it, so a click or a ?member= link never lands
 *  on a hidden row; opening or closing it by hand is remembered. */
function BuildersFold({ id, builders, all, selected }: { id: string; builders: CrewStation[]; all: CrewStation[]; selected: string }) {
  const members = builders.flatMap((b) => withHelpers(b, all));
  const holds = members.some((m) => m.name === selected);
  const [open, setOpen] = useState(() => holds || !!readOpen()[id]);
  useEffect(() => { if (holds) setOpen(true); }, [holds]);
  const helpers = members.length - builders.length;
  const waiting = members.filter((m) => m.state === "waiting");
  const quiet = members.filter((m) => m.state === "quiet");
  const names = (xs: CrewStation[]) => xs.map((x) => x.name).join(", ");
  return (
    <details className="crew-builders" open={open}
      onToggle={(e) => {
        // only its own: a fold inside one of its rows toggling is not this one
        if (e.target === e.currentTarget) setOpen(e.currentTarget.open);
      }}>
      <summary onClick={(e) => rememberOpen(id, !wasOpen(e))}>
        <span className="crew-builders-count">{builders.length} {builders.length === 1 ? "builder" : "builders"}{helpers > 0 && <> · {helpers} {helpers === 1 ? "helper" : "helpers"}</>}</span>
        {waiting.length > 0 && <span className="crew-builders-flag">
          <span className="fo-st fo-st-wait" title={names(waiting)}>{waiting.length} waiting</span>
          {!open && <>{" "}<span className="crew-builders-who">{names(waiting)}</span></>}
        </span>}
        {quiet.length > 0 && <span className="crew-builders-flag">
          <span className="fo-st fo-st-quiet" title={names(quiet)}>{quiet.length} quiet</span>
          {!open && <>{" "}<span className="crew-builders-who">{names(quiet)}</span></>}
        </span>}
      </summary>
      <ul className="crew-list">
        {builders.map((b) => <MemberRow key={b.name} s={b} all={all} selected={selected} depth={1} />)}
      </ul>
    </details>
  );
}

type CrewFinished = NonNullable<Awaited<ReturnType<typeof api.crewProjects>>["finished"]>[number];

/** The finished projects, shut by default, each with its builders behind a fold like an open
 *  project's. Opens by itself when the member picked is one of those builders or their helpers. */
function FinishedList({ finished, builders, all, selected }: { finished: CrewFinished[]; builders: CrewStation[]; all: CrewStation[]; selected: string }) {
  const ids = new Set(finished.map((p) => p.id));
  const holds = builders.filter((b) => b.project && ids.has(b.project))
    .flatMap((b) => withHelpers(b, all)).some((m) => m.name === selected);
  const [open, setOpen] = useState(holds);
  useEffect(() => { if (holds) setOpen(true); }, [holds]);
  return (
    <details className="crew-finished" open={open}
      onToggle={(e) => { if (e.target === e.currentTarget) setOpen(e.currentTarget.open); }}>
      <summary>{finished.length} finished</summary>
      <ul>
        {finished.map((p) => {
          const mine = builders.filter((b) => b.project === p.id);
          return (
            <li key={p.id}>
              <Link to={`/projects/${encodeURIComponent(p.id)}`}>{p.name}</Link>
              <span className="dim"> · {p.tickets_done} tickets{p.finished && <> · done <TimeAgo ts={p.finished.at} /></>}</span>
              {mine.length > 0 && <BuildersFold id={p.id} builders={mine} all={all} selected={selected} />}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

/** A Quiet station whose session is gone (killed, or never sent its end) stays Quiet for good:
 *  the person ends its row here. Tares keeps its history; a live session's next line undoes it. */
function MarkEnded({ name, onEnded }: { name: string; onEnded: () => void }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const end = async () => {
    setAsking(false); setBusy(true); setErr(null);
    try { await api.endStation(name); onEnded(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    finally { setBusy(false); }
  };
  return (
    <div>
      <button type="button" disabled={busy} onClick={() => setAsking(true)}>{busy ? "Ending…" : "Mark ended"}</button>
      <p className="help" style={{ margin: "6px 0 0" }}>
        Use this when the session is gone. Tares keeps its history, and the row comes back if the session speaks again.
      </p>
      {err && <p className="fo-warn" style={{ margin: "6px 0 0" }}>{err}</p>}
      {asking && (
        <ConfirmDialog
          title={`End ${name}?`}
          message={`Its row shows Ended from now on. If ${name} is still running, its next line turns it back.`}
          confirmLabel={`End ${name}`}
          onCancel={() => setAsking(false)}
          onConfirm={end}
        />
      )}
    </div>
  );
}

// ── one member's details ────────────────────────────────────────────────────

/** How many of a member's last lines show before "Show N more". */
const RECENT_SHOWN = 8;

/** Whether a station is one of the crew's own or a helper under one: they serve every project and
 *  hold no tickets. Goes by its root up the parent chain, so a builder whose project id starts with
 *  crew- stays a builder. */
export function onCrewSide(name: string, all: CrewStation[]): boolean {
  let s = all.find((x) => x.name === name);
  for (let i = 0; s && s.role === "helper" && s.parent && i < 10; i++) {
    const up = all.find((x) => x.name === s!.parent);
    if (!up) return CREW_ROLES.some((r) => s!.parent === `crew-${r}` || s!.parent!.startsWith(`crew-${r}-`));
    s = up;
  }
  return !!s && CREW_ROLES.includes(s.role ?? "");
}

function MemberDetail({ name, crewSide, onChanged }: { name: string; crewSide: boolean; onChanged: () => void }) {
  const { data, error, reload } = usePolling(() => api.station(name), 10000);
  const [allLines, setAllLines] = useState(false);
  // after asking for a /context reading, look again sooner than the usual poll
  const asked = () => { reload(); [3000, 6000, 10000].forEach((ms) => setTimeout(reload, ms)); };
  if (error && !data) return <div className="fo-panel crew-detail"><ErrorState error={error} what={name} onRetry={reload} /></div>;
  if (!data) return <div className="fo-panel crew-detail"><p className="dim" style={{ padding: 16 }}>loading…</p></div>;
  const s = data;
  // newest first, without the bookkeeping rows a transcript carries (titles, names, empty system lines)
  const lines = s.recent.filter((l) => ["user", "assistant", "tool_use", "tool_result", "session_state"].includes(l.type ?? "")
    && l.text.trim() && l.text.trim() !== l.type).reverse();
  const shown = allLines ? lines : lines.slice(0, RECENT_SHOWN);
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
          {!crewSide && s.project_name && <> Works on <Link to={`/projects/${encodeURIComponent(s.project ?? "")}`}>{s.project_name}</Link>.</>}
        </p>
        {s.state === "quiet" && <MarkEnded name={s.name} onEnded={() => { reload(); onChanged(); }} />}
        <StationUsage s={s} onAsked={asked} />
        <div>
          <div className="crew-detail-label">Open it in a terminal</div>
          {s.state === "ended"
            ? <p className="dim" style={{ margin: 0 }}>It has ended, so there is no session to open.</p>
            : <div className="fo-cmd"><code>{s.attach}</code><CopyButton text={s.attach} label="Copy" /></div>}
        </div>
        {!crewSide && <div>
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
          ) : <p className="dim" style={{ margin: 0 }}>None right now.</p>}
        </div>}
        <div>
          <div className="crew-detail-label">Its last lines, newest first</div>
          {lines.length ? (
            <>
              <ol className="crew-recent">
                {shown.map((l, i) => (
                  <li key={i}><span className="dim">{l.type === "session_state" ? "state" : l.type?.replace("_", " ")}</span> {l.text.replace(/^\[tool_result\]\s*/, "")}</li>
                ))}
              </ol>
              {lines.length > shown.length && (
                <button type="button" className="fo-link-btn crew-more" onClick={() => setAllLines(true)}>Show {lines.length - shown.length} more</button>
              )}
            </>
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
          <dt>Kit</dt>
          <dd>{st.kit?.frozen ? <>frozen since {new Date(st.kit.frozen.at).toLocaleString()} by {st.kit.frozen.by}: stations that start or revive use the frozen kit, running ones keep reading the checkout until they restart, and the watch moves none onto a newer kit</> : "following the checkout"}</dd>
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
  const { data: udata, error: uerror, reload: ureload } = usePolling(() => api.claudeCodeUsage(7), 30000);
  const [params] = useSearchParams();
  const [paneOpen, setPaneOpen] = useState(() => !readPaneFolded());
  const [standup, setStandup] = useState(false);
  const closeStandup = useCallback(() => setStandup(false), []);
  const stations = data?.stations ?? [];
  const builders = stations.filter((s) => s.role === "builder");
  // a ?member= link to a builder, or a helper under one, opens the Projects pane for this visit
  const member = params.get("member");
  const inProjects = !!member && builders.flatMap((b) => withHelpers(b, stations)).some((m) => m.name === member);
  useEffect(() => { if (inProjects) setPaneOpen(true); }, [inProjects]);
  if (error && !data) return <ErrorState error={error} what="the crew" onRetry={reload} />;
  const projects = pdata?.projects ?? [];
  const finished = pdata?.finished ?? [];
  const desk = ddata?.desk ?? [];
  const crew = stations.filter((s) => CREW_ROLES.includes(s.role ?? ""));
  const projectIds = new Set(projects.map((p) => p.id));
  const finishedIds = new Set(finished.map((p) => p.id));
  // a finished project keeps its builders under it; Other builders is for no project or an unknown one
  const stray = pdata ? builders.filter((b) => !b.project || (!projectIds.has(b.project) && !finishedIds.has(b.project))) : [];
  const selected = member ?? crew.find((s) => s.role === "orchestrator")?.name ?? "";
  const known = stations.some((s) => s.name === selected);
  const week = new Map((udata?.projects ?? []).filter((x) => x.project).map((x) => [x.project, x]));
  const now = new Map((udata?.window.projects ?? []).filter((x) => x.project).map((x) => [x.project, x]));

  return (
    <div className="crew">
      <div className="pagehead">
        <div>
          <h1>Crew</h1>
          <p className="subtitle">One crew serves every factory project. It runs on your machine; Tares shows what it hears from it.</p>
        </div>
        {(projects.length > 0 || finished.length > 0) && (
          <span className="btnrow"><button type="button" onClick={() => setStandup(true)}>Standup</button></span>
        )}
      </div>
      {standup && <StandupSheet onClose={closeStandup} />}

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
            <details className="fo-panel fo-fold" open={paneOpen}
              onToggle={(e) => {
                // only its own: the project folds and the finished list inside toggle too
                if (e.target === e.currentTarget) setPaneOpen(e.currentTarget.open);
              }}>
              <summary onClick={(e) => rememberPaneFolded(wasOpen(e))}>
                <span className="fo-fold-h">Projects</span>
                <span className="dim">{pdata ? `${projects.length} open${finished.length ? ` · ${finished.length} finished` : ""}` : ""}</span>
              </summary>
              {perror && !pdata ? <div style={{ padding: "10px 16px" }}><ErrorState error={perror} what="the crew's projects" /></div> : projects.length ? (
                <ul className="crew-list">
                  {projects.map((p) => (
                    <ProjectBlock key={p.id} p={p} all={stations} selected={selected}
                      builders={builders.filter((b) => b.project === p.id)}
                      week={week.get(p.id)} now={now.get(p.id)} />
                  ))}
                  {stray.length > 0 && (
                    <li className="crew-project">
                      <div className="crew-project-head"><span className="crew-project-name">Other builders</span></div>
                      <BuildersFold id=":other" builders={stray} all={stations} selected={selected} />
                    </li>
                  )}
                </ul>
              ) : <p className="dim" style={{ padding: "10px 16px", margin: 0 }}>{finished.length ? "None open." : <>None yet. Plan one with <span className="mono">tares factory spec</span> in its repo; the spec ends with the hand-over.</>}</p>}
              {finished.length > 0 && (
                <FinishedList finished={finished} builders={builders} all={stations} selected={selected} />
              )}
            </details>
            <PlanPanel data={udata} error={uerror} reload={ureload} />
          </div>
          <div className="crew-col">
            {known ? <MemberDetail key={selected} name={selected} crewSide={onCrewSide(selected, stations)} onChanged={reload} />
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

/** The questions waiting for you (TR-453), for a project's page. You answer them in the
 *  orchestrator's session; a permission a station was refused you can allow here. `onChange` runs
 *  after one is allowed (the page's decisions gain it). */
export function Desk({ project, onChange }: { project?: string; onChange?: () => void }) {
  const { data } = usePolling(() => api.desk(project ?? ""), 15000);
  const open = data?.desk ?? [];
  const d = useDesk(open, onChange);
  if (!d.items.length) return null;
  return (
    <section className="fo-attn fo-attn-waiting" role="status" aria-labelledby="desk-h">
      <h2 id="desk-h"><DeskTitle open={open.filter((x) => !d.allowed[x.id]).length} /></h2>
      <DeskList {...d} />
    </section>
  );
}

// ── the desk, shared by the Crew page's notice and a project's page ──────────

function DeskTitle({ open }: { open: number }) {
  return open ? <>{open} {open === 1 ? "question" : "questions"} waiting for you</> : <>Nothing else waiting for you</>;
}

/** The open items, plus the permission requests allowed here since the page opened: an allowed one
 *  stays in its place, saying what happens next, after the desk itself no longer lists it. */
function useDesk(open: DeskItem[], onChange?: () => void) {
  const [allowed, setAllowed] = useState<Record<string, DeskAllowed>>({});
  const items = [...open.filter((x) => !allowed[x.id]), ...Object.values(allowed).map((a) => a.desk_item)]
    .sort((a, b) => Number(b.blocking) - Number(a.blocking) || a.created_at.localeCompare(b.created_at));
  const onAllowed = (a: DeskAllowed) => { setAllowed((m) => ({ ...m, [a.desk_item.id]: a })); onChange?.(); };
  return { items, allowed, onAllowed };
}

function DeskList({ items, allowed, onAllowed }: ReturnType<typeof useDesk>) {
  return (
    <>
      <ul className="crew-desk">
        {items.map((d) => (
          <li key={d.id}>
            <b>{d.label}</b>
            {allowed[d.id] ? <span className="fo-st fo-st-done" style={{ marginLeft: 6 }}>allowed</span>
              : d.blocking && <span className="fo-st fo-st-quiet" style={{ marginLeft: 6 }}>blocks work</span>}
            {d.project_name && <span className="dim"> · {d.project_name}</span>}
            <div>{d.question}</div>
            <div className="dim" style={{ fontSize: 13 }}>
              Recommended: {d.recommendation.replace(/[.!]+$/, "")}. {sentence(d.why)}
              {d.blocks && <> Blocks: {d.blocks.replace(/[.!]+$/, "")}.</>}
              {d.assumptions.length > 0 && <> Confirms {d.assumptions.join(", ")}.</>}
            </div>
            {d.command && (allowed[d.id] ? <AllowedNote a={allowed[d.id]} />
              : <PermissionRequest d={d} onAllowed={onAllowed} />)}
          </li>
        ))}
      </ul>
      {items.some((d) => !d.command) && (
        <>
          <p className="help" style={{ margin: 0 }}>Answer questions in the orchestrator's session:</p>
          <div className="fo-cmd">
            <code>{attachCmd("crew-orchestrator")}</code>
            <CopyButton text={attachCmd("crew-orchestrator")} label="Copy" what="the orchestrator's attach command" />
          </div>
        </>
      )}
    </>
  );
}

/** Tares keeps a grant to this many characters (factory.grant_line). */
const GRANT_MAX = 400;

/** The grant as the console offers it: the station, the command and where. A command split over
 *  lines reads as one (its continuation backslashes dropped); one too long for a grant is cut at a
 *  word, so the grant covers the command as it starts. */
function grantSentence(d: DeskItem, scope: "project" | "all"): string {
  const cmd = (d.command ?? "").split("\n").map((l) => l.trim().replace(/\s*\\$/, "")).filter(Boolean).join(" ");
  const say = (c: string) => `${d.station} may run \`${c}\` ${scope === "project" && d.project_name ? `in ${d.project_name}` : "in every project"}`;
  const room = GRANT_MAX - say("").length;
  if (cmd.length <= room) return say(cmd);
  const cut = cmd.slice(0, Math.max(room - 4, 0));
  return say(`${cut.slice(0, Math.max(cut.lastIndexOf(" "), 1))} ...`);
}

/** A permission a station was refused: the command, the grant in words you may edit, where it
 *  holds, and one click. The click is your consent; Tares keeps the grant in these words. */
function PermissionRequest({ d, onAllowed }: { d: DeskItem; onAllowed: (a: DeskAllowed) => void }) {
  const station = d.station ?? "the station";
  // A builder's grant may hold in its project only. A crew station (and its helpers) serves every
  // project and starts with only the grants for every project, as does an item with no project.
  const either = !!d.project && !station.startsWith("crew-");
  const [scope, setScope] = useState<"project" | "all">(either ? "project" : "all");
  const [words, setWords] = useState(() => grantSentence(d, scope));
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // the box shows the whole grant, one line or many: it is what you consent to
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const ta = box.current; if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + ta.offsetHeight - ta.clientHeight + "px";
  }, [words]);
  const length = words.split(/\s+/).join(" ").trim().length;
  const pick = (s: "project" | "all") => { setScope(s); if (!edited) setWords(grantSentence(d, s)); };
  const allow = async () => {
    setBusy(true); setErr(null);
    try { onAllowed(await api.allowDesk(d.label, words, scope)); }
    catch (e) { setErr(String((e as Error)?.message ?? e)); }
    finally { setBusy(false); }
  };
  const wid = `perm-words-${d.id}`;
  return (
    <div className="crew-perm">
      <div className="crew-detail-label">{station} was refused this command</div>
      <pre className="crew-perm-cmd"><code>{d.command}</code></pre>
      <label htmlFor={wid} className="crew-detail-label">The grant, kept in your words</label>
      <textarea id={wid} ref={box} rows={1} value={words} disabled={busy}
                onChange={(e) => { setWords(e.target.value); setEdited(true); }} />
      {length > GRANT_MAX && <p className="fo-warn" style={{ margin: 0 }}>The grant is {length} characters; keep it to {GRANT_MAX}.</p>}
      {either ? (
        <fieldset className="su-who-mini" disabled={busy}>
          <label className={scope === "project" ? "on" : ""}>
            <input type="radio" name={`perm-scope-${d.id}`} checked={scope === "project"} onChange={() => pick("project")} />
            Only {d.project_name ?? "this project"}
          </label>
          <label className={scope === "all" ? "on" : ""}>
            <input type="radio" name={`perm-scope-${d.id}`} checked={scope === "all"} onChange={() => pick("all")} />
            Every project
          </label>
        </fieldset>
      ) : (
        <p className="help" style={{ margin: 0 }}>
          Kept for every project{d.project ? `: ${station} works for all of them` : ""}.
        </p>
      )}
      {err && <p className="fo-warn" style={{ margin: 0 }}>{err}</p>}
      <div>
        <button type="button" className="primary" disabled={busy || !length || length > GRANT_MAX} onClick={allow}>Allow for {station}</button>
      </div>
    </div>
  );
}

function AllowedNote({ a }: { a: DeskAllowed }) {
  if (!a.added) return <p className="crew-perm-done">That grant was already kept, so nothing changed.</p>;
  return (
    <p className="crew-perm-done">
      Allowed{a.scope === "all" ? " for every project" : ""}. The crew watch restarts <span className="mono">{a.station}</span> with
      the permission once it is between turns.
    </p>
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
