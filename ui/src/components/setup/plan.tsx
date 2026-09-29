import { useRef, useState } from "react";

import { api } from "../../api";
import { SkillEditor } from "../SkillsPanel";
import type { Plan, PlanKnob, PlanSkill, PlanTool, PlanWake } from "../../types";
import { NumberStepper, Switch, errText, parseSkillMd } from "./common";

// Step 2: the whole plan on one screen, in plain words. Numbers change in place, switches turn
// optional parts on or off, skills are edited here, and anything else is said in plain words
// (adjust). The machinery Tares fills in stays behind "Show what Tares sets up for you".
// Nothing is created until "Looks right, set it up".

/** The knob values the plan's sentences were written with, per wake and knob. */
export type Baseline = Record<string, number>;
export const baselineOf = (p: Plan): Baseline =>
  Object.fromEntries(p.wakes.flatMap((w) => w.knobs.map((k) => [`${w.key}.${k.id}`, k.value])));

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The sentence cut around its numbers, in knob order, so the steppers sit where the numbers were
 *  ("more than [5] errors in [5] minutes"). null when a number can't be found in order; the
 *  steppers then go on their own lines under the sentence. */
function splitAtKnobs(w: PlanWake, base: Baseline): (string | PlanKnob)[] | null {
  const out: (string | PlanKnob)[] = [];
  let rest = w.sentence;
  for (const k of w.knobs) {
    const v = base[`${w.key}.${k.id}`];
    if (v === undefined) return null;
    const m = new RegExp(`(^|[^\\d.,])(${esc(String(v))})(?![\\d.,]\\d|\\d)`).exec(rest);
    if (!m) return null;
    const at = m.index + m[1].length;
    out.push(rest.slice(0, at), k);
    rest = rest.slice(at + String(v).length);
  }
  out.push(rest);
  return out;
}

function WakeItem({ w, base, onKnob }: { w: PlanWake; base: Baseline; onKnob: (id: string, v: number) => void }) {
  const parts = w.knobs.length ? splitAtKnobs(w, base) : null;
  const stepper = (k: PlanKnob) => (
    <NumberStepper key={k.id} value={k.value} min={k.min} max={k.max} label={k.label}
                   onChange={(v) => onKnob(k.id, v)} />
  );
  if (parts) {
    return (
      <p className="su-wake">
        {parts.map((x, i) => (typeof x === "string" ? <span key={i}>{x}</span> : stepper(x)))}
      </p>
    );
  }
  return (
    <div className="su-item">
      <span className="su-item-what">{w.sentence}</span>
      {w.knobs.map((k) => (
        <span className="su-knob-row" key={k.id}>{stepper(k)} <span>{k.label}</span></span>
      ))}
    </div>
  );
}

/** "check recent deploys" -> "To check recent deploys." */
function whyText(why: string): string {
  const w = why.trim().replace(/^to\s+/i, "").replace(/\.$/, "");
  return w ? `To ${w[0].toLowerCase()}${w.slice(1)}.` : "";
}

let seq = 0;
const newKey = (prefix: string, taken: string[]) => {
  let k = "";
  do { seq += 1; k = `${prefix}${Date.now().toString(36)}${seq}`; } while (taken.includes(k));
  return k;
};

