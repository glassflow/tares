import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import ConfirmDialog from "./ConfirmDialog";
import { ErrorState, TimeAgo, formatBytes, usePolling } from "./bits";
import type { SkillSummary } from "../types";

// The Skills tab of a project (TR-332): instructions the project's agents load by name. Every
// agent here sees each skill's name and description, and loads the body with its `skill` tool
// when a task matches. Written here, or uploaded as a SKILL.md (front matter, then the body).

const NAME_RE = /^[a-z0-9-]{1,64}$/;

type Draft = { name: string; description: string; body: string; editing?: string };

export default function SkillsPanel({ project, onOpenAgent }: {
  project: string;
  onOpenAgent: (name: string) => void;
}) {
  const { data, error, reload } = usePolling(() => api.skills(project), 15000);
  const [draft, setDraft] = useState<Draft>();
  const [open, setOpen] = useState<string>();
  const [openBody, setOpenBody] = useState<string>();
  const [confirmDel, setConfirmDel] = useState<string>();
  const [msg, setMsg] = useState<string>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const fail = (e: unknown) => setErr(String((e as Error).message ?? e));

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setErr(undefined); setMsg(undefined);
    try {
      const out = await api.uploadSkill(project, await file.text());
      setMsg(`${out.created ? "Added" : "Replaced"} ${out.name} from ${file.name}`);
      reload();
    } catch (e) { fail(e); }
    if (fileRef.current) fileRef.current.value = "";
  };

  const edit = async (name: string) => {
    setErr(undefined); setMsg(undefined);
    try {
      const sk = await api.skill(project, name);
      setDraft({ name: sk.name, description: sk.description, body: sk.body, editing: sk.name });
    } catch (e) { fail(e); }
  };

  const toggle = async (name: string) => {
    if (open === name) { setOpen(undefined); return; }
    setOpen(name); setOpenBody(undefined);
    try { setOpenBody((await api.skill(project, name)).body); } catch (e) { fail(e); }
  };

  const save = async () => {
    if (!draft) return;
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      if (draft.editing) {
        await api.updateSkill(project, draft.editing, { description: draft.description, body: draft.body });
      } else {
        await api.createSkill(project, { name: draft.name.trim(), description: draft.description, body: draft.body });
      }
      if (open === (draft.editing ?? draft.name.trim())) setOpenBody(draft.body);
      setDraft(undefined);
      reload();
    } catch (e) { fail(e); }
    setBusy(false);
  };

  const remove = async (name: string) => {
    setConfirmDel(undefined); setErr(undefined); setMsg(undefined);
    try {
      await api.deleteSkill(project, name);
      if (open === name) setOpen(undefined);
      reload();
    } catch (e) { fail(e); }
  };

  const nameBad = !!draft && !draft.editing && draft.name.trim() !== "" && !NAME_RE.test(draft.name.trim());
  const descBad = !!draft && (draft.description.length > 1024 || /\n\s*\n/.test(draft.description));
  const bodyBytes = draft ? new TextEncoder().encode(draft.body).length : 0;
  const canSave = !!draft && !busy && !nameBad && !descBad && bodyBytes <= 65536
    && (!!draft.editing || NAME_RE.test(draft.name.trim()))
    && draft.description.trim() !== "" && draft.body.trim() !== "";

  return (
    <>
      <div className="pagehead">
        <div>
          <h2 style={{ margin: 0 }}>Skills</h2>
          <p className="help" style={{ margin: "4px 0 0", whiteSpace: "normal" }}>
            Every agent in this project sees each skill's name and description, and loads the full
            text only when a task matches it.
          </p>
        </div>
        {!draft && (
          <span className="btnrow">
            <button type="button" onClick={() => fileRef.current?.click()}>Upload SKILL.md</button>
            <button type="button" className="primary"
                    onClick={() => { setErr(undefined); setMsg(undefined); setDraft({ name: "", description: "", body: "" }); }}>
              New skill
            </button>
          </span>
        )}
        <input ref={fileRef} type="file" accept=".md,text/markdown,text/plain" style={{ display: "none" }}
               aria-label="SKILL.md file" onChange={(e) => upload(e.target.files?.[0])} />
      </div>

      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help" style={{ margin: "0 0 10px" }}>{msg}</p>}

      {draft && (
        <div className="panel" style={{ marginBottom: 16 }}>
          <h3 style={{ margin: "0 0 10px" }}>{draft.editing ? <>Edit <span className="mono">{draft.editing}</span></> : "New skill"}</h3>
          {!draft.editing && (
            <label className={`field${nameBad ? " invalid" : ""}`} style={{ maxWidth: 420 }}>
              <span className="lbl">name</span>
              <input type="text" className="mono" autoFocus placeholder="checkout-triage"
                     value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              <span className="help">lowercase letters, digits and dashes; the agent loads the skill by this name</span>
            </label>
          )}
          <label className={`field${descBad ? " invalid" : ""}`}>
            <span className="lbl">description</span>
            <textarea rows={2} value={draft.description} placeholder="How to triage a checkout incident. Use when the checkout service returns errors."
                      onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
            <span className="help">
              one paragraph that says what the skill is for and when to use it; the agent decides
              from this alone whether to load it · {draft.description.length}/1024
            </span>
          </label>
          <div className="skill-editor">
            <label className="field" style={{ margin: 0 }}>
              <span className="lbl">instructions</span>
              <textarea className="code" rows={16} value={draft.body} placeholder={"# Checkout triage\n\n1. Read the last hour of checkout logs.\n2. ..."}
                        onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
              <span className="help" style={bodyBytes > 65536 ? { color: "var(--err)" } : undefined}>
                markdown · {formatBytes(bodyBytes)} of 64 KiB
              </span>
            </label>
            <div className="field" style={{ margin: 0 }}>
              <span className="lbl">preview</span>
              <div className="skill-preview md">
                {draft.body.trim()
                  ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.body}</ReactMarkdown>
                  : <span className="dim">the instructions, as the agent reads them</span>}
              </div>
            </div>
          </div>
          <div className="btnrow" style={{ marginTop: 12 }}>
            <button className="primary" disabled={!canSave} onClick={save}>
              {busy ? "saving…" : draft.editing ? "Save" : "Add skill"}
            </button>
            <button type="button" onClick={() => setDraft(undefined)}>Cancel</button>
          </div>
        </div>
      )}

      {error && <ErrorState error={error} what="this project's skills" onRetry={reload} />}
      {!data && !error && <div className="dim">loading…</div>}
      {data && (data.length ? (
        <table>
          <thead>
            <tr>
              <th>name</th><th>description</th><th>loaded in the last 7 days by</th>
              <th className="num">size</th><th>updated</th><th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {data.map((sk) => (
              <SkillRow key={sk.name} sk={sk} open={open === sk.name} body={openBody}
                        onToggle={() => toggle(sk.name)} onEdit={() => edit(sk.name)}
                        onDelete={() => setConfirmDel(sk.name)} onOpenAgent={onOpenAgent} />
            ))}
          </tbody>
        </table>
      ) : !draft && (
        <div className="empty">
          No skills yet. Add a playbook, a runbook or a house style as a skill, and every agent in
          this project can load it when a task calls for it.
        </div>
      ))}

      {confirmDel && (
        <ConfirmDialog title={`Delete skill ${confirmDel}?`} danger confirmLabel="Delete"
                       message="The project's agents stop seeing it from their next run. Runs that loaded it keep the record."
                       onConfirm={() => remove(confirmDel)} onCancel={() => setConfirmDel(undefined)} />
      )}
    </>
  );
}

