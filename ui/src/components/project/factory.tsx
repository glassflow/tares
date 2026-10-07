import { useState } from "react";

import { api } from "../../api";
import { TimeAgo, usePolling } from "../bits";
import { VLink, type Ctx } from "./common";
import { Goal } from "./overview";
import type { DocSummary, Milestone, ProjectSession, Ticket } from "../../types";

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
  const tickets = tk?.tickets ?? [];
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
    { key: "review", title: "Review", text: "the last ticket: verify and open the PR" },
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
          <CopyButton text={prompt} label="Copy build prompt" primary />
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
                   label={active.session === firstLinked ? "spec session" : "build session"}
                   ticket={next?.status === "in_progress" ? next.title : undefined}
                   pickUp={`Pick up Tares project "${ctx.s.name}" where the last session stopped.`} />
      )}

      {next ? (
        <NextUp key={next.id} ctx={ctx} next={next} of={tickets.indexOf(next) + 1} total={tickets.length}
                prompt={prompt} waiting={attention !== null && next.status === "in_progress"} />
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
              meta={[`${tickets.length} · ${done} done`,
                     milestones.length ? `${milestones.length} ${milestones.length === 1 ? "milestone" : "milestones"}` : "",
                     tk?.linear ? "in Linear" : "in Tares"].filter(Boolean).join(" · ")}>
          {milestones.length ? (
            <>
              {milestones.map((m) => (
                <MilestoneGroup key={m.id} m={m} tickets={tickets.filter((t) => t.milestone === m.id)} nextId={next?.id} />
              ))}
              {tickets.some((t) => !t.milestone) && (
                <>
                  <div className="fo-sub">No milestone</div>
                  <TicketList tickets={tickets.filter((t) => !t.milestone)} nextId={next?.id} />
                </>
              )}
            </>
          ) : <TicketList tickets={tickets} nextId={next?.id} />}
          <div className="fo-foot"><VLink v={{ kind: "tickets" }} className="fo-link">All tickets</VLink></div>
        </Fold>
      </div>

      <Fold id="sessions" title="Sessions"
            meta={<>{sessions.length} · {sessions[0]?.last_at ? <>last active <TimeAgo ts={sessions[0].last_at} /></> : "none yet"}</>}>
        {sessions.length ? (
          <ul className="fo-docs">
            {sessions.map((x) => (
              <li key={x.session}>
                <span className="fo-sess">
                  <VLink v={{ kind: "claude", name: x.session }}>
                    {x.session === firstLinked ? "Spec session" : "Build session"}{x.repo && <> · {x.repo}</>}
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
function NextUp({ ctx, next, of, total, prompt, waiting }: {
  ctx: Ctx; next: Ticket; of: number; total: number; prompt: string; waiting?: boolean;
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
      <div className="fo-cmd">
        <code>{prompt}</code>
        <CopyButton text={prompt} label="Copy" />
      </div>
      <span className="help">Paste it into a new Claude Code session with the Tares plugin, in the project's repository.</span>
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

function TicketList({ tickets, nextId }: { tickets: Ticket[]; nextId?: string }) {
  return (
    <ol className="fo-tickets">
      {tickets.map((t) => {
        const waits = t.status === "todo" && t.blocked_by.length > 0;
        return (
          <li key={t.id} className={t.id === nextId ? "is-next" : ""}>
            <span className="fo-n">{t.label}</span>
            <span className="fo-tk">
              <VLink v={{ kind: "ticket", name: t.id }}>{t.title}</VLink>
              {waits && <small className="fo-wait">waits for {t.blocked_by.join(", ")}</small>}
            </span>
            <span className={`fo-st fo-st-${t.status}`}>
              {t.id === nextId && t.status === "todo" ? "next" : waits ? "blocked" : STATUS_TEXT[t.status]}
            </span>
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
