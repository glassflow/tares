import { useState } from "react";

import { api } from "../../api";
import { TimeAgo, usePolling } from "../bits";
import { VLink, type Ctx } from "./common";
import { Goal } from "./overview";
import type { DocSummary, Ticket } from "../../types";

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
  const tickets = tk?.tickets ?? [];
  const next = tickets.find((t) => t.status === "in_progress") ?? tickets.find((t) => t.status === "todo");

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

      {next ? (
        <NextUp key={next.id} ctx={ctx} next={next} of={tickets.indexOf(next) + 1} total={tickets.length}
                prompt={prompt} />
      ) : tk && (
        <section className="fo-next fo-next-empty">
          {tickets.length
            ? <h2>Every ticket is done.</h2>
            : <><h2>No tickets yet</h2><p className="help">Run <span className="mono">/tares:spec</span> in Claude Code to spec this project, or add tickets under Setup.</p></>}
        </section>
      )}

      <div className="fo-cols">
        <section className="fo-panel" aria-labelledby="fo-docs-h">
          <header><h3 id="fo-docs-h">Docs</h3><VLink v={{ kind: "docs" }} className="fo-link">All docs</VLink></header>
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
        </section>

        <details className="fo-panel fo-fold">
          <summary>
            <span className="fo-fold-h">Tickets</span>
            <span className="fo-meta">{tickets.length} · {done} done · {tk?.linear ? "in Linear" : "in Tares"}</span>
          </summary>
          <ol className="fo-tickets">
            {tickets.map((t, i) => (
              <li key={t.id} className={t.id === next?.id ? "is-next" : ""}>
                <span className="fo-n">{t.identifier ?? i + 1}</span>
                <VLink v={{ kind: "ticket", name: t.id }}>{t.title}</VLink>
                <span className={`fo-st fo-st-${t.status}`}>{t.id === next?.id && t.status === "todo" ? "next" : STATUS_TEXT[t.status]}</span>
              </li>
            ))}
          </ol>
          <div className="fo-foot"><VLink v={{ kind: "tickets" }} className="fo-link">All tickets</VLink></div>
        </details>
      </div>

      <details className="fo-panel fo-fold">
        <summary>
          <span className="fo-fold-h">Sessions</span>
          <span className="fo-meta">
            {sessions.length} · {sessions[0]?.last_at ? <>last active <TimeAgo ts={sessions[0].last_at} /></> : "none yet"}
          </span>
        </summary>
        {sessions.length ? (
          <ul className="fo-docs">
            {sessions.map((x) => (
              <li key={x.session}>
                <VLink v={{ kind: "claude", name: x.session }}>
                  {x.session === firstLinked ? "Spec session" : "Build session"}{x.repo && <> · {x.repo}</>}
                </VLink>
                <small>{x.started_at ? <TimeAgo ts={x.started_at} /> : null} · {x.lines} lines</small>
              </li>
            ))}
          </ul>
        ) : <p className="fo-empty">No Claude Code session has worked in this project yet.</p>}
      </details>
    </div>
  );
}

/** The ticket a build session takes next, with what it is about and the sentence to start one. */
function NextUp({ ctx, next, of, total, prompt }: {
  ctx: Ctx; next: Ticket; of: number; total: number; prompt: string;
}) {
  const { data: full } = usePolling(() => api.ticket(ctx.id, next.id), 30000);
  const text = gist(full?.working_doc_body);
  return (
    <section className="fo-next" aria-labelledby="fo-next-h">
      <span className="fo-label">
        {next.status === "in_progress" ? "In progress" : "Next up"} · ticket {of} of {total}
        {next.identifier && <> · {next.identifier}</>}
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
