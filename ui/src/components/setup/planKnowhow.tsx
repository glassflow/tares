import { useEffect, useRef, useState } from "react";

import { api } from "../../api";
import { Picker } from "../bits";
import { SkillEditor } from "../SkillsPanel";
import type { Plan, PlanTool, SkillSummary } from "../../types";
import { Switch, errText, parseSkillMd } from "./common";
import { EditorFrame, ItemActions, Problems } from "./planBits";
import { newKey } from "./planEdit";
import type { CardCtx } from "./plan";

// Card 4, Know-how: the skills the agents follow, each switched on or off, edited, removed;
// added by hand, from a SKILL.md, or copied from another project. Below the cards, the tools
// (MCP servers) the agents can call: suggested ones switched on, one already on Tares attached,
// or a new one added by address.

type Sk = { name: string; description: string; body: string };

/** A skill in, by name: replaces the one being edited (or one of that name), else joins. Returns
 *  the plan, and whether it was added. */
function putSkill(plan: Plan, sk: Sk, replacing?: string): [Plan, boolean] {
  if (replacing && plan.skills.some((s) => s.name === sk.name && s.key !== replacing)) {
    throw new Error(`There is already a skill called ${sk.name}.`);
  }
  const hit = plan.skills.find((s) => s.key === replacing) ?? plan.skills.find((s) => s.name === sk.name);
  if (hit) return [{ ...plan, skills: plan.skills.map((s) => (s.key === hit.key ? { ...s, ...sk, enabled: true } : s)) }, false];
  return [{ ...plan, skills: [...plan.skills, { key: newKey("s", plan.skills.map((s) => s.key)), ...sk, enabled: true }] }, true];
}

export function KnowHowCard({ ctx }: { ctx: CardCtx }) {
  const { plan, edit, probs, open, openEditor, closeEditor, busy } = ctx;
  const [msg, setMsg] = useState<string>();
  const [err, setErr] = useState<string>();
  const fileRef = useRef<HTMLInputElement>(null);
  const editingHere = !!open?.startsWith("skills.");

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setErr(undefined); setMsg(undefined);
    try {
      const sk = parseSkillMd(await file.text());
      const [next, added] = putSkill(plan, sk);
      edit(next);
      setMsg(`${added ? "Added" : "Replaced"} ${sk.name} from ${file.name}.`);
    } catch (e) { setErr(`Could not read ${file.name}: ${errText(e)}`); }
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <li className={`su-card${editingHere ? " editing" : ""}`}>
      <h2 className="su-card-n">4. Know-how</h2>
      {plan.skills.length === 0 && (
        <p className="help">Optional. Notes the agents follow, like what your error codes mean. You can add them any time.</p>
      )}
      {plan.skills.map((s) => {
        const id = `skills.${s.key}`;
        const editId = `su-edit-${id}`;
        if (open === id) {
          return (
            <SkillEditor key={s.key} initial={{ name: s.name, description: s.description, body: s.body }} renamable
                         onSubmit={(sk) => { edit(putSkill(plan, sk, s.key)[0]); }}
                         onSaved={() => closeEditor(editId)} onCancel={() => closeEditor(editId)} />
          );
        }
        return (
          <div className="su-item" key={s.key}>
            <Switch checked={s.enabled} disabled={busy}
                    onChange={(on) => edit({ ...plan, skills: plan.skills.map((x) => (x.key === s.key ? { ...x, enabled: on } : x)) })}>
              <span className="su-item-what mono">{s.name}</span>
            </Switch>
            {s.description && <span className="help">{s.description}</span>}
            <ItemActions id={editId} what={s.name} changeLabel="Edit" disabled={busy || !!open}
                         onChange={() => { setMsg(undefined); setErr(undefined); openEditor(id); }}
                         onRemove={() => { edit({ ...plan, skills: plan.skills.filter((x) => x.key !== s.key) }); closeEditor("su-add-skills"); }} />
            <Problems list={probs[id]} />
          </div>
        );
      })}
      {open === "skills.new" && (
        <SkillEditor onSubmit={(sk) => { edit(putSkill(plan, sk)[0]); }}
                     onSaved={() => closeEditor("su-add-skills")} onCancel={() => closeEditor("su-add-skills")} />
      )}
      {open === "skills.copy" && (
        <CopySkill ctx={ctx} onCopy={(sk) => {
          const [next, added] = putSkill(plan, sk);
          edit(next);
          setMsg(`${added ? "Copied" : "Replaced"} ${sk.name}.`);
        }} onDone={() => closeEditor("su-copy-skills")} />
      )}
      {!editingHere && (
        <div className="su-card-actions">
          <button type="button" id="su-add-skills" disabled={busy || !!open}
                  onClick={() => { setMsg(undefined); setErr(undefined); openEditor("skills.new"); }}>Add a skill</button>
          <button type="button" disabled={busy || !!open} onClick={() => fileRef.current?.click()}>Upload SKILL.md</button>
          <button type="button" id="su-copy-skills" disabled={busy || !!open}
                  onClick={() => { setMsg(undefined); setErr(undefined); openEditor("skills.copy"); }}>Copy from another project</button>
        </div>
      )}
      <input ref={fileRef} type="file" accept=".md,text/markdown,text/plain" className="sr-only" tabIndex={-1}
             aria-label="SKILL.md file" onChange={(e) => upload(e.target.files?.[0])} />
      <p className="su-live help" role="status" aria-live="polite">{msg}</p>
      {err && <p className="su-err" role="alert">{err}</p>}
    </li>
  );
}

