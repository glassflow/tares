import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import { Combo, ErrorState, Picker, TimeAgo, formatBytes, usePolling } from "../bits";
import { Facts, NotHere, VLink, ViewHead, type Ctx } from "./common";
import { prBrief } from "./factory";
import type { Doc, DocKind, Ticket, TicketStatus } from "../../types";

// What a spec session leaves in a project for the session that builds it (TR-403, TR-407): the
// docs (starting prompt, spec, plan, AGENTS.md, notes), the tickets in order with a working doc
// each (in Tares, or synced from Linear), and the Claude Code sessions that worked here.

export const KIND_LABEL: Record<DocKind, string> = {
  start: "Starting prompt", spec: "Spec", plan: "Plan", agents: "AGENTS.md", memory: "Memory",
  grants: "Grants", note: "Note", working: "Working doc",
};
const KIND_HELP: Record<DocKind, string> = {
  start: "what a session that builds the project reads first",
  spec: "what is built and why",
  plan: "milestones and the order of work",
  agents: "conventions, commands, how to test",
  memory: "facts and preferences that hold across projects",
  grants: "what you allow a tares-factory crew to do, in your words, dated",
  note: "anything else worth keeping with the project",
  working: "how to do one ticket: context, steps, files, how to verify",
};
// a person adds these kinds; memory is the shared one, made by Tares
const KINDS: DocKind[] = ["start", "spec", "plan", "agents", "note", "working"];
const STATUS_LABEL: Record<TicketStatus, string> = {
  todo: "to do", in_progress: "in progress", done: "done", canceled: "canceled",
};
const STATUSES: TicketStatus[] = ["todo", "in_progress", "done", "canceled"];
const MAX_DOC = 256 * 1024;

function StatusBadge({ s }: { s: TicketStatus }) {
  const cls = s === "done" ? "ok" : s === "in_progress" ? "running" : s === "canceled" ? "paused" : "";
  return <span className={`badge ${cls}`}>{STATUS_LABEL[s]}</span>;
}

function By({ d }: { d: { updated_by: string | null; updated_at: string } }) {
  return <>changed <TimeAgo ts={d.updated_at} />{d.updated_by && <> by {d.updated_by}</>}</>;
}

