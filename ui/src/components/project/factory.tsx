import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import { CrewState, Desk } from "../../pages/Crew";
import { TimeAgo, usePolling } from "../bits";
import { VLink, type Ctx } from "./common";
import { Goal } from "./overview";
import type { DocSummary, Milestone, ProjectRepo, ProjectSession, RepoPR, Ticket } from "../../types";

// The page of a software factory project (kind "software_factory"): a project a spec session
// made in Claude Code and sessions build from docs and tickets. Where the build stands (spec,
// build, review, done) and how far, the next ticket with the sentence that starts a build
// session, the docs pinned on the left, the tickets folded on the right, the sessions below.
// It has no agents, so the agent health, counters and findings of the usual Overview are not here.

type Phase = "spec" | "build" | "review" | "done";

function phaseOf(hasStart: boolean, tickets: Ticket[]): Phase {
  const open = tickets.filter((t) => t.status !== "canceled");
  if (!hasStart || !open.length) return "spec";
  const left = open.filter((t) => t.status !== "done");
  if (!left.length) return "done";
  // the last ticket of a factory plan verifies and opens the PR: when it is all that is left,
  // the build is in review
  if (left.length === 1 && left[0].id === open[open.length - 1].id && open.length > 1) return "review";
  return "build";
}

/** The first paragraph of a working doc, without its heading: what the ticket is about. */
function gist(body: string | null | undefined): string {
  if (!body) return "";
  const para = body.split(/\n\s*\n/)
    .map((p) => p.replace(/^#+\s.*$/gm, "").trim())   // a heading line above the paragraph
    .find((p) => p) ?? "";
  const text = para.replace(/[`*_]/g, "").replace(/\s+/g, " ").trim();
  return text.length > 280 ? `${text.slice(0, 277).replace(/\s+\S*$/, "")}…` : text;
}

function CopyButton({ text, label, primary }: { text: string; label: string; primary?: boolean }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1800); }
    catch { /* the sentence stays selectable next to the button */ }
  };
  return <button type="button" className={primary ? "primary" : undefined} onClick={copy}>{done ? "Copied" : label}</button>;
}

const STATUS_TEXT: Record<Ticket["status"], string> = {
  todo: "to do", in_progress: "in progress", done: "done", canceled: "canceled",
};

export default function FactoryOverview({ ctx }: { ctx: Ctx }) {
  const { data: docs } = usePolling(() => api.docs(ctx.id), 15000);
  const { data: tk } = usePolling(() => api.tickets(ctx.id), 15000);
  const { data: ss } = usePolling(() => api.projectSessions(ctx.id), 15000);
  const { data: msd } = usePolling(() => api.milestones(ctx.id), 15000);
  const { data: crew } = usePolling(() => api.projectCrew(ctx.id), 15000);
  const { data: asm } = usePolling(() => api.assumptions(ctx.id), 30000);
  const { data: decisions } = usePolling(() => api.projectDecisions(ctx.id), 30000);
  const { data: rp } = usePolling(() => api.projectRepos(ctx.id), 30000);
  // the repos the project touches (TR-463); picking one narrows the ticket list to its tickets
  const repos = rp?.repos ?? [];
  const [repoKey, setRepoKey] = useState<string | null>(null);
  const repoOn = repos.find((r) => (r.name ?? r.path) === repoKey) ?? null;
  const openPrs = repos.reduce((n, r) => n + r.open_prs.length, 0);
  // handed to the crew: the crew builds it, there is no build prompt to copy
  const byCrew = !!crew?.handover;
  const allTickets = tk?.tickets ?? [];
  const tickets = allTickets;
  const shownTickets = repoOn ? allTickets.filter((t) => repoOn.tickets.includes(t.label)) : allTickets;
  const milestones = msd?.milestones ?? [];
  // the ticket a build takes next: the one in progress, else the first ready one (TR-414)
  const next = tickets.find((t) => t.status === "in_progress")
    ?? tickets.find((t) => t.status === "todo" && t.ready);
  const blocked = tickets.filter((t) => t.status === "todo" && !t.ready && t.blocked_by.length);

  const own = (docs ?? []).filter((d) => !d.global && d.kind !== "working" && d.kind !== "note");
  const shared = (docs ?? []).filter((d) => d.global);
  const questions = (docs ?? []).filter((d) => d.kind === "note" && !d.global);
  const start = own.find((d) => d.kind === "start");
  const phase = phaseOf(!!start, tickets);
  const live = tickets.filter((t) => t.status !== "canceled");
  const done = live.filter((t) => t.status === "done").length;
  const doing = live.filter((t) => t.status === "in_progress").length;
  const sessions = ss?.sessions ?? [];
  const firstLinked = [...sessions].sort((a, b) => a.linked_at.localeCompare(b.linked_at))[0]?.session;
  // the session the build depends on: the most recently active one (sessions come newest first)
  const active = sessions.find((s) => s.state !== "replaced");
  const building = tickets.some((t) => t.status === "in_progress");
  const quietMin = active ? quiet(active, ss?.now) : 0;
  // With nothing in progress, a session waiting at its prompt is not a stopped build: it is
  // idle, and the one that finished the last ticket has finished. The stored state stays raw.
  const ms = (t?: string | null) => (t ? Date.parse(t) : NaN);
  const lastDone = phase === "done"
    ? Math.max(...tickets.filter((t) => t.status === "done").map((t) => ms(t.updated_at))) : NaN;
  const finisher = Number.isNaN(lastDone) ? undefined : sessions
    .filter((s) => s.state !== "replaced" && ms(s.started_at) <= lastDone && ms(s.last_at) >= lastDone)
    .sort((a, b) => ms(b.last_at) - ms(a.last_at))[0]?.session;
  const shownState = (s: ProjectSession, _now?: string): Shown =>
    building ? null : s.session === finisher ? "finished" : s.state === "waiting" ? "idle" : null;
  const attention: "waiting" | "silent" | null = !building || !active ? null
    : active.state === "waiting" ? "waiting"
    : active.state !== "ended" && quietMin >= SILENT_MIN ? "silent" : null;
  const prompt = `Work on Tares project "${ctx.s.name}": read its starting prompt and begin.`;
  const where = tk?.linear ? <>in Linear project <a href={tk.linear.url} target="_blank" rel="noreferrer">{tk.linear.name}</a></> : "in Tares";

  const steps: { key: Phase; title: string; text: React.ReactNode }[] = [
    { key: "spec", title: "Spec", text: start ? <>written <TimeAgo ts={start.updated_at} /></> : "no starting prompt yet" },
    { key: "build", title: "Build", text: phase === "spec" ? "after the spec" : doing ? `${doing} in progress` : done ? `${done} of ${live.length} done` : "not started" },
    { key: "review", title: "Review", text: byCrew ? "every pull request, by the crew's reviewer" : "the last ticket: verify and open the PR" },
    { key: "done", title: "Done", text: phase === "done" ? "every ticket done" : "when every ticket is done" },
  ];
  const order: Phase[] = ["spec", "build", "review", "done"];
  const at = order.indexOf(phase);

  return (
    <div className="gf fo">
      <div className="gf-head">
        <div className="gf-head-text">
          <h1 className="gf-name fo-name">{ctx.s.name}<span className="fo-tag">Software factory</span></h1>
          <Goal ctx={ctx} />
        </div>
        <div className="gf-head-actions">
          {byCrew
            ? <Link to="/crew" className="btn primary" title="The crew builds this project">The crew</Link>
            : <CopyButton text={prompt} label="Copy build prompt" primary />}
          <VLink v={{ kind: "activity" }} className="btn"
                 title="Every doc, ticket, session, source and setting of this project">Setup</VLink>
        </div>
      </div>

      <ol className="fo-steps" aria-label="Where the build stands">
        {steps.map((s, i) => (
          <li key={s.key} className={i < at || phase === "done" ? "is-done" : i === at ? "is-now" : ""}
              aria-current={i === at ? "step" : undefined}>
            <b><i aria-hidden="true" />{s.title}</b>
            <span>{s.text}</span>
          </li>
        ))}
      </ol>

      {live.length > 0 && (
        <div className="fo-progress">
          <div className="fo-progress-row">
            <span><b>{done} of {live.length} tickets done</b> · tickets live {where}</span>
            <span className="fo-counts">
              <span><b>{live.length - done - doing}</b> to do</span>
              <span><b>{doing}</b> in progress</span>
              <span><b>{done}</b> done</span>
            </span>
          </div>
          <div className="fo-bar" aria-hidden="true">
            {live.map((t) => <i key={t.id} className={t.status === "done" ? "done" : t.status === "in_progress" ? "run" : ""} />)}
          </div>
        </div>
      )}

      {attention && active && (
        <Attention kind={attention} session={active} quietMin={quietMin}
                   label={active.station ?? (active.session === firstLinked ? "spec session" : "build session")}
                   ticket={next?.status === "in_progress" ? next.title : undefined}
                   pickUp={`Pick up Tares project "${ctx.s.name}" where the last session stopped.`} />
      )}

      {next ? (
        <NextUp key={next.id} ctx={ctx} next={next} of={tickets.indexOf(next) + 1} total={tickets.length}
                prompt={prompt} waiting={attention !== null && next.status === "in_progress"}
                byCrew={byCrew} />
      ) : tk && (
        <section className="fo-next fo-next-empty">
          {!tickets.length
            ? <><h2>No tickets yet</h2><p className="help">Run <span className="mono">/tares:spec</span> in Claude Code to spec this project, or add tickets under Setup.</p></>
            : blocked.length
              ? <><h2>No ticket is ready</h2><p className="help">Every open ticket waits for another one that is not done: {blocked.map((t) => `${t.label} waits for ${t.blocked_by.join(", ")}`).join("; ")}.</p></>
              : <h2>Every ticket is done.</h2>}
        </section>
      )}

      <div className="fo-cols">
        <Fold id="docs" title="Docs" pinnedByDefault
              meta={docs === undefined ? "loading…" : [
                `${own.length} ${own.length === 1 ? "doc" : "docs"}`,
                questions.length ? `${questions.length} open ${questions.length === 1 ? "question" : "questions"}` : "",
                shared.length ? `${shared.length} shared` : "",
              ].filter(Boolean).join(" · ")}>
          {docs === undefined ? <p className="fo-empty">loading…</p> : (
            <>
              {own.length ? <DocList docs={own} /> : <p className="fo-empty">No docs yet.</p>}
              {questions.length > 0 && <>
                <div className="fo-sub">Open questions from builders</div>
                <DocList docs={questions} />
              </>}
              {shared.length > 0 && <>
                <div className="fo-sub">Shared by every project</div>
                <DocList docs={shared} />
              </>}
            </>
          )}
          <div className="fo-foot"><VLink v={{ kind: "docs" }} className="fo-link">All docs</VLink></div>
        </Fold>

        <Fold id="tickets" title="Tickets"
              meta={[repoOn ? `${shownTickets.length} in ${repoOn.name ?? repoOn.path}` : `${tickets.length} · ${done} done`,
                     milestones.length ? `${milestones.length} ${milestones.length === 1 ? "milestone" : "milestones"}` : "",
                     tk?.linear ? "in Linear" : "in Tares"].filter(Boolean).join(" · ")}>
          {repoOn && (
            <div className="fo-repo-on">
              <span>Only the tickets of <b>{repoOn.name ?? repoOn.path}</b>.</span>
              {repoOn.open_prs.map((p) => <a key={p.url} href={p.url} target="_blank" rel="noreferrer">{p.ticket} PR{p.number ? ` #${p.number}` : ""}</a>)}
              <button type="button" className="fo-link-btn" onClick={() => setRepoKey(null)}>Show all</button>
            </div>
          )}
          {milestones.length ? (
            <>
              {milestones.map((m) => {
                const mine = shownTickets.filter((t) => t.milestone === m.id);
                return repoOn && !mine.length ? null
                  : <MilestoneGroup key={m.id} m={m} tickets={mine} nextId={next?.id} />;
              })}
              {shownTickets.some((t) => !t.milestone) && (
                <>
                  <div className="fo-sub">No milestone</div>
                  <TicketList tickets={shownTickets.filter((t) => !t.milestone)} nextId={next?.id} />
                </>
              )}
            </>
          ) : <TicketList tickets={shownTickets} nextId={next?.id} />}
          <div className="fo-foot"><VLink v={{ kind: "tickets" }} className="fo-link">All tickets</VLink></div>
        </Fold>
      </div>

      {repos.length > 0 && (
        <Fold id="github" title="GitHub"
              meta={`${repos.length} ${repos.length === 1 ? "repo" : "repos"}${openPrs ? ` · ${openPrs} open ${openPrs === 1 ? "PR" : "PRs"}` : ""}`}>
          <GitHubRepos repos={repos} on={repoKey} pick={(k) => setRepoKey(k === repoKey ? null : k)} />
        </Fold>
      )}

      <Desk project={ctx.id} />

      {(decisions?.decisions.length ?? 0) > 0 && (
        <Fold id="decisions" title="Decisions"
              meta={`${decisions!.decisions.length} · your words, newest first`}>
          <ul className="fo-docs">
            {decisions!.decisions.map((d) => (
              <li key={d.id} style={{ display: "block" }}>
                {d.label && <b>{d.label} </b>}{d.question}
                <div>"{d.words}"{d.choice && <span className="dim"> · {d.choice}</span>}</div>
                <small style={{ display: "block" }}>
                  <TimeAgo ts={d.at} />
                  {d.matched ? " · matches what you typed" : <span className="fo-warn"> · not found in the session</span>}
                  {d.standing && (d.granted ? " · kept as a grant" : " · standing")}
                </small>
              </li>
            ))}
          </ul>
        </Fold>
      )}

      {(asm?.assumptions.length ?? 0) > 0 && (
        <Fold id="assumptions" title="Assumptions"
              meta={`${asm!.assumptions.filter((a) => a.state === "open").length} open · ${asm!.assumptions.length} in all`}>
          <ul className="fo-docs">
            {[...asm!.assumptions].sort((a, b) => (a.state === "open" ? 0 : 1) - (b.state === "open" ? 0 : 1)).map((a) => (
              <li key={a.id} style={{ display: "block" }}>
                <b>{a.label}</b> <span className={`fo-st${a.state === "open" ? " fo-st-wait" : a.state === "kept" ? " fo-st-done" : ""}`}>{a.state}</span>{" "}
                {a.question} → <b>{a.choice}</b>
                <small style={{ display: "block" }}>
                  {a.tickets.length > 0 && <>{a.tickets.map((t, i) => <span key={t}>{i ? ", " : ""}<VLink v={{ kind: "ticket", name: t }}>{t}</VLink></span>)} · </>}
                  {a.made_by ?? "a session"}{a.words && <> · you said: "{a.words}"</>}
                </small>
              </li>
            ))}
          </ul>
        </Fold>
      )}

      {crew && (crew.handover || crew.stations.length > 0) && (
        <Fold id="crew" title="Crew"
              meta={[crew.handover ? "handed to the crew" : "",
                     `${crew.stations.length} ${crew.stations.length === 1 ? "station" : "stations"}`].filter(Boolean).join(" · ")}>
          {crew.handover && (
            <p className="fo-empty">Handed to the crew <TimeAgo ts={crew.handover.at} />{crew.handover.repos.length > 0 && <> with {crew.handover.repos.length === 1 ? "its repo" : `its ${crew.handover.repos.length} repos`}</>}{crew.handover.autonomy && <>, at {crew.handover.autonomy}</>}.</p>
          )}
          {crew.stations.length ? (
            <ul className="fo-docs">
              {crew.stations.map((s) => (
                <li key={s.name}>
                  <span className="fo-sess">
                    <VLink v={{ kind: "claude", name: s.session }}>{s.name}</VLink>
                    <small> {s.role}</small>
                    <CrewState s={s} />
                  </span>
                  <small>{s.last_at ? <TimeAgo ts={s.last_at} /> : null}</small>
                </li>
              ))}
            </ul>
          ) : <p className="fo-empty">No station has worked on it yet. The orchestrator starts a builder once it reads the plan.</p>}
          <div className="fo-foot"><Link to="/crew" className="fo-link">The whole crew</Link></div>
        </Fold>
      )}

      <Fold id="sessions" title="Sessions"
            meta={<>{sessions.length} · {sessions[0]?.last_at ? <>last active <TimeAgo ts={sessions[0].last_at} /></> : "none yet"}</>}>
        {sessions.length ? (
          <ul className="fo-docs">
            {sessions.map((x) => (
              <li key={x.session}>
                <span className="fo-sess">
                  <VLink v={{ kind: "claude", name: x.session }}>
                    {x.station ?? (x.session === firstLinked ? "Spec session" : "Build session")}{x.repo && <> · {x.repo}</>}
                  </VLink>
                  <StateBadge s={x} now={ss?.now} shown={shownState(x, ss?.now)} />
                </span>
                <small>{x.started_at ? <TimeAgo ts={x.started_at} /> : null} · {x.lines} lines</small>
              </li>
            ))}
          </ul>
        ) : <p className="fo-empty">No Claude Code session has worked in this project yet.</p>}
      </Fold>
    </div>
  );
}

// a session that has sent nothing for this long while a ticket is in progress may be gone
const SILENT_MIN = 10;

const CI_TEXT: Record<string, [string, string]> = {
  success: ["CI passed", "fo-st-done"], failure: ["CI failed", "fo-st-quiet"],
  pending: ["CI running", "fo-st-in_progress"], none: ["no CI", ""],
};
const PR_STAGE: Record<string, string> = {
  doing: "being built", review: "waiting for review", changes: "changes asked", blocked: "blocked",
};

/** One pull request: what it is, which ticket, and where it stands on GitHub and with the crew. */
function PRRow({ p }: { p: RepoPR }) {
  const ci = p.ci ? CI_TEXT[p.ci] : null;
  const rv = p.review;
  return (
    <li className="fo-gh-pr">
      <div className="fo-gh-pr-head">
        <a href={p.url} target="_blank" rel="noreferrer"><b>#{p.number ?? "?"}</b> {p.title}</a>
        <VLink v={{ kind: "ticket", name: p.ticket }} className="fo-gh-tk">{p.ticket}</VLink>
      </div>
      <div className="fo-gh-pr-state">
        {p.stage && PR_STAGE[p.stage] && <span className={`fo-st ${p.stage === "review" ? "fo-st-wait" : p.stage === "changes" || p.stage === "blocked" ? "fo-st-quiet" : "fo-st-in_progress"}`}>{PR_STAGE[p.stage]}</span>}
        {ci && <span className={`fo-st ${ci[1]}`}>{ci[0]}</span>}
        {rv && (rv.current
          ? <span className={`fo-st ${rv.verdict === "pass" ? "fo-st-done" : "fo-st-quiet"}`}>
              {rv.verdict === "pass" ? "review passed" : rv.verdict === "changes" ? "changes asked" : "review blocked"}{rv.round > 1 ? `, round ${rv.round}` : ""}
            </span>
          : <span className="fo-st">review is for an older commit</span>)}
        {!rv && p.stage === "review" && <span className="fo-st">not reviewed yet</span>}
      </div>
      <small className="dim">
        {p.branch && <span className="mono">{p.branch}</span>}
        {p.head && <> at <span className="mono">{p.head}</span></>}
        {p.holder && <> · held by <span className="mono">{p.holder}</span></>}
        {p.checked_at && <> · read from GitHub <TimeAgo ts={p.checked_at} /></>}
      </small>
    </li>
  );
}

/** The GitHub repos the project works on (TR-463): each repo, linked to GitHub, with its open pull
 *  requests listed under it and where each stands; the merged ones folded below. */
function GitHubRepos({ repos, on, pick }: { repos: ProjectRepo[]; on: string | null; pick: (k: string) => void }) {
  return (
    <div className="fo-gh">
      {repos.map((r) => {
        const k = (r.name ?? r.path) as string;
        return (
          <section key={k} className={`fo-gh-repo${on === k ? " is-on" : ""}`}>
            <div className="fo-gh-name">
              {r.name ? <a href={`https://github.com/${r.name}`} target="_blank" rel="noreferrer" className="mono">{r.name} ↗</a>
                : <span className="mono">{r.path}</span>}
              <button type="button" className="fo-link-btn" aria-pressed={on === k} onClick={() => pick(k)}>
                {on === k ? "show all tickets" : `show its ${r.tickets.length} ${r.tickets.length === 1 ? "ticket" : "tickets"}`}
              </button>
            </div>
            {r.path && r.name && <small className="dim">checked out at <span className="mono">{r.path}</span></small>}
            {!r.path && <small className="fo-warn">not checked out on the crew's machine yet</small>}
            {r.github_error && <small className="fo-warn">Tares could not read GitHub for this repo ({r.github_error}); the state below may be out of date.</small>}
            <div className="fo-sub">Open pull requests · {r.open_prs.length}</div>
            {r.open_prs.length
              ? <ul className="fo-gh-prs">{r.open_prs.map((p) => <PRRow key={p.url} p={p} />)}</ul>
              : <p className="dim fo-gh-none">None open.</p>}
            {(r.merged_prs ?? []).length > 0 && (
              <details className="fo-gh-merged">
                <summary>{(r.merged_prs ?? []).length} merged</summary>
                <ul>
                  {(r.merged_prs ?? []).map((p) => (
                    <li key={p.url}>
                      <a href={p.url} target="_blank" rel="noreferrer">#{p.number ?? "?"} {p.title}</a>
                      {" "}<span className="dim">{p.ticket}{p.merged_at && <> · merged <TimeAgo ts={p.merged_at} /></>}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </section>
        );
      })}
    </div>
  );
}

function quiet(s: ProjectSession, now?: string): number {
  return s.last_at && now ? Math.floor((Date.parse(now) - Date.parse(s.last_at)) / 60000) : 0;
}

type Shown = "finished" | "idle" | null;

/** A section that folds, with a pin: a pinned one is open on every visit and does not fold.
 *  Pins are this browser's, per section, across projects. */
function Fold({ id, title, meta, pinnedByDefault, children }: {
  id: string; title: string; meta: React.ReactNode; pinnedByDefault?: boolean; children: React.ReactNode;
}) {
  const key = `tares.factory.pin.${id}`;
  const [pinned, setPinned] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key);
      return v === null ? !!pinnedByDefault : v === "1";
    } catch { return !!pinnedByDefault; }
  });
  const [open, setOpen] = useState(pinned);
  const togglePin = (e: React.MouseEvent) => {
    e.preventDefault(); e.stopPropagation();
    const next = !pinned;
    setPinned(next);
    if (next) setOpen(true);
    try { localStorage.setItem(key, next ? "1" : "0"); } catch { /* the pin lasts this visit */ }
  };
  return (
    <details className={`fo-panel fo-fold${pinned ? " is-pinned" : ""}`} open={pinned || open}
             onToggle={(e) => { if (!pinned) setOpen((e.target as HTMLDetailsElement).open); }}>
      <summary onClick={(e) => { if (pinned) e.preventDefault(); }}>
        <span className="fo-fold-h">{title}</span>
        <span className="fo-fold-right">
          <span className="fo-meta">{meta}</span>
          <button type="button" className={`fo-pin${pinned ? " on" : ""}`} onClick={togglePin}
                  aria-pressed={pinned} aria-label={pinned ? `Unpin ${title}` : `Pin ${title} open`}
                  title={pinned ? "Pinned open: click to let it fold" : "Pin open: stays open on every visit"}>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M9.5 1.5l5 5-2 .5-2.5 2.5.5 3-1.5 1.5-3-3L2.5 14.5 1.5 13.5 5 10 2 7l1.5-1.5 3 .5L9 3.5z"
                    fill={pinned ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            </svg>
          </button>
        </span>
      </summary>
      {children}
    </details>
  );
}

function StateBadge({ s, now, shown }: { s: ProjectSession; now?: string; shown?: Shown }) {
  if (shown === "finished") return <span className="fo-st fo-st-done" title="it finished the last ticket">finished</span>;
  if (shown === "idle") return <span className="fo-st" title={`waiting at its prompt; nothing is in progress${s.state_reason ? ` (${s.state_reason})` : ""}`}>idle</span>;
  if (s.state === "waiting") return <span className="fo-st fo-st-wait" title={s.state_reason ?? undefined}>waiting</span>;
  if (s.state === "ended") return <span className="fo-st">ended</span>;
  if (s.state === "replaced") return <span className="fo-st" title={s.state_reason ?? undefined}>replaced</span>;
  if (s.state === "working") {
    return quiet(s, now) >= SILENT_MIN
      ? <span className="fo-st fo-st-quiet" title="nothing sent for a while">quiet</span>
      : <span className="fo-st fo-st-in_progress">working</span>;
  }
  return null;
}

/** A build that has stopped: the session waits for the person, or has gone silent. */
function Attention({ kind, session, quietMin, label, ticket, pickUp }: {
  kind: "waiting" | "silent"; session: ProjectSession; quietMin: number; label: string; ticket?: string;
  pickUp: string;
}) {
  const said = (session.last_said ?? "").trim();
  return (
    <section className={`fo-attn fo-attn-${kind}`} role="status" aria-labelledby="fo-attn-h">
      <h2 id="fo-attn-h">
        {kind === "waiting"
          ? <>The {label} is waiting for you{session.state_at && <> since <TimeAgo ts={session.state_at} /></>}</>
          : <>No word from the {label} for {quietMin} min</>}
      </h2>
      {kind === "waiting" ? (
        <>
          {said && <p className="fo-said">{said.length > 600 ? `${said.slice(0, 597)}…` : said}</p>}
          <p className="help" style={{ margin: 0 }}>
            {ticket && <>Ticket "{ticket}" is in progress. </>}
            Answer it in that Claude Code session, or say "continue" there.
            {session.state_reason && session.state_reason !== "the turn ended; the session waits for input" && <> Claude Code said: {session.state_reason}.</>}
          </p>
        </>
      ) : (
        <p className="help" style={{ margin: 0 }}>
          {ticket ? <>Ticket "{ticket}" is in progress, but </> : null}the session has sent nothing
          since <TimeAgo ts={session.last_at} />. It may have crashed, its terminal was closed, or the
          laptop went to sleep. Check its terminal.
        </p>
      )}
      <div className="fo-cmd">
        <code>{pickUp}</code>
        <CopyButton text={pickUp} label="Copy pick-up prompt" />
      </div>
      <span className="help">
        Or carry on in a fresh Claude Code session with the Tares plugin: it reads where the build
        stopped and takes it over. <VLink v={{ kind: "claude", name: session.session }} className="fo-link">Open the session</VLink>
      </span>
    </section>
  );
}

/** The ticket a build session takes next, with what it is about and the sentence to start one. */
function NextUp({ ctx, next, of, total, prompt, waiting, byCrew }: {
  ctx: Ctx; next: Ticket; of: number; total: number; prompt: string; waiting?: boolean;
  byCrew?: boolean;
}) {
  const { data: full } = usePolling(() => api.ticket(ctx.id, next.id), 30000);
  const text = gist(full?.working_doc_body);
  return (
    <section className="fo-next" aria-labelledby="fo-next-h">
      <span className="fo-label">
        {next.status === "in_progress" ? (waiting ? "In progress · stopped" : "In progress") : "Next up"} · {next.label} · ticket {of} of {total}
        {next.milestone_name && <> · {next.milestone_name}</>}
      </span>
      <h2 id="fo-next-h">{next.title}</h2>
      {text && <p className="fo-gist">{text}</p>}
      <VLink v={{ kind: "ticket", name: next.id }} className="fo-link">
        {next.working_doc ? "Open the working doc" : "Write its working doc"}</VLink>
      {byCrew ? (
        <span className="help">
          The crew builds this project{next.holder ? <>: <span className="mono">{next.holder}</span> holds this ticket</> : ""}.
          To steer it, talk to the orchestrator: <span className="mono">tares factory crew attach orchestrator</span>.
        </span>
      ) : (
        <>
          <div className="fo-cmd">
            <code>{prompt}</code>
            <CopyButton text={prompt} label="Copy" />
          </div>
          <span className="help">Paste it into a new Claude Code session with the Tares plugin, in the project's repository.</span>
        </>
      )}
    </section>
  );
}

/** A milestone's tickets under its name, progress and goal, its acceptance checks folded. */
function MilestoneGroup({ m, tickets, nextId }: { m: Milestone; tickets: Ticket[]; nextId?: string }) {
  return (
    <div className="fo-ms">
      <div className="fo-ms-head">
        <b>{m.name}</b>
        <span className={`fo-st${m.finished ? " fo-st-done" : ""}`}>
          {m.finished ? "done" : `${m.done} of ${m.tickets}`}
        </span>
      </div>
      {m.goal && <p className="fo-ms-goal">{m.goal}</p>}
      {m.checks.length > 0 && (
        <details className="fo-checks">
          <summary>{m.checks.length} acceptance {m.checks.length === 1 ? "check" : "checks"}</summary>
          <ul>
            {m.checks.map((c, i) => (
              <li key={i}><code>{c.check}</code>{c.expect && <> <span aria-hidden="true">→</span> {c.expect}</>}</li>
            ))}
          </ul>
        </details>
      )}
      {tickets.length ? <TicketList tickets={tickets} nextId={nextId} />
        : <p className="fo-empty">No tickets in this milestone yet.</p>}
    </div>
  );
}

const STAGE_TEXT: Record<NonNullable<Ticket["stage"]>, string> = {
  todo: "to do", doing: "doing", review: "in review", changes: "changes asked", merged: "merged",
  shipped: "shipped", blocked: "blocked",
};
const STAGE_CLASS: Record<NonNullable<Ticket["stage"]>, string> = {
  todo: "", doing: "fo-st-in_progress", review: "fo-st-in_progress", changes: "fo-st-wait",
  merged: "fo-st-done", shipped: "fo-st-done", blocked: "fo-st-quiet",
};

/** The pull request's state in a few words: CI and the reviewer's verdict, or merged. */
export function prBrief(pr: Ticket["pr"]): string | null {
  if (!pr || !pr.url) return null;
  if (pr.merged) return `PR #${pr.number ?? ""} merged`;
  const bits = [`PR #${pr.number ?? ""}`];
  if (pr.ci && pr.ci !== "none") bits.push(`CI ${pr.ci === "success" ? "green" : pr.ci === "failure" ? "red" : "running"}`);
  if (pr.verdict) bits.push(pr.verdict === "pass" ? "review passed" : pr.verdict === "changes" ? "changes asked" : "review pending");
  return bits.join(" · ");
}

function TicketList({ tickets, nextId }: { tickets: Ticket[]; nextId?: string }) {
  const many = new Set(tickets.map((t) => t.repo ?? t.pr?.repo).filter(Boolean)).size > 1;
  return (
    <ol className="fo-tickets">
      {tickets.map((t) => {
        const waits = t.status === "todo" && t.blocked_by.length > 0;
        const prRepo = t.pr?.repo ?? t.repo;
        const pr = prBrief(t.pr) && (many && prRepo ? `${prRepo.split("/").pop()} ${prBrief(t.pr)}` : prBrief(t.pr));
        return (
          <li key={t.id} className={t.id === nextId ? "is-next" : ""}>
            <span className="fo-n">{t.label}</span>
            <span className="fo-tk">
              <VLink v={{ kind: "ticket", name: t.id }}>{t.title}</VLink>
              {waits && <small className="fo-wait">waits for {t.blocked_by.join(", ")}</small>}
              {(t.holder || pr) && (
                <small className="fo-wait">
                  {t.holder && <>held by <span className="mono">{t.holder}</span></>}
                  {t.holder && pr && " · "}
                  {pr && (t.pr?.url ? <a href={t.pr.url} target="_blank" rel="noreferrer">{pr}</a> : pr)}
                </small>
              )}
              {t.stage === "blocked" && t.stage_reason && <small className="fo-wait">blocked: {t.stage_reason}</small>}
              {t.verdict && (
                t.verdict.current
                  ? <small className="fo-wait">review round {t.verdict.round}: {t.verdict.verdict === "pass" ? "Pass" : t.verdict.verdict === "changes" ? "changes asked" : "blocked"}</small>
                  : <small className="fo-warn">{t.verdict.note}</small>
              )}
              {t.pr_note && <small className="fo-warn">{t.pr_note}</small>}
            </span>
            {t.stage && t.stage !== "todo" && !(t.owner === "linear" && (t.status === "done" || t.status === "canceled")) ? (
              <span className={`fo-st ${STAGE_CLASS[t.stage]}`}>{STAGE_TEXT[t.stage]}</span>
            ) : (
              <span className={`fo-st fo-st-${t.status}`}>
                {t.id === nextId && t.status === "todo" ? "next" : waits ? "blocked" : STATUS_TEXT[t.status]}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function DocList({ docs }: { docs: DocSummary[] }) {
  return (
    <ul className="fo-docs">
      {docs.map((d) => (
        <li key={d.id}>
          <VLink v={{ kind: "doc", name: d.id }}>{d.kind === "start" ? "Starting prompt" : d.title}</VLink>
          <small><TimeAgo ts={d.updated_at} /></small>
        </li>
      ))}
    </ul>
  );
}