/** The technical list: every object the plan creates, by name, for someone who wants to check. */
function Machinery({ plan }: { plan: Plan }) {
  const m = (s: string) => <span className="mono">{s}</span>;
  const tools = plan.who === "tares" ? plan.tools.filter((t) => t.enabled) : [];
  const skills = plan.skills.filter((s) => s.enabled);
  const agents = plan.who === "tares" ? plan.agents.filter((a) => a.enabled) : [];
  return (
    <ul className="su-machinery">
      {plan.watches.map((w) => (
        <li key={w.key}>
          {w.existing ? <>The existing source {m(w.name)}</> : <>A new {m(w.connector)} source {m(w.name)}</>}
          {!w.existing && w.config && Object.keys(w.config).length > 0 && (
            <> with {Object.keys(w.config).map((k, i) => <span key={k}>{i > 0 && ", "}{m(k)}</span>)}</>
          )}
        </li>
      ))}
      {plan.wakes.map((w) => (
        <li key={w.key}>
          A trigger {m(w.name)} on {w.sources.map((s, i) => <span key={s}>{i > 0 && ", "}{m(s)}</span>)}
          {w.key_field && <>, counted per {m(w.key_field)}</>}
          {w.filters.length > 0 && <>, {w.filters.length} {w.filters.length === 1 ? "filter" : "filters"}</>}
          {w.window && <>, window {m(w.window)}</>}
          {w.cooldown && <>, quiet for {m(w.cooldown)} after it fires</>}
        </li>
      ))}
      {agents.map((a) => (
        <li key={a.key}>
          A Tares agent {m(a.name)}{" "}
          {a.on_trigger && a.trigger ? <>woken by {m(a.trigger)}</> : <>started only by a handoff</>}
          {a.model && <>, model {m(a.model)}</>}
          {a.handoffs.map((h) => <span key={h.verdict + h.agent}>, hands off to {m(h.agent)} on the verdict {m(h.verdict)}</span>)}
          {a.mcp_servers.length > 0 && <>, may use {a.mcp_servers.map((s, i) => <span key={s}>{i > 0 && ", "}{m(s)}</span>)}</>}
        </li>
      ))}
      {plan.who === "own" && plan.own_agent && (
        <li>A project key {m(plan.own_agent.name)} that reads this project and records findings in it</li>
      )}
      {tools.map((t) => <li key={t.key}>An MCP server {m(t.name)}{t.url && <> at {m(t.url)}</>}</li>)}
      {skills.map((s) => <li key={s.key}>A skill {m(s.name)}</li>)}
      <li>Everything stays editable later under Setup, Advanced setup</li>
    </ul>
  );
}