/** Write or change a doc: kind, title, markdown with a live preview. */
export function DocEditor({ project, initial, fixedKind, onSaved, onCancel }: {
  project: string;
  initial?: Doc;
  fixedKind?: DocKind;
  onSaved: (d: Doc) => void;
  onCancel: () => void;
}) {
  const [kind, setKind] = useState<DocKind>(initial?.kind ?? fixedKind ?? "spec");
  const [title, setTitle] = useState(initial?.title ?? (fixedKind ? KIND_LABEL[fixedKind] : ""));
  const [body, setBody] = useState(initial?.body ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const bytes = new TextEncoder().encode(body).length;
  const canSave = !busy && title.trim() !== "" && bytes <= MAX_DOC;
  const save = async () => {
    setBusy(true); setErr(undefined);
    try {
      const d = initial
        ? await api.updateDoc(project, initial.id, { kind, title: title.trim(), body })
        : await api.createDoc(project, { kind, title: title.trim(), body });
      onSaved(d);
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <h3 style={{ margin: "0 0 10px" }}>{initial ? <>Edit {initial.title}</> : "New doc"}</h3>
      {err && <div className="alert error">{err}</div>}
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <label className="field" style={{ flex: "2 1 280px" }}>
          <span className="lbl">title</span>
          <input type="text" autoFocus={!initial} value={title} maxLength={200}
                 onChange={(e) => setTitle(e.target.value)} />
        </label>
        {!fixedKind && !initial?.global && (
          <div className="field" style={{ flex: "1 1 200px" }}>
            <span className="lbl">kind</span>
            <Picker value={kind} onChange={(v) => setKind(v as DocKind)} options={KINDS} labels={KIND_LABEL}
                    ariaLabel="kind" />
            <span className="help">{KIND_HELP[kind]}</span>
          </div>
        )}
      </div>
      <div className="skill-editor">
        <label className="field" style={{ margin: 0 }}>
          <span className="lbl">text</span>
          <textarea className="code" rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
          <span className="help" style={bytes > MAX_DOC ? { color: "var(--err)" } : undefined}>
            markdown · {formatBytes(bytes)} of 256 KiB
          </span>
        </label>
        <div className="field" style={{ margin: 0 }}>
          <span className="lbl">preview</span>
          <div className="skill-preview md">
            {body.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
              : <span className="dim">the doc, as a session reads it</span>}
          </div>
        </div>
      </div>
      <div className="btnrow" style={{ marginTop: 12 }}>
        <button className="primary" disabled={!canSave} onClick={save}>
          {busy ? "saving…" : initial ? "Save" : "Add doc"}
        </button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

/** The project's docs in reading order, working docs listed with their tickets instead. */
export function DocsView({ ctx }: { ctx: Ctx }) {
  const { data, error, reload } = usePolling(() => api.docs(ctx.id), 15000);
  const adding = ctx.params.get("add") === "1";
  const setAdding = (open: boolean) => ctx.go({ kind: "docs" }, open ? { add: "1" } : undefined, true);
  if (error && !data) return <ErrorState error={error} what="the docs" onRetry={reload} />;
  const docs = (data ?? []).filter((d) => d.kind !== "working" && !d.global);
  const shared = (data ?? []).filter((d) => d.global);
  const working = (data ?? []).filter((d) => d.kind === "working").length;
  return (
    <>
      <ViewHead title="Docs"
                sub="What a session that builds this project reads: the starting prompt, the spec, the plan and the AGENTS.md. A spec session writes them; you can edit any of them here.">
        <button type="button" className="primary" onClick={() => setAdding(!adding)}>New doc</button>
      </ViewHead>
      {adding && (
        <DocEditor project={ctx.id} onCancel={() => setAdding(false)}
                   onSaved={(d) => { reload(); ctx.go({ kind: "doc", name: d.id }); }} />
      )}
      {data === undefined ? <div className="dim">loading…</div> : docs.length ? (
        <table>
          <thead><tr><th>doc</th><th>kind</th><th className="num">size</th><th>changed</th></tr></thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id} className="clickable" onClick={() => ctx.go({ kind: "doc", name: d.id })}>
                <td><VLink v={{ kind: "doc", name: d.id }}>{d.title}</VLink></td>
                <td><span className="chip">{KIND_LABEL[d.kind]}</span></td>
                <td className="num">{formatBytes(d.size ?? 0)}</td>
                <td><By d={d} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : !adding && (
        <div className="empty">
          No docs yet. Run <span className="mono">/tares:spec</span> in Claude Code to spec this
          project with Claude, or add a doc with New doc.
        </div>
      )}
      {shared.length > 0 && (
        <>
          <h3 style={{ margin: "22px 0 8px" }}>Shared by every project</h3>
          <p className="help" style={{ margin: "0 0 8px" }}>
            Your standing rules and what holds everywhere. Sessions read them in every project and
            add a line when you tell them something that applies beyond one project.
          </p>
          <table>
            <thead><tr><th>doc</th><th className="num">size</th><th>changed</th></tr></thead>
            <tbody>
              {shared.map((d) => (
                <tr key={d.id} className="clickable" onClick={() => ctx.go({ kind: "doc", name: d.id })}>
                  <td><VLink v={{ kind: "doc", name: d.id }}>{d.title}</VLink></td>
                  <td className="num">{formatBytes(d.size ?? 0)}</td>
                  <td><By d={d} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {working > 0 && (
        <p className="help" style={{ marginTop: 12 }}>
          {working} working {working === 1 ? "doc sits" : "docs sit"} with {working === 1 ? "its ticket" : "their tickets"}:{" "}
          <VLink v={{ kind: "tickets" }}>see the tickets</VLink>.
        </p>
      )}
    </>
  );
}

/** One doc: read it, edit it, take it out of the project. */
export function DocView({ ctx, name }: { ctx: Ctx; name: string }) {
  const { data, error, reload } = usePolling(() => api.doc(ctx.id, name), 30000);
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  if (error && !data) return /404/.test(String(error))
    ? <NotHere what="doc" name={name} back={{ kind: "docs" }} />
    : <ErrorState error={error} what="this doc" onRetry={reload} />;
  if (!data) return <div className="dim">loading…</div>;
  const shared = data.projects.filter((p) => p !== ctx.id);
  return (
    <>
      <ViewHead title={data.title}
                sub={data.global ? <>Shared by every project · {KIND_HELP[data.kind]}</>
                  : <>{KIND_LABEL[data.kind]} · {KIND_HELP[data.kind]}</>}>
        {!editing && <>
          <button className="primary" onClick={() => setEditing(true)}>Edit</button>
          {!data.global && <button className="danger" onClick={() => setConfirmDel(true)}>Remove</button>}
        </>}
      </ViewHead>
      {editing ? (
        <DocEditor project={ctx.id} initial={data} onCancel={() => setEditing(false)}
                   onSaved={() => { setEditing(false); reload(); }} />
      ) : (
        <>
          <Facts rows={[
            ["changed", <By d={data} />],
            ...(shared.length && !data.global ? [["also in", shared.map(ctx.projectName).join(", ")] as [string, React.ReactNode]] : []),
          ]} />
          <div className="panel md" style={{ marginTop: 14 }}>
            {data.body.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.body}</ReactMarkdown>
              : <span className="dim">empty</span>}
          </div>
        </>
      )}
      {confirmDel && (
        <ConfirmDialog title={`Remove ${data.title} from this project?`} danger confirmLabel="Remove"
          message={shared.length
            ? "The other projects that include it keep it."
            : "It is deleted. A ticket whose working doc it was loses the link."}
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setConfirmDel(false);
            try { await api.deleteDoc(ctx.id, name); ctx.go({ kind: data.kind === "working" ? "tickets" : "docs" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }} />
      )}
    </>
  );
}

/** Where the tickets live: link a Linear project, or see the link and its last sync. */
function LinearPanel({ ctx, linked, hasOwn, onChanged }: {
  ctx: Ctx; linked: { name: string; url: string; synced_at?: string | null; error?: string | null } | null;
  hasOwn: boolean; onChanged: () => void;
}) {
  const { data: conn } = usePolling(() => api.linear(), 60000);
  const [picking, setPicking] = useState(false);
  const [q, setQ] = useState("");
  const [projects, setProjects] = useState<{ id: string; name: string; url: string }[]>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(undefined);
    try { await fn(); onChanged(); } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const search = async (text: string) => {
    setQ(text);
    try { setProjects((await api.linearProjects(text)).projects); } catch (e) { setErr(String((e as Error).message ?? e)); }
  };
  if (linked) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        {err && <div className="alert error">{err}</div>}
        {linked.error && <div className="alert warn">The last sync with Linear failed: {linked.error}</div>}
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <span>
            Tickets live in Linear project{" "}
            <a href={linked.url} target="_blank" rel="noreferrer">{linked.name}</a>.{" "}
            <span className="help">
              Tares keeps this list in sync about once a minute
              {linked.synced_at && <>; last synced <TimeAgo ts={linked.synced_at} /></>}.
            </span>
          </span>
          <span className="btnrow">
            <button type="button" disabled={busy} onClick={() => run(() => api.syncLinear(ctx.id))}>
              {busy ? "syncing…" : "Sync now"}</button>
            <button type="button" disabled={busy} title="the tickets stay, as Tares tickets"
                    onClick={() => run(() => api.unlinkLinear(ctx.id))}>Stop using Linear</button>
          </span>
        </div>
      </div>
    );
  }
  if (hasOwn || !conn) return null;
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      {err && <div className="alert error">{err}</div>}
      {!conn.connected ? (
        <p className="help" style={{ margin: 0 }}>
          The tickets can live in Linear instead: <a href="/settings?tab=linear">connect Linear</a> first.
        </p>
      ) : !picking ? (
        <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
          <span className="help">Keep the tickets in a Linear project instead, and Tares keeps this list in sync.</span>
          <button type="button" onClick={() => { setPicking(true); search(""); }}>Use a Linear project</button>
        </div>
      ) : (
        <div className="field" style={{ maxWidth: 420, margin: 0 }}>
          <span className="lbl">Linear project</span>
          <Combo value={q} placeholder="search your Linear projects…"
                 options={(projects ?? []).map((p) => p.name)}
                 onChange={(v) => {
                   const hit = (projects ?? []).find((p) => p.name === v);
                   if (hit) run(() => api.linkLinear(ctx.id, hit.id)).then(() => setPicking(false));
                   else search(v);
                 }} />
          <span className="help">{busy ? "linking and reading its tickets…" : "its issues become this project's tickets"}</span>
        </div>
      )}
    </div>
  );
}

/** The project's tickets in order, each with its working doc. */
export function TicketsView({ ctx }: { ctx: Ctx }) {
  const { data, error, reload } = usePolling(() => api.tickets(ctx.id), 15000);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  if (error && !data) return <ErrorState error={error} what="the tickets" onRetry={reload} />;
  const tickets = data?.tickets ?? [];
  const linked = data?.linear ?? null;
  const add = async () => {
    try { await api.createTicket(ctx.id, { title: title.trim() }); setTitle(""); setAdding(false); reload(); }
    catch (e) { ctx.fail(e); }
  };
  const setStatus = async (t: Ticket, status: TicketStatus) => {
    try { await api.updateTicket(ctx.id, t.id, { status }); reload(); } catch (e) { ctx.fail(e); }
  };
  return (
    <>
      <ViewHead title="Tickets"
                sub="The work, in order. Each ticket has a working doc a session can do it from.">
        {!linked && <button type="button" className="primary" onClick={() => setAdding(!adding)}>New ticket</button>}
      </ViewHead>
      {data && <LinearPanel ctx={ctx} linked={linked} hasOwn={tickets.some((t) => t.owner === "tares")} onChanged={reload} />}
      {adding && (
        <div className="panel" style={{ marginBottom: 12 }}>
          <label className="field" style={{ maxWidth: 520, margin: 0 }}>
            <span className="lbl">title</span>
            <input type="text" autoFocus value={title} maxLength={200}
                   onChange={(e) => setTitle(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && title.trim()) add(); }} />
          </label>
          <div className="btnrow" style={{ marginTop: 10 }}>
            <button className="primary" disabled={!title.trim()} onClick={add}>Add ticket</button>
            <button type="button" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {data === undefined ? <div className="dim">loading…</div> : tickets.length ? (
        <table>
          <thead><tr><th>#</th><th>ticket</th><th>milestone</th><th>status</th><th>working doc</th></tr></thead>
          <tbody>
            {tickets.map((t) => (
              <tr key={t.id} className="clickable" onClick={() => ctx.go({ kind: "ticket", name: t.id })}>
                <td className="dim">{t.label}</td>
                <td>
                  <VLink v={{ kind: "ticket", name: t.id }}>{t.title}</VLink>
                  {t.status === "todo" && t.blocked_by.length > 0 &&
                    <div className="dim" style={{ fontSize: 12 }}>waits for {t.blocked_by.join(", ")}</div>}
                </td>
                <td>{t.milestone_name ?? <span className="dim">none</span>}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {t.owner === "tares"
                    ? <Picker value={t.status} options={STATUSES} labels={STATUS_LABEL}
                              onChange={(v) => setStatus(t, v as TicketStatus)} ariaLabel="status" />
                    : <StatusBadge s={t.status} />}
                </td>
                <td>{t.working_doc_title ?? <span className="dim">none yet</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : !adding && (
        <div className="empty">
          No tickets yet. A spec session (<span className="mono">/tares:spec</span> in Claude Code)
          writes them{linked ? " in Linear" : ""}{linked ? "" : ", or add one with New ticket"}.
        </div>
      )}
    </>
  );
}

/** Who moved the ticket and when, and the crew's messages about it (TR-427, TR-429): folded,
 *  shown only once there is something in them. */
function TicketCrew({ ctx, ticket }: { ctx: Ctx; ticket: Ticket }) {
  const { data } = usePolling(() => api.ticketMessages(ctx.id, ticket.id), 30000);
  const history = ticket.history ?? [];
  const msgs = data?.messages ?? [];
  const assumptions = ticket.assumptions ?? [];
  const checks = ticket.checks ?? [];
  if (!history.length && !msgs.length && !assumptions.length && !checks.length) return null;
  return (
    <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
      {assumptions.length > 0 && (
        <details className="panel" open={assumptions.some((a) => a.state === "open")}>
          <summary><b>Assumptions</b> <span className="dim">· {assumptions.length} · {assumptions.filter((a) => a.state === "open").length} open</span></summary>
          <ul className="tk-history">
            {assumptions.map((a) => (
              <li key={a.label}>
                <b>{a.label}</b> <span className="dim">({a.state})</span> {a.question} <b>→</b> {a.choice}
                <div className="dim">{a.why}{a.made_by && <> · by {a.made_by}</>}{a.words && <> · you said: "{a.words}"</>}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
      {checks.length > 0 && (
        <details className="panel">
          <summary><b>Checks run</b> <span className="dim">· {checks.length}{checks.some((c) => c.match && c.match !== "matched") ? ` · ${checks.filter((c) => c.match && c.match !== "matched").length} not in the recording` : ""}</span></summary>
          <ul className="tk-history">
            {checks.map((c) => (
              <li key={c.id}>
                {c.commit && <span className="mono dim">{c.commit.slice(0, 7)} </span>}<code>{c.command}</code> → {c.result}
                {c.broke_test && <span className="dim"> · broke the code under {c.broke_test}: it failed, then passed</span>}
                {c.match === "not_found" && <div className="fo-warn">not found in the recording</div>}
                {c.match === "differs" && <div className="fo-warn">the recording shows something else{c.recorded && <>: {c.recorded.slice(0, 200)}</>}</div>}
                <div className="dim"><TimeAgo ts={c.at} />{c.by && <> · {c.by}</>}</div>
              </li>
            ))}
          </ul>
        </details>
      )}
      {history.length > 0 && (
        <details className="panel">
          <summary><b>History</b> <span className="dim">· {history.length} {history.length === 1 ? "change" : "changes"}</span></summary>
          <ul className="tk-history">
            {history.map((h, i) => (
              <li key={i}>
                <TimeAgo ts={h.at} /> · {h.field === "holder" ? <>given to <span className="mono">{h.value}</span></> : <>moved to <b>{h.value}</b></>}
                {h.reason && <span className="dim">: {h.reason}</span>}
                {h.by && <span className="dim"> · by {h.by}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {msgs.length > 0 && (
        <details className="panel" open>
          <summary><b>Crew messages</b> <span className="dim">· {msgs.length}</span></summary>
          <ul className="tk-msgs">
            {msgs.map((m, i) => (
              <li key={i}>
                <details>
                  <summary>
                    <span className="mono">{m.type}</span> · {m.from || "a session"} → {m.to} · <TimeAgo ts={m.at} />
                    {m.first_line && <> · {m.first_line}</>}
                  </summary>
                  <pre>{m.text}</pre>
                </details>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** One ticket and its working doc. */
export function TicketView({ ctx, name }: { ctx: Ctx; name: string }) {
  const { data, error, reload } = usePolling(() => api.ticket(ctx.id, name), 15000);
  const [editingDoc, setEditingDoc] = useState(false);
  const [doc, setDoc] = useState<Doc>();
  const [confirmDel, setConfirmDel] = useState(false);
  if (error && !data) return /404/.test(String(error))
    ? <NotHere what="ticket" name={name} back={{ kind: "tickets" }} />
    : <ErrorState error={error} what="this ticket" onRetry={reload} />;
  if (!data) return <div className="dim">loading…</div>;
  const linear = data.owner === "linear";
  const editDoc = async () => {
    try { setDoc(data.working_doc ? await api.doc(ctx.id, data.working_doc) : undefined); setEditingDoc(true); }
    catch (e) { ctx.fail(e); }
  };
  return (
    <>
      <ViewHead title={data.title}
                sub={linear ? <>Lives in Linear as{" "}
                  {data.url ? <a href={data.url} target="_blank" rel="noreferrer">{data.identifier}</a> : data.identifier}:
                  change its title and status there.</> : undefined}>
        {!editingDoc && <>
          <button className="primary" onClick={editDoc}>{data.working_doc ? "Edit working doc" : "Write working doc"}</button>
          {!linear && <button className="danger" onClick={() => setConfirmDel(true)}>Delete</button>}
        </>}
      </ViewHead>
      <Facts rows={[
        ["ticket", data.label],
        ["status", <StatusBadge s={data.status} />],
        ["milestone", data.milestone_name ?? <span className="dim">none</span>],
        ["depends on", data.depends_on.length ? data.depends_on.join(", ") : <span className="dim">nothing</span>],
        ["ready", data.status === "todo"
          ? (data.ready ? "yes" : `no, waits for ${data.blocked_by.join(", ")}`)
          : <span className="dim">{data.status === "in_progress" ? "being worked on" : "finished"}</span>],
        ...(data.holder || data.stage ? [
          ["held by", data.holder ? <span className="mono">{data.holder}</span> : <span className="dim">nobody yet</span>],
          ["stage", <>{data.stage ?? "todo"}{data.stage_reason && <span className="dim">: {data.stage_reason}</span>}</>],
        ] as [string, React.ReactNode][] : []),
        ...(data.pr?.url ? [
          ["pull request", <><a href={data.pr.url} target="_blank" rel="noreferrer">{prBrief(data.pr)}</a>
            {data.pr.head && <span className="dim"> · head {data.pr.head.slice(0, 7)}</span>}
            {data.pr.error && <span className="dim"> · {data.pr.error}</span>}</>],
        ] as [string, React.ReactNode][] : []),
        ["changed", <TimeAgo ts={data.updated_at} />],
      ]} />
      {data.pr_note && <p className="fo-warn" style={{ marginTop: 10, fontSize: 14 }}>{data.pr_note}</p>}
      <TicketCrew ctx={ctx} ticket={data} />
      {editingDoc ? (
        <div style={{ marginTop: 14 }}>
          <DocEditor project={ctx.id} initial={doc} fixedKind="working"
                     onCancel={() => setEditingDoc(false)}
                     onSaved={async (d) => {
                       try {
                         if (!data.working_doc) await api.updateTicket(ctx.id, data.id, { working_doc: d.id });
                         setEditingDoc(false); reload();
                       } catch (e) { ctx.fail(e); }
                     }} />
        </div>
      ) : data.working_doc_body != null ? (
        <div className="panel md" style={{ marginTop: 14 }}>
          <h3 style={{ marginTop: 0 }}>{data.working_doc_title}</h3>
          {data.working_doc_body.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.working_doc_body}</ReactMarkdown>
            : <span className="dim">empty</span>}
        </div>
      ) : (
        <div className="empty" style={{ marginTop: 14 }}>
          No working doc yet. A session doing this ticket needs one: context, steps, files likely
          touched, how to verify.
        </div>
      )}
      {confirmDel && (
        <ConfirmDialog title={`Delete ticket ${data.title}?`} danger confirmLabel="Delete"
          message="Its working doc stays in the project's docs."
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setConfirmDel(false);
            try { await api.deleteTicket(ctx.id, data.id); ctx.go({ kind: "tickets" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }} />
      )}
    </>
  );
}

/** The Claude Code sessions that worked in this project, the spec session among them. */
export function ClaudeSessionsView({ ctx }: { ctx: Ctx }) {
  const { data, error, reload } = usePolling(() => api.projectSessions(ctx.id), 15000);
  if (error && !data) return <ErrorState error={error} what="the sessions" onRetry={reload} />;
  const rows = data?.sessions ?? [];
  return (
    <>
      <ViewHead title="Claude Code sessions"
                sub="Sessions that worked in this project, recorded whole: the conversation from before the project existed is included." />
      {data === undefined ? <div className="dim">loading…</div> : rows.length ? (
        <table>
          <thead><tr><th>started</th><th>repo</th><th className="num">lines</th><th>last activity</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.session} className="clickable" onClick={() => ctx.go({ kind: "claude", name: r.session })}>
                <td><VLink v={{ kind: "claude", name: r.session }}>{r.started_at ? <TimeAgo ts={r.started_at} /> : r.session.slice(0, 8)}</VLink></td>
                <td>{r.repo ?? <span className="dim">unknown</span>}</td>
                <td className="num">{r.lines.toLocaleString()}</td>
                <td>{r.last_at ? <TimeAgo ts={r.last_at} /> : <span className="dim">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty">
          No Claude Code session has worked in this project yet. A session with the Tares plugin
          shows up here once it creates or names the project.
        </div>
      )}
    </>
  );
}

/** One session, top to bottom. */
export function ClaudeSessionView({ ctx, name }: { ctx: Ctx; name: string }) {
  const { data, error, reload } = usePolling(() => api.projectSession(ctx.id, name), 15000);
  if (error && !data) return /404/.test(String(error))
    ? <NotHere what="session" name={name} back={{ kind: "claude" }} />
    : <ErrorState error={error} what="this session" onRetry={reload} />;
  if (!data) return <div className="dim">loading…</div>;
  // Claude Code's own bookkeeping lines carry no text worth reading here
  const all = data.lines.filter((l) => l.text && l.text.trim() !== (l.labels.type ?? l.event_type));
  // a subagent's lines (TR-434) fold under it: the session's own lines first, then each subagent
  // with the task it was given (its first line) and the summary it returned (its last)
  const lines = all.filter((l) => !l.labels.subagent);
  const subs = new Map<string, typeof all>();
  for (const l of all) {
    if (l.labels.subagent) subs.set(l.labels.subagent, [...(subs.get(l.labels.subagent) ?? []), l]);
  }
  return (
    <>
      <ViewHead title="Claude Code session" sub={<span className="mono">{name}</span>} />
      <div className="panel" style={{ padding: 0 }}>
        {lines.map((l, i) => <SessionLine key={i} l={l} first={!i} />)}
        {!lines.length && <div className="empty">No lines recorded.</div>}
      </div>
      {subs.size > 0 && (
        <div style={{ display: "grid", gap: 8, marginTop: 14 }}>
          <h3 style={{ margin: 0 }}>Subagents <span className="dim">· {subs.size}</span></h3>
          {[...subs.entries()].map(([id, ls]) => (
            <details key={id} className="panel" style={{ padding: 0 }}>
              <summary style={{ padding: "8px 12px", cursor: "pointer" }}>
                <span className="mono">{id.slice(0, 10)}</span> · {ls.length} lines · <TimeAgo ts={ls[0].event_time} />
                <div className="dim" style={{ fontSize: 13, marginTop: 4 }}>
                  <b>Task:</b> {ls[0].text.slice(0, 300)}{ls[0].text.length > 300 ? "…" : ""}
                </div>
                {ls.length > 1 && (
                  <div className="dim" style={{ fontSize: 13, marginTop: 2 }}>
                    <b>Returned:</b> {ls[ls.length - 1].text.slice(0, 300)}{ls[ls.length - 1].text.length > 300 ? "…" : ""}
                  </div>
                )}
              </summary>
              {ls.map((l, i) => <SessionLine key={i} l={l} first={false} />)}
            </details>
          ))}
        </div>
      )}
    </>
  );
}

function SessionLine({ l, first }: { l: { event_type: string; text: string; event_time: string; labels: Record<string, string> }; first: boolean }) {
  return (
    <div className="cc-line" style={{ padding: "6px 12px", borderTop: first ? undefined : "1px solid var(--line)" }}>
      <span className="help" style={{ marginRight: 8 }}><TimeAgo ts={l.event_time} /> · {l.labels.type ?? l.event_type}</span>
      <span style={{ whiteSpace: "pre-wrap" }}>{l.text}</span>
    </div>
  );
}