function SkillRow({ sk, open, body, onToggle, onEdit, onDelete, onOpenAgent }: {
  sk: SkillSummary; open: boolean; body: string | undefined;
  onToggle: () => void; onEdit: () => void; onDelete: () => void; onOpenAgent: (name: string) => void;
}) {
  return (
    <>
      <tr className="clickable" onClick={onToggle}>
        <td className="mono" style={{ whiteSpace: "nowrap", verticalAlign: "top" }}>
          <span className="dim">{open ? "▾" : "▸"}</span> {sk.name}
        </td>
        <td style={{ whiteSpace: "normal", verticalAlign: "top" }}>{sk.description}</td>
        <td style={{ verticalAlign: "top" }} onClick={(e) => e.stopPropagation()}>
          {sk.loaded_by.length
            ? sk.loaded_by.map((a) => (
                <a key={a} href="#agents" className="chip mono" style={{ marginRight: 4 }}
                   title="the agent, on the Agents tab"
                   onClick={(e) => { e.preventDefault(); onOpenAgent(a); }}>{a}</a>))
            : <span className="dim">no agent</span>}
        </td>
        <td className="num" style={{ verticalAlign: "top" }}>{formatBytes(sk.size)}</td>
        <td style={{ whiteSpace: "nowrap", verticalAlign: "top" }}><TimeAgo ts={sk.updated_at} /></td>
        <td style={{ verticalAlign: "top" }} onClick={(e) => e.stopPropagation()}>
          <div className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
            <button onClick={onEdit}>Edit</button>
            <button className="danger" onClick={onDelete}>Delete</button>
          </div>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6} style={{ background: "var(--wash, transparent)" }}>
            <div className="md" style={{ padding: "8px 4px" }}>
              {body === undefined
                ? <span className="dim">loading…</span>
                : <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