export function PlanStep({ plan, setPlan, base, setBase, onBack, onApplied }: {
  plan: Plan; setPlan: (p: Plan) => void;
  base: Baseline; setBase: (b: Baseline) => void;
  onBack: () => void;
  onApplied: (r: Awaited<ReturnType<typeof api.applySetup>>) => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [adjusting, setAdjusting] = useState(false);
  const [adjustErr, setAdjustErr] = useState<string>();
  const [details, setDetails] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyErr, setApplyErr] = useState<string>();
  const [editing, setEditing] = useState<PlanSkill | "new">();
  const [skillMsg, setSkillMsg] = useState<string>();
  const [skillErr, setSkillErr] = useState<string>();
  const [addingTool, setAddingTool] = useState(false);
  const [toolName, setToolName] = useState("");
  const [toolUrl, setToolUrl] = useState("");
  const [toolErr, setToolErr] = useState<string>();
  const fileRef = useRef<HTMLInputElement>(null);
  const busy = adjusting || applying;
  const tares = plan.who === "tares";

  const set = (patch: Partial<Plan>) => setPlan({ ...plan, ...patch });
  const setKnob = (wake: string, id: string, v: number) => set({
    wakes: plan.wakes.map((w) => (w.key !== wake ? w
      : { ...w, knobs: w.knobs.map((k) => (k.id === id ? { ...k, value: v } : k)) })),
  });
  const setAgent = (key: string, enabled: boolean) =>
    set({ agents: plan.agents.map((a) => (a.key === key ? { ...a, enabled } : a)) });
  const setTool = (key: string, patch: Partial<PlanTool>) =>
    set({ tools: plan.tools.map((t) => (t.key === key ? { ...t, ...patch } : t)) });
  const setSkill = (key: string, patch: Partial<PlanSkill>) =>
    set({ skills: plan.skills.map((s) => (s.key === key ? { ...s, ...patch } : s)) });

  /** A skill in, by name: replaces the one with that name, else joins the list. */
  const putSkill = (sk: { name: string; description: string; body: string }, replacing?: string) => {
    const hit = plan.skills.find((s) => s.key === replacing) ?? plan.skills.find((s) => s.name === sk.name);
    if (replacing && plan.skills.some((s) => s.name === sk.name && s.key !== replacing)) {
      throw new Error(`There is already a skill called ${sk.name}.`);
    }
    if (hit) {
      set({ skills: plan.skills.map((s) => (s.key === hit.key ? { ...s, ...sk, enabled: true } : s)) });
      return false;
    }
    set({ skills: [...plan.skills, { key: newKey("s", plan.skills.map((s) => s.key)), ...sk, enabled: true }] });
    return true;
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setSkillErr(undefined); setSkillMsg(undefined);
    try {
      const sk = parseSkillMd(await file.text());
      const added = putSkill(sk);
      setSkillMsg(`${added ? "Added" : "Replaced"} ${sk.name} from ${file.name}.`);
    } catch (e) { setSkillErr(`Could not read ${file.name}: ${errText(e)}`); }
    if (fileRef.current) fileRef.current.value = "";
  };

  const addTool = () => {
    const name = toolName.trim();
    const url = toolUrl.trim();
    if (!name || !url) return;
    if (plan.tools.some((t) => t.name === name)) { setToolErr(`There is already a tool called ${name}.`); return; }
    // a tool the person adds is offered to every agent in the plan
    set({
      tools: [...plan.tools, { key: newKey("t", plan.tools.map((t) => t.key)), name, url, why: "", can_act: false, enabled: true }],
      agents: plan.agents.map((a) => (a.mcp_servers.includes(name) ? a : { ...a, mcp_servers: [...a.mcp_servers, name] })),
    });
    setToolName(""); setToolUrl(""); setAddingTool(false); setToolErr(undefined);
  };

  const adjust = async () => {
    const text = instruction.trim();
    if (!text || busy) return;
    setAdjusting(true); setAdjustErr(undefined);
    try {
      const r = await api.adjustSetup(plan, text);
      setPlan(r.plan); setBase(baselineOf(r.plan)); setInstruction("");
    } catch (e) { setAdjustErr(errText(e)); }
    setAdjusting(false);
  };

  const apply = async () => {
    if (busy) return;
    setApplying(true); setApplyErr(undefined);
    try { onApplied(await api.applySetup({ ...plan, name: plan.name.trim() || plan.goal })); }
    catch (e) { setApplyErr(errText(e)); setApplying(false); }
  };

  const toolLabel = (name: string) => {
    const t = plan.tools.find((x) => x.name === name);
    return t && !t.enabled ? `${name} (once you turn it on below)` : name;
  };

  return (
    <section className="su-section" aria-labelledby="su-plan-h">
      <div className="su-head su-narrow">
        <h1 id="su-plan-h" className="su-title">Here is the plan</h1>
        <p className="su-goal-line">
          {plan.goal}{" "}
          <button type="button" className="linklike su-small" onClick={onBack} disabled={busy}>Change the goal</button>
        </p>
        {plan.summary && <p>{plan.summary}</p>}
        <p className="su-sub">Nothing runs until you say so. Adjust any number here, or say what to change below.</p>
      </div>

      <label className="field su-name">
        <span className="lbl">Project name</span>
        <input type="text" value={plan.name} maxLength={80} disabled={busy}
               onChange={(e) => set({ name: e.target.value })} />
      </label>

      <ol className="su-cards">
        <li className="su-card">
          <h2 className="su-card-n">1. Watches</h2>
          {plan.watches.length === 0 && <p className="help">Nothing to watch. Say what data to use below.</p>}
          {plan.watches.map((w) => (
            <div className="su-item" key={w.key}>
              <span className="su-item-what">{w.sentence}</span>
              <span className="help">
                {w.existing ? "Already connected to Tares."
                  : w.needs === "send" ? "You connect it in the next step."
                  : w.needs === "credential" ? "Needs a credential; you add it in the next step."
                  : "Tares connects it for you."}
              </span>
            </div>
          ))}
        </li>

        <li className="su-card">
          <h2 className="su-card-n">2. Wakes when</h2>
          {plan.wakes.length === 0 && <p className="help">Nothing wakes the agents yet. Say when they should look below.</p>}
          {plan.wakes.map((w) => (
            <WakeItem key={w.key} w={w} base={base} onKnob={(id, v) => setKnob(w.key, id, v)} />
          ))}
        </li>

        <li className="su-card">
          <h2 className="su-card-n">3. {tares ? "Agents" : "Your agent"}</h2>
          {tares && plan.agents.length === 0 && <p className="help">No agent yet. Say what the agents should do below.</p>}
          {tares && plan.agents.map((a) => (
            <div className="su-item" key={a.key}>
              {a.optional
                ? <Switch checked={a.enabled} onChange={(on) => setAgent(a.key, on)} disabled={busy}>
                    <span className="su-item-what">{a.sentence}</span>
                  </Switch>
                : <span className="su-item-what">{a.sentence}</span>}
              {a.optional && !a.enabled && <span className="help">Off: it will not be set up.</span>}
              {a.mcp_servers.length > 0 && (
                <span className="help">Can use {a.mcp_servers.map(toolLabel).join(", ")}.</span>
              )}
            </div>
          ))}
          {!tares && plan.own_agent && (
            <div className="su-item">
              <span className="su-item-what">{plan.own_agent.sentence}</span>
              <span className="help">
                It joins with a project key you get in the next step, then is{" "}
                {plan.own_agent.wake === "webhook" ? "woken at its webhook" : "told when it next checks in"}.
              </span>
            </div>
          )}
        </li>

        <li className="su-card">
          <h2 className="su-card-n">4. Know-how</h2>
          {plan.skills.length === 0 && (
            <p className="help">Optional. Notes the agents follow, like what your error codes mean. You can add them any time.</p>
          )}
          {plan.skills.map((s) => (
            <div className="su-item" key={s.key}>
              <Switch checked={s.enabled} onChange={(on) => setSkill(s.key, { enabled: on })} disabled={busy}>
                <span className="su-item-what mono">{s.name}</span>
              </Switch>
              {s.description && <span className="help">{s.description}</span>}
              <button type="button" className="linklike su-small" disabled={busy}
                      aria-label={`Edit ${s.name}`}
                      onClick={() => { setSkillErr(undefined); setSkillMsg(undefined); setEditing(s); }}>Edit</button>
            </div>
          ))}
          <div className="su-card-actions">
            <button type="button" disabled={busy}
                    onClick={() => { setSkillErr(undefined); setSkillMsg(undefined); setEditing("new"); }}>Add a skill</button>
            <button type="button" disabled={busy} onClick={() => fileRef.current?.click()}>Upload SKILL.md</button>
            <input ref={fileRef} type="file" accept=".md,text/markdown,text/plain" className="sr-only" tabIndex={-1}
                   aria-label="SKILL.md file" onChange={(e) => upload(e.target.files?.[0])} />
          </div>
          <p className="su-live help" role="status" aria-live="polite">{skillMsg}</p>
          {skillErr && <p className="su-err" role="alert">{skillErr}</p>}
        </li>
      </ol>

      {editing && (
        <SkillEditor key={editing === "new" ? "new" : editing.key}
                     initial={editing === "new" ? undefined : { name: editing.name, description: editing.description, body: editing.body }}
                     renamable
                     onSubmit={(sk) => { putSkill(sk, editing === "new" ? undefined : editing.key); }}
                     onSaved={() => setEditing(undefined)}
                     onCancel={() => setEditing(undefined)} />
      )}

      {tares && (
        <section className="su-tools" aria-labelledby="su-tools-h">
          <h2 id="su-tools-h" className="su-h2">Tools the agents can use</h2>
          <p className="help">Outside services the agents can call, through MCP. Each stays off until you turn it on.</p>
          {plan.tools.length === 0 && !addingTool && <p className="help">None suggested for this goal.</p>}
          {plan.tools.length > 0 && (
            <ul className="su-tool-list">
              {plan.tools.map((t) => (
                <li key={t.key} className="su-tool">
                  <Switch checked={t.enabled} onChange={(on) => setTool(t.key, { enabled: on })} disabled={busy}>
                    <span className="su-item-what mono">{t.name}</span>
                  </Switch>
                  {t.why && <span>{whyText(t.why)}</span>}
                  {t.can_act && <span className="su-warn">It can make changes, not only read. Turn it on only if the agents should act.</span>}
                  {t.enabled && !t.url && <span className="help">You add its address in the next step.</span>}
                  {t.url && <span className="help mono su-break">{t.url}</span>}
                </li>
              ))}
            </ul>
          )}
          {addingTool ? (
            <form className="su-addtool" onSubmit={(e) => { e.preventDefault(); addTool(); }}>
              <label className="field">
                <span className="lbl">Name</span>
                <input type="text" autoFocus value={toolName} placeholder="github" onChange={(e) => setToolName(e.target.value)} />
              </label>
              <label className="field">
                <span className="lbl">Address</span>
                <input type="url" className="mono" value={toolUrl} placeholder="https://mcp.example.com/mcp"
                       onChange={(e) => setToolUrl(e.target.value)} />
                <span className="help">An MCP server over HTTP. Every agent in the plan can use it.</span>
              </label>
              {toolErr && <p className="su-err" role="alert">{toolErr}</p>}
              <div className="btnrow">
                <button type="submit" disabled={!toolName.trim() || !toolUrl.trim()}>Add the tool</button>
                <button type="button" onClick={() => setAddingTool(false)}>Cancel</button>
              </div>
            </form>
          ) : (
            <div><button type="button" disabled={busy} onClick={() => setAddingTool(true)}>Add a tool</button></div>
          )}
        </section>
      )}

      {plan.notes.length > 0 && (
        <ul className="su-notes" aria-label="Good to know">
          {plan.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      <form className="su-adjust" onSubmit={(e) => { e.preventDefault(); adjust(); }}>
        <label htmlFor="su-adjust-in" className="sr-only">Say what to change</label>
        <input id="su-adjust-in" type="text" value={instruction} disabled={busy}
               placeholder="Say what to change, for example: only for payments-api"
               onChange={(e) => setInstruction(e.target.value)} />
        <button type="submit" disabled={busy || !instruction.trim()}>
          {adjusting ? "Updating the plan…" : "Update the plan"}
        </button>
      </form>
      <p className="su-live" role="status" aria-live="polite">{adjusting && "Updating the plan…"}</p>
      {adjustErr && <div className="alert error" role="alert">{adjustErr}</div>}

      <div className="su-details">
        <button type="button" className="linklike su-small" aria-expanded={details} aria-controls="su-machinery"
                onClick={() => setDetails(!details)}>
          {details ? "Hide what Tares sets up for you" : "Show what Tares sets up for you"}
        </button>
        {details && <div id="su-machinery"><Machinery plan={plan} /></div>}
      </div>

      {applyErr && <div className="alert error" role="alert">{applyErr}</div>}
      <div className="btnrow">
        <button type="button" className="primary su-cta" onClick={apply} disabled={busy || !!editing}>
          {applying ? "Setting it up…" : "Looks right, set it up"}
        </button>
        <button type="button" className="su-cta" onClick={onBack} disabled={busy}>Back</button>
      </div>
      {editing && <p className="help">Save or cancel the skill you are editing first.</p>}
      <p className="su-live" role="status" aria-live="polite">{applying && "Setting it up…"}</p>
    </section>
  );
}
