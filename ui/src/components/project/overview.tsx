import { Link } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../../api";
import { ErrorState, Picker, TimeAgo, usePolling } from "../bits";
import type {
  OutlineWatch, ProjectHealth, ProjectOutline, ProjectResult, ProjectResults, ProjectSetup,
} from "../../types";
import { VLink, viewFrom, type Ctx, type View } from "./common";

// The goal-first project page (the owner's rule: show the goal and what the agents concluded, in
// one click). Three views, none with the left nav:
//   Overview   the goal, one sentence of how it works, whether it is working, today's totals and
//              what the agents found, newest first
//   Result     one finding: the next step first, the note, how Tares got there
//   How        the setup as four plain steps, each with a Change link into the full setup
// The machinery (sources, triggers, firings, raw events, runs) stays in the full setup.

const GOAL_MAX = 200;

/** "Today 14:03", "Yesterday 09:12", or "Sep 27, 14:03". */
export function whenLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(new Date()) - day(d)) / 86400000);
  if (diff === 0) return `Today ${time}`;
  if (diff === 1) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

/** Spend in dollars, for a person: "$0.07", "under $0.01", "$0". */
function money(v: number | null | undefined): string | null {
  if (v == null || !Number.isFinite(v)) return null;
  if (v === 0) return "$0";
  if (v < 0.01) return "under $0.01";
  return `$${v.toFixed(2)}`;
}

function took(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms)) return null;
  if (ms < 60000) return `${Math.max(1, Math.round(ms / 1000))} s`;
  return `${Math.round(ms / 60000)} min`;
}

const chainLabel = (chain: string[]) => chain.join(", then ");

function headlineOf(r: ProjectResult): string {
  if (r.headline) return r.headline;
  const agent = r.chain[r.chain.length - 1] ?? "The agent";
  return r.verdict ? `${agent} concluded ${r.verdict}` : `${agent} finished without a headline`;
}

function ResultBadge({ r }: { r: ProjectResult }) {
  // a practice result is not a real finding: its kind would claim something needs doing
  if (r.practice) return <span className="gf-badge practice">Practice</span>;
  if (r.handled) return <span className="gf-badge handled">Handled</span>;
  return r.kind === "action"
    ? <span className="gf-badge action">Needs action</span>
    : <span className="gf-badge quiet">No action</span>;
}

// ── Overview ────────────────────────────────────────────────────────────────