/** Pick a project, then copy any of its skills into the plan (name, description and body). */
function CopySkill({ ctx, onCopy, onDone }: { ctx: CardCtx; onCopy: (sk: Sk) => void; onDone: () => void }) {
  const projects = ctx.cell.projects;
  const [pid, setPid] = useState("");
  const [skills, setSkills] = useState<SkillSummary[]>();
  const [err, setErr] = useState<string>();
  const [copying, setCopying] = useState<string>();
  useEffect(() => {
    if (!pid) { setSkills(undefined); return; }
    let live = true;
    setSkills(undefined); setErr(undefined);
    api.skills(pid).then((s) => { if (live) setSkills(s); })
      .catch((e) => { if (live) { setSkills([]); setErr(errText(e)); } });
    return () => { live = false; };
  }, [pid]);
  const copy = async (name: string) => {
    setCopying(name); setErr(undefined);
    try {
      const sk = await api.skill(pid, name);
      onCopy({ name: sk.name, description: sk.description, body: sk.body });
    } catch (e) { setErr(errText(e)); }
    setCopying(undefined);
  };
  const labels: Record<string, string> = { "": "pick a project" };
  for (const p of projects ?? []) labels[p.id] = p.name;
  const taken = new Set(ctx.plan.skills.map((s) => s.name));
  return (
    <EditorFrame title="Copy a skill from another project" onCancel={onDone}>
      {projects === undefined ? <p className="help">Reading the projects…</p>
        : projects.length === 0 ? <p className="help">There are no other projects yet.</p> : (
          <div className="field">
            <span className="lbl">Project</span>
            <Picker value={pid} options={["", ...projects.map((p) => p.id)]} labels={labels} ariaLabel="Project" onChange={setPid} />
          </div>
        )}
      {pid && skills === undefined && <p className="help">Reading its skills…</p>}
      {pid && skills?.length === 0 && !err && <p className="help">This project has no skills.</p>}
      {skills && skills.length > 0 && (
        <ul className="su-copy-list">
          {skills.map((s) => (
            <li key={s.name}>
              <span className="su-item-what mono">{s.name}</span>
              {s.description && <span className="help">{s.description}</span>}
              <button type="button" className="su-small" disabled={!!copying} aria-label={`Copy ${s.name}`}
                      onClick={() => copy(s.name)}>
                {copying === s.name ? "Copying…" : taken.has(s.name) ? "Copy again" : "Copy"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="su-err" role="alert">{err}</p>}
    </EditorFrame>
  );
}

/** "check recent deploys" -> "To check recent deploys." */
/** The reason as a sentence: "check recent deploys" -> "To check recent deploys."; a reason
 *  that is already a sentence ("Knowing which deploys went out helps") is kept as it is. */
function whyText(why: string): string {
  const w = why.trim().replace(/\.$/, "");
  if (!w) return "";
  if (/^to\s/i.test(w)) return `${w[0].toUpperCase()}${w.slice(1)}.`;
  // a bare verb phrase reads as a purpose; anything else is shown as written
  const verb = /^(check|see|find|look|read|compare|correlate|fetch|get|search|query|open|page|post|list)\b/i.test(w);
  return verb ? `To ${w[0].toLowerCase()}${w.slice(1)}.` : `${w[0].toUpperCase()}${w.slice(1)}.`;
}

export function ToolsSection({ ctx }: { ctx: CardCtx }) {
  const { plan, edit, probs, open, openEditor, closeEditor, busy, cell } = ctx;
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [err, setErr] = useState<string>();
  const setTool = (key: string, patch: Partial<PlanTool>) =>
    edit({ ...plan, tools: plan.tools.map((t) => (t.key === key ? { ...t, ...patch } : t)) });
  // a tool the person adds or attaches is offered to every agent in the plan
  const addTool = (t: PlanTool) => edit({
    ...plan, tools: [...plan.tools, t],
    agents: plan.agents.map((a) => (a.mcp_servers.includes(t.name) ? a : { ...a, mcp_servers: [...a.mcp_servers, t.name] })),
  });
  const removeTool = (t: PlanTool) => edit({
    ...plan, tools: plan.tools.filter((x) => x.key !== t.key),
    agents: plan.agents.map((a) => ({ ...a, mcp_servers: a.mcp_servers.filter((n) => n !== t.name) })),
  });
  const addNew = () => {
    const n = name.trim(), u = url.trim();
    if (!n || !u) return;
    if (plan.tools.some((t) => t.name === n)) { setErr(`There is already a tool called ${n}.`); return; }
    const key = newKey("t", plan.tools.map((t) => t.key));
    addTool({ key, name: n, url: u, why: "", can_act: false, enabled: true, existing: false });
    setName(""); setUrl(""); setErr(undefined);
    closeEditor(`su-edit-tools.${key}`);
  };
  const inPlan = new Set(plan.tools.map((t) => t.name));
  const attachable = (cell.servers ?? []).filter((m) => !inPlan.has(m.name));

  return (
    <section className="su-tools" aria-labelledby="su-tools-h">
      <h2 id="su-tools-h" className="su-h2">Tools the agents can use</h2>
      <p className="help">Outside services the agents can call, through MCP. Each stays off until you turn it on.</p>
      {plan.tools.length === 0 && <p className="help">None in the plan.</p>}
      {plan.tools.length > 0 && (
        <ul className="su-tool-list">
          {plan.tools.map((t) => (
            <li key={t.key} className="su-tool">
              <Switch checked={t.enabled} onChange={(on) => setTool(t.key, { enabled: on })} disabled={busy}>
                <span className="su-item-what mono">{t.name}</span>
              </Switch>
              {t.existing && <span className="help">Already on Tares, used as it is.</span>}
              {t.why && <span>{whyText(t.why)}</span>}
              {t.can_act && <span className="su-warn">It can make changes, not only read. Turn it on only if the agents should act.</span>}
              {t.enabled && !t.url && <span className="help">You add its address in the next step.</span>}
              {t.url && <span className="help mono su-break">{t.url}</span>}
              <ItemActions id={`su-edit-tools.${t.key}`} what={t.name} changeLabel="Remove" disabled={busy || !!open}
                           onChange={() => { removeTool(t); closeEditor("su-attach-tools"); }} />
              <Problems list={probs[`tools.${t.key}`]} />
            </li>
          ))}
        </ul>
      )}
      {open === "tools.attach" && (
        <div className="su-addtool">
          <EditorFrame title="Attach a tool already on Tares" onCancel={() => closeEditor("su-attach-tools")}>
            {cell.servers === undefined ? <p className="help">Reading the tools on Tares…</p>
              : attachable.length === 0 ? <p className="help">Every tool on Tares is in the plan already, or there is none yet.</p> : (
                <ul className="su-copy-list">
                  {attachable.map((m) => (
                    <li key={m.name}>
                      <span className="su-item-what mono">{m.name}</span>
                      <span className="help mono su-break">{m.url}</span>
                      <button type="button" className="su-small" aria-label={`Attach ${m.name}`}
                              onClick={() => {
                                const key = newKey("t", plan.tools.map((t) => t.key));
                                addTool({ key, name: m.name, url: m.url, why: "", can_act: false, enabled: true, existing: true });
                                closeEditor(`su-edit-tools.${key}`);
                              }}>Attach</button>
                    </li>
                  ))}
                </ul>
              )}
          </EditorFrame>
        </div>
      )}
      {open === "tools.new" && (
        <form className="su-addtool" onSubmit={(e) => { e.preventDefault(); addNew(); }}
              onKeyDown={(e) => { if (e.key === "Escape") closeEditor("su-add-tools"); }}>
          <label className="field">
            <span className="lbl">Name</span>
            <input type="text" autoFocus value={name} placeholder="github" onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="field">
            <span className="lbl">Address</span>
            <input type="url" className="mono" value={url} placeholder="https://mcp.example.com/mcp"
                   onChange={(e) => setUrl(e.target.value)} />
            <span className="help">An MCP server over HTTP. Every agent in the plan can use it.</span>
          </label>
          {err && <p className="su-err" role="alert">{err}</p>}
          <div className="btnrow">
            <button type="submit" disabled={!name.trim() || !url.trim()}>Add the tool</button>
            <button type="button" onClick={() => closeEditor("su-add-tools")}>Cancel</button>
          </div>
        </form>
      )}
      {open !== "tools.new" && open !== "tools.attach" && (
        <div className="btnrow">
          <button type="button" id="su-attach-tools" disabled={busy || !!open} onClick={() => openEditor("tools.attach")}>
            Attach one already on Tares
          </button>
          <button type="button" id="su-add-tools" disabled={busy || !!open} onClick={() => openEditor("tools.new")}>
            Add a new one
          </button>
        </div>
      )}
    </section>
  );
}
