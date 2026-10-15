import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import { CrewState, Desk } from "../../pages/Crew";
import { TimeAgo, usePolling } from "../bits";
import { ProgressRing, ticketBucket, ticketProgress } from "../ProgressRing";
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
// a ticket's cell in the progress bar, by its bucket in the ring
const BAR_CLASS: Record<ReturnType<typeof ticketBucket>, string> = {
  done: "done", doing: "run", blocked: "blocked", todo: "", canceled: "",
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
  const phase: Phase = crew?.handover?.finished ? "done" : phaseOf(!!start, tickets);
  // counted like the ring in the header, so the ring and the counts below it agree
  const prog = ticketProgress(tickets);
  const live = tickets.filter((t) => ticketBucket(t) !== "canceled");
  const { done, doing } = prog;
  const sessions = ss?.sessions ?? [];
  const firstLinked = [...sessions].sort((a, b) => a.linked_at.localeCompare(b.linked_at))[0]?.session;
  const building = tickets.some((t) => t.status === "in_progress");
  const ms = (t?: string | null) => (t ? Date.parse(t) : NaN);
  // The session working the ticket in progress (TR-474): the crew station holding it, else the
  // newest build session active since the ticket went in progress. Never the spec session: it
  // wrote the plan, not the code, so it is never told about a build ticket.
  const doingTicket = tickets.find((t) => t.status === "in_progress");
  const active = !doingTicket ? undefined
    : doingTicket.holder ? sessions.find((s) => s.station === doingTicket.holder && s.state !== "replaced")
    : sessions.find((s) => s.state !== "replaced" && !s.station && s.session !== firstLinked
                        && ms(s.last_at) >= ms(doingTicket.updated_at) - HOLD_SLACK_MS);
  const quietMin = active ? quiet(active, ss?.now) : 0;
  // With nothing in progress, a session waiting at its prompt is not a stopped build: it is
  // idle, and the one that finished the last ticket has finished. The stored state stays raw.
  const lastDone = phase === "done"
    ? Math.max(...tickets.filter((t) => t.status === "done").map((t) => ms(t.updated_at))) : NaN;
  const finisher = Number.isNaN(lastDone) ? undefined : sessions
    .filter((s) => s.state !== "replaced" && ms(s.started_at) <= lastDone && ms(s.last_at) >= lastDone)
    .sort((a, b) => ms(b.last_at) - ms(a.last_at))[0]?.session;
  const shownState = (s: ProjectSession, _now?: string): Shown =>
    building ? null : s.session === finisher ? "finished" : s.state === "waiting" ? "idle" : null;
  // no session on the ticket: the person picks it up (the crew's orchestrator does it for a crew)
  const attention: AttentionKind | null = !building ? null
    : !active || active.state === "ended" ? (byCrew ? null : "orphan")
    : active.state === "waiting" ? "waiting"
    : active.state === "quiet" || quietMin >= SILENT_MIN ? "silent" : null;
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
          <h1 className="gf-name fo-name">{ctx.s.name}
            <span className="fo-name-tag"><span className="fo-tag">Software factory</span>
              {tk ? <ProgressRing progress={prog} size={16} /> : <span className="fo-ring-slot" />}</span></h1>
          {tk?.linear && <div className="fo-linear"><LinearMark />Tickets live in Linear project{" "}
            <a href={tk.linear.url} target="_blank" rel="noreferrer">{tk.linear.name} ↗</a></div>}
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
              <span><b>{prog.todo}</b> to do</span>
              <span><b>{doing}</b> in progress</span>
              {prog.blocked > 0 && <span><b>{prog.blocked}</b> blocked</span>}
              <span><b>{done}</b> done</span>
            </span>
          </div>
          <div className="fo-bar" aria-hidden="true">
            {live.map((t) => <i key={t.id} className={BAR_CLASS[ticketBucket(t)]} />)}
          </div>
        </div>
      )}

      {attention && doingTicket && (
        <Attention kind={attention} session={attention === "orphan" ? undefined : active}
                   quietMin={quietMin} label={active?.station ?? "build session"}
                   ticket={doingTicket.title}
                   pickUp={`Pick up Tares project "${ctx.s.name}" where the last session stopped.`} />
      )}

      {crew?.handover?.finished ? (
        <section className="fo-next fo-done">
          <h2>Done <TimeAgo ts={crew.handover.finished.at} /></h2>
          <p className="help" style={{ margin: 0 }}>
            You said: "{crew.handover.finished.words}"{crew.handover.finished.decision && <> ({crew.handover.finished.decision})</>}.
            {" "}Every ticket is merged and the crew no longer works on it. Handing it over again opens it.
          </p>
        </section>
      ) : next ? (
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

        <Fold id="tickets" title="Tickets" icon={tk?.linear ? <LinearMark /> : undefined}
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
        <Fold id="github" title="GitHub" icon={<GitHubMark />}
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
// a session counts as working a ticket when it was active at most this long before the ticket
// went in progress (its last line can reach Tares a little before the ticket's update)
const HOLD_SLACK_MS = 2 * 60_000;
// waiting / silent: the session on the ticket stopped; orphan: no session is on it (TR-474)
type AttentionKind = "waiting" | "silent" | "orphan";

// GitHub's own marks (octicons), drawn inline: the logo, a repo, a pull request
export function LinearMark() {
  return (
    <svg className="fo-linear-logo" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M2.886 4.18A11.982 11.982 0 0 1 11.99 0C18.624 0 24 5.376 24 12.009c0 3.64-1.62 6.903-4.18 9.105L2.887 4.18ZM1.817 5.626l16.556 16.556c-.524.33-1.075.62-1.65.866L.951 7.277c.247-.575.537-1.126.866-1.65ZM.322 9.163l14.515 14.515c-.71.172-1.443.282-2.195.322L0 11.358a12 12 0 0 1 .322-2.195Zm-.17 4.862 9.823 9.824a12.02 12.02 0 0 1-9.824-9.824Z" />
    </svg>
  );
}

function GitHubMark() {
  return (
    <svg className="fo-gh-logo" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}
function RepoIcon() {
  return (
    <svg className="fo-gh-ico" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 010-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 11-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1h-8a1 1 0 00-1 1v6.708A2.486 2.486 0 014.5 9h8V1.5zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z" />
    </svg>
  );
}
function PRIcon() {
  return (
    <svg className="fo-gh-ico" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path fill="currentColor" d="M1.5 3.25a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zm5.677-.177L9.573.677A.25.25 0 0110 .854V2.5h1A2.5 2.5 0 0113.5 5v5.628a2.251 2.251 0 11-1.5 0V5a1 1 0 00-1-1h-1v1.646a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm0 9.5a.75.75 0 100 1.5.75.75 0 000-1.5zm8.25.75a.75.75 0 101.5 0 .75.75 0 00-1.5 0z" />
    </svg>
  );
}

/** Where a pull request stands, in a few words: the crew's stage, CI, the reviewer's verdict. */
function prStatus(p: RepoPR): string {
  const bits: string[] = [];
  const stage: Record<string, string> = { doing: "being built", review: "waiting for review",
    changes: "changes asked", blocked: "blocked" };
  if (p.review && p.review.current && p.stage !== "changes")
    bits.push(p.review.verdict === "pass" ? "review passed" : p.review.verdict === "changes" ? "changes asked" : "review blocked");
  else if (p.stage && stage[p.stage]) bits.push(stage[p.stage]);
  if (p.ci === "success") bits.push("CI passed");
  else if (p.ci === "failure") bits.push("CI failed");
  else if (p.ci === "pending") bits.push("CI running");
  return bits.join(" · ");
}

/** The GitHub repos the project works on (TR-463): each repo, where it is checked out, and its open
 *  pull requests under it, each linked to GitHub. Merged ones fold away below them. */
function GitHubRepos({ repos, on, pick }: { repos: ProjectRepo[]; on: string | null; pick: (k: string) => void }) {
  return (
    <ul className="fo-gh">
      {repos.map((r) => {
        const k = (r.name ?? r.path) as string;
        const merged = r.merged_prs ?? [];
        return (
          <li key={k} className={`fo-gh-repo${on === k ? " is-on" : ""}`}>
            <div className="fo-gh-name">
              <RepoIcon />
              {r.name ? <a href={`https://github.com/${r.name}`} target="_blank" rel="noreferrer">{r.name}</a>
                : <span className="mono">{r.path}</span>}
              {r.path && r.name && <span className="dim fo-gh-path">(checked out at <span className="mono">{r.path}</span>)</span>}
              {!r.path && <span className="fo-warn">(not checked out on the crew's machine yet)</span>}
              <button type="button" className="fo-link-btn fo-gh-filter" aria-pressed={on === k} onClick={() => pick(k)}>
                {on === k ? "all tickets" : `${r.tickets.length} ${r.tickets.length === 1 ? "ticket" : "tickets"}`}
              </button>
            </div>
            {r.github_error && <div className="fo-warn fo-gh-err">Tares could not read GitHub for this repo ({r.github_error}).</div>}
            <ul className="fo-gh-prs">
              {r.open_prs.map((p) => (
                <li key={p.url}>
                  <span className="fo-gh-branch" aria-hidden="true">↳</span>
                  <PRIcon />
                  <a href={p.url} target="_blank" rel="noreferrer">PR #{p.number ?? "?"}</a>
                  <span className="fo-gh-title">{p.title}</span>
                  {prStatus(p) && <span className="dim fo-gh-status">{prStatus(p)}</span>}
                </li>
              ))}
              {!r.open_prs.length && <li className="dim"><span className="fo-gh-branch" aria-hidden="true">↳</span>no open pull requests</li>}
            </ul>
            {merged.length > 0 && (
              <details className="fo-gh-merged">
                <summary>{merged.length} merged</summary>
                <ul className="fo-gh-prs">
                  {merged.map((p) => (
                    <li key={p.url}>
                      <span className="fo-gh-branch" aria-hidden="true">↳</span>
                      <PRIcon />
                      <a href={p.url} target="_blank" rel="noreferrer">PR #{p.number ?? "?"}</a>
                      <span className="fo-gh-title">{p.title}</span>
                      <span className="dim fo-gh-status">merged{p.merged_at && <> <TimeAgo ts={p.merged_at} /></>}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function quiet(s: ProjectSession, now?: string): number {
  return s.last_at && now ? Math.floor((Date.parse(now) - Date.parse(s.last_at)) / 60000) : 0;
}

type Shown = "finished" | "idle" | null;

/** A section that folds, with a pin: a pinned one is open on every visit and does not fold.
 *  Pins are this browser's, per section, across projects. */
function Fold({ id, title, icon, meta, pinnedByDefault, children }: {
  id: string; title: string; icon?: React.ReactNode; meta: React.ReactNode; pinnedByDefault?: boolean;
  children: React.ReactNode;
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
             onToggle={(e) => {
               // only its own: a fold inside (the merged pull requests) toggling is not this one
               if (e.target !== e.currentTarget || pinned) return;
               setOpen((e.target as HTMLDetailsElement).open);
             }}>
      <summary onClick={(e) => { if (pinned) e.preventDefault(); }}>
        <span className="fo-fold-h">{icon}{title}</span>
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
  if (s.state === "quiet") return <span className="fo-st fo-st-quiet" title="nothing heard from it for a long while; it has most likely ended">quiet</span>;
  if (s.state === "replaced") return <span className="fo-st" title={s.state_reason ?? undefined}>replaced</span>;
  if (s.state === "working") {
    return quiet(s, now) >= SILENT_MIN
      ? <span className="fo-st fo-st-quiet" title="nothing sent for a while">quiet</span>
      : <span className="fo-st fo-st-in_progress">working</span>;
  }
  return null;
}

/** A build that has stopped: the session on the ticket waits for the person or has gone silent,
 *  or no session is on it at all. */
function Attention({ kind, session, quietMin, label, ticket, pickUp }: {
  kind: AttentionKind; session?: ProjectSession; quietMin: number; label: string; ticket?: string;
  pickUp: string;
}) {
  if (kind === "orphan" || !session) {
    return (
      <section className="fo-attn fo-attn-silent" role="status" aria-labelledby="fo-attn-h">
        <h2 id="fo-attn-h">{ticket ? <>Ticket "{ticket}" is in progress, but no session is on it</> : <>No session is on the build</>}</h2>
        <p className="help" style={{ margin: 0 }}>
          No Claude Code session working on this ticket is open. Paste the pick-up prompt into a
          fresh Claude Code session with the Tares plugin, in the project's repository: it reads
          where the build stopped and carries on.
        </p>
        <div className="fo-cmd">
          <code>{pickUp}</code>
          <CopyButton text={pickUp} label="Copy pick-up prompt" />
        </div>
      </section>
    );
  }
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