/** The goal, editable in place. A project without one asks for it. */
function Goal({ ctx }: { ctx: Ctx }) {
  const goal = ctx.s.goal ?? null;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  // what was just saved, shown until the summary catches up
  const [saved, setSaved] = useState<string | null | undefined>(undefined);
  useEffect(() => { setSaved(undefined); }, [goal]);
  const shown = saved !== undefined ? saved : goal;

  const open = () => { setDraft(shown ?? ""); setErr(undefined); setEditing(true); };
  const save = async () => {
    const next = draft.replace(/\s+/g, " ").trim().slice(0, GOAL_MAX);
    setBusy(true); setErr(undefined);
    try {
      await api.updateProject(ctx.id, { goal: next || null });
      setSaved(next || null); setEditing(false); ctx.refresh();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  if (editing) {
    return (
      <form className="gf-goal-form" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <label className="field" style={{ margin: 0 }}>
          <span className="lbl">What should this project achieve?</span>
          <input type="text" value={draft} maxLength={GOAL_MAX} autoFocus disabled={busy}
                 onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Escape") setEditing(false); }}
                 placeholder="e.g. Catch checkout outages early and find the root cause." />
          <span className="help">One line, {GOAL_MAX - draft.length} characters left. Leave it empty to remove the goal.</span>
        </label>
        {err && <div className="alert error" style={{ margin: "8px 0 0" }}>{err}</div>}
        <div className="btnrow" style={{ marginTop: 8 }}>
          <button type="submit" className="primary" disabled={busy}>{busy ? "Saving…" : "Save goal"}</button>
          <button type="button" onClick={() => setEditing(false)} disabled={busy}>Cancel</button>
        </div>
      </form>
    );
  }
  if (!shown) {
    return (
      <button type="button" className="gf-goal-add" onClick={open}>
        Add a goal: what should this project achieve?
      </button>
    );
  }
  return (
    <p className="gf-goal">
      {shown}{" "}
      <button type="button" className="linklike gf-goal-change" onClick={open} aria-label="Change the goal">Change</button>
    </p>
  );
}

function HealthBanner({ ctx, health, onResume }: { ctx: Ctx; health: ProjectHealth; onResume: () => void }) {
  const [showAll, setShowAll] = useState(false);
  const first = health.issues[0];
  const rest = health.issues.slice(1);
  const hasSessions = !!ctx.s.sessions;
  const fixButton = (issue: typeof first, cls = "") => {
    // credentials and model providers are the cell's, on the console's own Settings page
    if (issue?.fix && (issue.view === "settings:github" || issue.view === "settings:providers"))
      return <Link className={`btn gf-fix ${cls}`} to="/settings">{issue.fix}</Link>;
    const v = issue && viewFrom(issue.view, hasSessions);
    return issue?.fix && v
      ? <button type="button" className={`gf-fix ${cls}`} onClick={() => ctx.go(v)}>{issue.fix}</button>
      : null;
  };
  const tone = health.state === "working" ? "working"
    : health.state === "paused" ? "paused"
    : health.state === "setting_up" ? "setting-up"
    : first?.severity === "warning" ? "warning" : "error";
  const title = health.state === "working" ? "Working."
    : health.state === "paused" ? "Paused."
    : health.state === "setting_up" ? "Setting up."
    : "Needs attention.";
  // the message the banner leads with: the most important issue when there is one
  const message = health.state === "attention" && first ? first.message : health.message;
  return (
    <div className={`gf-health ${tone}`} role="status">
      <div className="gf-health-row">
        <span className="gf-health-dot" aria-hidden="true" />
        <span className="gf-health-title">{title}</span>
        <span className="gf-health-msg">{message}</span>
        {health.state === "paused"
          ? <button type="button" className="gf-fix" onClick={onResume}>Resume</button>
          : fixButton(first)}
      </div>
      {health.state !== "attention" && first && health.state !== "paused" && (
        <p className="gf-health-more">{first.message}</p>
      )}
      {rest.length > 0 && (
        <>
          <button type="button" className="linklike gf-health-toggle" aria-expanded={showAll}
                  onClick={() => setShowAll(!showAll)}>
            {showAll ? "Hide the other issues" : `${rest.length} more ${rest.length === 1 ? "issue" : "issues"}`}
          </button>
          {showAll && (
            <ul className="gf-health-list">
              {rest.map((i, n) => (
                <li key={n}>
                  <span>{i.message}</span>
                  {fixButton(i, "small")}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

/** A project set up goal first that stopped before the end: the way back to the step it stored.
 *  Template projects have no stored setup; the call fails or says done, and nothing shows. */
function FinishSetup({ id }: { id: string }) {
  const [setup, setSetup] = useState<ProjectSetup>();
  useEffect(() => {
    let live = true;
    api.projectSetup(id).then((r) => { if (live) setSetup(r); }).catch(() => {});
    return () => { live = false; };
  }, [id]);
  if (!setup || !setup.step || setup.step === "done") return null;
  // a practice run already done is the end of the flow, whether or not "Open the project" was pressed
  if (setup.step === "try" && setup.practice_run) return null;
  const own = setup.plan?.who === "own";
  const message = setup.step === "try"
    ? "Run it once now to see a result before a real one arrives."
    : own ? "Connect your agent so it is woken when this project needs it."
    : "Connect your data so the agents have something to look at.";
  return (
    <div className="gf-health gf-finish" role="status">
      <div className="gf-health-row">
        <span className="gf-health-dot" aria-hidden="true" />
        <span className="gf-health-title">Finish setting up.</span>
        <span className="gf-health-msg">{message}</span>
        <Link className="btn gf-fix" to={`/projects/${encodeURIComponent(id)}/setup`}>
          {setup.step === "try" ? "Try it" : "Connect it"}
        </Link>
      </div>
    </div>
  );
}

function Today({ today }: { today: ProjectResults["today"] }) {
  return (
    <p className="gf-today">
      <span><strong>{today.looked_at}</strong> looked at today</span>
      <span><strong>{today.found}</strong> {today.found === 1 ? "needs" : "need"} action</span>
      <span><strong>{money(today.spent_usd) ?? "$0"}</strong> spent</span>
    </p>
  );
}

export function ResultCard({ r, onOpen }: { r: ProjectResult; onOpen: () => void }) {
  return (
    <li>
      <button type="button" className={`gf-card${r.handled ? " handled" : ""}`} onClick={onOpen}>
        <ResultBadge r={r} />
        <span className="gf-card-body">
          <span className="gf-card-top">
            <span className="gf-card-headline">{headlineOf(r)}</span>
            {r.entity && <span className="gf-entity">{r.entity}</span>}
          </span>
          {r.summary && <span className="gf-card-summary">{r.summary}</span>}
          {r.next_step && <span className="gf-card-next"><strong>Next:</strong> {r.next_step}</span>}
        </span>
        <span className="gf-card-meta">
          <span>{whenLabel(r.at)}</span>
          {r.chain.length > 0 && <span>{chainLabel(r.chain)}</span>}
          {r.external && <span>from an agent outside Tares</span>}
        </span>
      </button>
    </li>
  );
}

// What the results list shows: everything, only findings, or one agent's conclusions. Remembered
// per project in this browser, so a busy watcher's "nothing to report" stays out of the way.
function useShow(project: string): [string, (v: string) => void] {
  const key = `tares_results_show:${project}`;
  const [show, setShowState] = useState<string>(() => {
    try { return localStorage.getItem(key) ?? ""; } catch { return ""; }
  });
  const setShow = (v: string) => {
    setShowState(v);
    try { if (v) localStorage.setItem(key, v); else localStorage.removeItem(key); } catch { /* private window */ }
  };
  return [show, setShow];
}

function Results({ ctx, head, headError, reload, outline, show, setShow }: {
  ctx: Ctx; head: ProjectResults | undefined; headError: string | undefined; reload: () => void;
  outline: ProjectOutline | undefined; show: string; setShow: (v: string) => void;
}) {
  const [older, setOlder] = useState<ProjectResult[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null | undefined>(undefined);
  // a new filter starts over from the newest page
  useEffect(() => { setOlder([]); setOlderCursor(undefined); }, [show]);
  // the project's agents, and any other agent whose conclusions are on the page (one outside Tares)
  const agents = [...new Set([...(outline?.agents ?? []).map((a) => a.name),
                              ...(head?.results ?? []).map((r) => r.chain[r.chain.length - 1]).filter(Boolean)])];
  const showOptions = ["", "findings", ...agents.map((a) => `agent:${a}`)];
  if (show && !showOptions.includes(show)) showOptions.push(show);
  const showLabels: Record<string, string> = { "": "Everything", findings: "Only findings" };
  for (const a of agents) showLabels[`agent:${a}`] = `Only ${a}`;
  if (show.startsWith("agent:") && !agents.includes(show.slice(6))) showLabels[show] = `Only ${show.slice(6)}`;
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string>();
  const seen = new Set<string>();
  const results = head === undefined ? undefined
    : [...head.results, ...older].filter((r) => (seen.has(r.id) ? false : (seen.add(r.id), true)));
  const cursor = olderCursor === undefined ? head?.next_before : olderCursor;
  const more = async () => {
    if (!cursor) return;
    setLoadingMore(true); setMoreError(undefined);
    try {
      const page = await api.projectResults(ctx.id, { limit: 20, before: cursor, show });
      setOlder((o) => [...o, ...page.results]);
      setOlderCursor(page.next_before);
    } catch (e) { setMoreError(String((e as Error).message ?? e)); }
    setLoadingMore(false);
  };
  const wake = outline?.wakes.find((w) => !w.paused)?.sentence;

  return (
    <section aria-labelledby="gf-results-h" className="gf-results">
      <div className="gf-results-head">
        <h2 id="gf-results-h">What the agents found</h2>
        {(agents.length > 1 || show) && (
          <Picker value={show} onChange={setShow} options={showOptions} labels={showLabels}
                  ariaLabel="show which results" />
        )}
      </div>
      {headError && <ErrorState error={headError} what="the results" onRetry={reload} />}
      {results && results.length > 0 && (
        <ol className="gf-cards">
          {results.map((r) => <ResultCard key={r.id} r={r} onOpen={() => ctx.go({ kind: "result", name: r.id })} />)}
        </ol>
      )}
      {results && results.length === 0 && !headError && (
        <div className="gf-empty">
          <p>{show
            ? <>Nothing here with this filter yet. <button type="button" className="linklike" onClick={() => setShow("")}>Show everything</button></>
            : "Nothing yet. Each time this project wakes, its agents look, and what they conclude shows up here, newest first."}</p>
          {!wake && outline && <p className="help">Nothing wakes it yet. <VLink v={{ kind: "triggers" }} extra={{ add: "1" }}>Add a trigger</VLink> to say when the agents should look.</p>}
        </div>
      )}
      {moreError && <ErrorState error={moreError} what="older results" onRetry={more} />}
      {results && results.length > 0 && cursor && (
        <div className="btnrow">
          <button type="button" onClick={more} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load older"}</button>
        </div>
      )}
    </section>
  );
}

/** The project's landing view. `actions` are Pause/Resume and the template's actions. */
export function Overview({ ctx, actions, onResume }: { ctx: Ctx; actions: React.ReactNode; onResume: () => void }) {
  const { data: outline } = usePolling(() => api.projectOutline(ctx.id), 30000);
  const { data: health, error: healthError, reload: reloadHealth } = usePolling(() => api.projectHealth(ctx.id), 15000);
  const [show, setShow] = useShow(ctx.id);
  const { data: head, error: headError, reload } = usePolling(() => api.projectResults(ctx.id, { limit: 20, show }), 15000);
  const shownOnce = useRef(false);
  useEffect(() => {   // the poll's first load already used it; reload only when it changes
    if (shownOnce.current) reload(); else shownOnce.current = true;
  }, [show]);   // eslint-disable-line react-hooks/exhaustive-deps
  // a pause or resume changes what the health says; ask again right away, not at the next poll
  const status = ctx.s.status;
  const [lastStatus, setLastStatus] = useState(status);
  useEffect(() => {
    if (status !== lastStatus) { setLastStatus(status); reloadHealth(); }
  }, [status, lastStatus]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="gf">
      <div className="gf-head">
        <div className="gf-head-text">
          <h1 className="gf-name">{ctx.s.name}</h1>
          <Goal ctx={ctx} />
          {outline?.sentence && <p className="gf-sentence">{outline.sentence}</p>}
        </div>
        <div className="gf-head-actions">
          <VLink v={{ kind: "how" }} className="btn">Setup</VLink>
          {actions}
        </div>
      </div>

      <FinishSetup id={ctx.id} />
      {health && <HealthBanner ctx={ctx} health={health} onResume={onResume} />}
      {!health && healthError && (
        <p className="help" style={{ margin: 0 }}>Could not check whether this project is working: {healthError}</p>
      )}

      {head && <Today today={head.today} />}

      <Results ctx={ctx} head={head} headError={headError} reload={reload} outline={outline}
               show={show} setShow={setShow} />
    </div>
  );
}

// ── Result ──────────────────────────────────────────────────────────────────

export function ResultView({ ctx, runId }: { ctx: Ctx; runId: string }) {
  const { data: r, error, reload } = usePolling(() => api.projectResult(ctx.id, runId), 30000);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const back = <VLink v={{ kind: "overview" }} className="btn">Back to the project</VLink>;

  if (!r) {
    return (
      <div className="gf gf-narrow">
        {error ? <ErrorState error={error} what="this result" onRetry={reload} /> : <p className="help">Loading…</p>}
        <div className="btnrow">{back}</div>
      </div>
    );
  }

  const toggle = async () => {
    setBusy(true); setErr(undefined);
    try { await api.setResultHandled(ctx.id, runId, !r.handled); reload(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const cost = money(r.cost_usd);
  const time = took(r.duration_ms);
  const rawLink: View = { kind: "activity" };

  return (
    <article className="gf gf-narrow" aria-labelledby="gf-result-h">
      <div className="gf-result-head">
        <div className="gf-result-meta">
          <ResultBadge r={r} />
          {r.entity && <span className="gf-entity">{r.entity}</span>}
          <span className="help">{whenLabel(r.at)}</span>
          {r.external && <span className="help">from an agent outside Tares</span>}
        </div>
        <h1 id="gf-result-h" className="gf-result-title">{headlineOf(r)}</h1>
      </div>

      {r.next_step && (
        <div className="gf-next"><strong>Next step:</strong> {r.next_step}</div>
      )}

      {r.note
        ? <div className="panel md gf-note"><ReactMarkdown remarkPlugins={[remarkGfm]}>{r.note}</ReactMarkdown></div>
        : r.summary && <p className="gf-summary">{r.summary}</p>}

      <section aria-labelledby="gf-steps-h">
        <h2 id="gf-steps-h" className="gf-h3">How Tares got here</h2>
        <ol className="gf-steps">
          {(r.steps ?? []).map((st, i) => (
            <li key={i}>
              <time className="gf-step-time" dateTime={st.at} title={new Date(st.at).toLocaleString()}>
                {new Date(st.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </time>{" "}
              {st.text}
            </li>
          ))}
          <li className="gf-steps-end">
            {[time && `Took ${time}`, cost && `cost ${cost}`].filter(Boolean).join(", ")}
            {(time || cost) && ". "}
            {r.thread && <VLink v={rawLink} extra={{ thread: r.thread }}>See the raw events and runs</VLink>}
          </li>
        </ol>
      </section>

      {err && <div className="alert error" style={{ margin: 0 }}>{err}</div>}
      {r.handled && (
        <p className="help" style={{ margin: 0 }}>
          Marked as handled {whenLabel(r.handled.at).replace(/^Today /, "today at ").replace(/^Yesterday /, "yesterday at ")
            .replace(/^([A-Z][a-z]{2} \d)/, "on $1")}{handledBy(r.handled.by)}.
        </p>
      )}
      <div className="btnrow">
        {(r.kind === "action" || r.handled) && (
          <button type="button" className={r.handled ? undefined : "primary"} onClick={toggle} disabled={busy}>
            {busy ? "Saving…" : r.handled ? "Mark as not handled" : "Mark as handled"}
          </button>
        )}
        {back}
      </div>
    </article>
  );
}

// ── How it works ────────────────────────────────────────────────────────────

function watchState(w: OutlineWatch): { cls: string; text: React.ReactNode } {
  switch (w.state) {
    case "receiving":
      return { cls: "ok", text: w.last_event_at ? <>Receiving, last <TimeAgo ts={w.last_event_at} /></> : "Receiving" };
    case "silent":
      return { cls: "err", text: w.detail ?? "Nothing has arrived lately" };
    case "error":
      return { cls: "err", text: w.detail ?? "Can't reach it" };
    case "paused":
      return { cls: "warn", text: "Paused" };
    case "waiting":
      return { cls: "dim", text: w.detail ?? "Waiting for its first event" };
  }
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="gf-step">
      <h2 className="gf-step-n">{n}. {title}</h2>
      {children}
    </li>
  );
}

/** " by claude-code" for a named key; nothing for the console's own credential (the auth token,
 *  or the per-person key Tares Cloud names user:<id>), which names no one a reader knows. */
function handledBy(by: string | null | undefined): string {
  if (!by || by === "console" || by.startsWith("user:") || by.startsWith("auth token")) return "";
  return ` by ${by}`;
}

function Change({ v, what }: { v: View; what: string }) {
  return <VLink v={v} className="gf-change" ariaLabel={`Change ${what}`}>Change</VLink>;
}

/** How this project works: the outline as four steps. `panels` are the template's own tables;
 *  `manage` is Edit and Delete. */
export function HowView({ ctx, panels, manage }: { ctx: Ctx; panels: React.ReactNode; manage: React.ReactNode }) {
  const { data: o, error, reload } = usePolling(() => api.projectOutline(ctx.id), 30000);
  const canAddAgent = (o?.wakes.length ?? 0) > 0;

  return (
    <div className="gf">
      <div className="gf-head">
        <div className="gf-head-text">
          <h1 className="gf-name">Setup</h1>
          <p className="gf-sentence">What this project watches, when it wakes, and who does the work. Change any step here.</p>
        </div>
        <div className="gf-head-actions">
          <VLink v={{ kind: "activity" }} className="btn"
                 title="Every source, trigger, agent run, firing and raw event, for when something does not work as expected">
            Advanced setup</VLink>
        </div>
      </div>

      {error && <ErrorState error={error} what="the setup" onRetry={reload} />}
      {!o && !error && <p className="help">Loading…</p>}
      {o && (
        <ol className="gf-how">
          <Step n={1} title="Watches">
            {o.watches.length === 0 && (
              <p className="gf-step-empty">No source yet, so nothing arrives. <VLink v={{ kind: "sources" }} extra={{ add: "1" }}>Add a source</VLink></p>
            )}
            {o.watches.map((w) => {
              const st = watchState(w);
              return (
                <div className="gf-step-item" key={w.source}>
                  <span className="gf-step-what">{w.title || w.source}</span>
                  {w.description && <span className="gf-step-desc">{w.description}</span>}
                  <span className={`gf-step-state ${st.cls}`}>{st.text}</span>
                  <Change v={{ kind: "source", name: w.source }} what={w.source} />
                </div>
              );
            })}
          </Step>
          <Step n={2} title="Wakes when">
            {o.wakes.length === 0 && (
              <p className="gf-step-empty">Nothing wakes the agents yet. <VLink v={{ kind: "triggers" }} extra={{ add: "1" }}>Add a trigger</VLink></p>
            )}
            {o.wakes.map((w) => (
              <div className="gf-step-item" key={w.trigger}>
                <span className="gf-step-what">{w.sentence}</span>
                {w.cooldown_sentence && <span className="gf-step-desc">{w.cooldown_sentence}</span>}
                {w.paused && <span className="gf-step-state warn">Paused</span>}
                <Change v={{ kind: "trigger", name: w.trigger }} what={w.trigger} />
              </div>
            ))}
          </Step>
          <Step n={3} title="Agents">
            {o.agents.length === 0 && !(o.outside ?? []).length && (
              <p className="gf-step-empty">No agent does the work yet.{" "}
                {canAddAgent
                  ? <VLink v={{ kind: "agents" }} extra={{ add: "1" }}>Add an agent</VLink>
                  : "Add a trigger first; an agent runs when one fires."}
              </p>
            )}
            {o.agents.map((a) => (
              <div className="gf-step-item" key={a.name}>
                <span className="gf-step-what">{a.sentence}</span>
                {!a.enabled && a.runs_on === "trigger" && <span className="gf-step-state warn">Off</span>}
                <Change v={{ kind: "agent", name: a.name }} what={a.name} />
              </div>
            ))}
            {(o.outside ?? []).map((x) => (
              <div className="gf-step-item" key={`outside:${x.name}`}>
                <span className="gf-step-what">{x.sentence}</span>
                {!x.joined && <span className="gf-step-state warn">Not connected</span>}
                <Change v={{ kind: "settings", name: "keys" }} what={x.name} />
              </div>
            ))}
          </Step>
          <Step n={4} title="Know-how">
            {o.skills.length === 0 && (
              <p className="gf-step-empty">No know-how yet. Skills are notes the agents read, like which error codes mean an outage. <VLink v={{ kind: "skills" }} extra={{ add: "1" }}>Add a skill</VLink></p>
            )}
            {o.skills.map((sk) => (
              <div className="gf-step-item" key={sk.name}>
                <span className="gf-step-what">{sk.name}</span>
                {sk.description && <span className="gf-step-desc">{sk.description}</span>}
                <span className="gf-step-state dim">
                  {sk.loaded_by.length ? `Read by ${sk.loaded_by.join(", ")}` : "No agent reads it yet"}
                </span>
                <Change v={{ kind: "skill", name: sk.name }} what={sk.name} />
              </div>
            ))}
          </Step>
        </ol>
      )}

      {panels}

      <div className="gf-setup-row">
        <span>Something not working as expected? Advanced setup shows every source, trigger, agent run, firing and raw event.</span>
        <VLink v={{ kind: "activity" }} className="gf-setup-link">Open advanced setup</VLink>
      </div>

      <div className="btnrow">
        <VLink v={{ kind: "overview" }} className="btn">Back to the project</VLink>
        {manage}
      </div>
    </div>
  );
}
