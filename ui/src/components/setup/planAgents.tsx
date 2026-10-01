import { useState } from "react";
import { Link } from "react-router-dom";

import type { SlackChannels } from "../../api";
import { Picker } from "../bits";
import type { PlanAgent, PlanOwnAgent } from "../../types";
import { Switch } from "./common";
import { Choice, EditorFrame, ItemActions, Problems } from "./planBits";
import { kebab, newKey, renameAgent, uniqueName } from "./planEdit";
import type { CardCtx } from "./plan";

// Card 3, who does the work: Tares agents or the person's own agent, switched here too. A Tares
// agent opens into its name, when it runs (on a wake-up, or only when another hands it over), its
// instructions, the model, the tools it may use and whom it hands over to; "Add an agent" starts
// from the agent presets. The person's own agent opens into its name and how it hears.

const VERDICT_RE = /^[a-z0-9][a-z0-9_-]*$/;

const minutesOf = (d: string | null | undefined, dflt: number) => {
  const m = /^(\d+(?:\.\d+)?)([smhd])$/.exec(String(d ?? "").trim());
  if (!m) return dflt;
  return Math.max(1, Math.ceil(Number(m[1]) * { s: 1 / 60, m: 1, h: 60, d: 1440 }[m[2] as "s" | "m" | "h" | "d"]));
};

export function AgentsCard({ ctx }: { ctx: CardCtx }) {
  const { plan, edit, probs, open, openEditor, closeEditor, busy } = ctx;
  const tares = plan.who === "tares";
  const editingHere = !!open?.startsWith("agents.") || open === "own_agent";
  const addId = "su-add-agents";
  const toolLabel = (name: string) => {
    const t = plan.tools.find((x) => x.name === name);
    return t && !t.enabled ? `${name} (once you turn it on under Tools)` : name;
  };
  const setWho = (who: "tares" | "own") => edit({
    ...plan, who,
    own_agent: who === "own" && !plan.own_agent ? { name: "my-agent", wake: "webhook", sentence: "" } : plan.own_agent,
  });

  return (
    <li className={`su-card${editingHere ? " editing" : ""}`}>
      <h2 className="su-card-n">3. {tares ? "Agents" : "Your agent"}</h2>
      <fieldset className="su-who-mini" disabled={busy || !!open}>
        <legend className="sr-only">Who does the work</legend>
        <label className={plan.who === "tares" ? "on" : ""}>
          <input type="radio" name="su-plan-who" checked={tares} onChange={() => setWho("tares")} /> Tares agents
        </label>
        <label className={plan.who === "own" ? "on" : ""}>
          <input type="radio" name="su-plan-who" checked={!tares} onChange={() => setWho("own")} /> Your own agent
        </label>
      </fieldset>
      <Problems list={probs["agents"]} />

      {tares && plan.agents.length === 0 && open !== "agents.new" && <p className="help">No agent yet. Add one to look when the project wakes.</p>}
      {tares && plan.agents.map((a) => {
        const id = `agents.${a.key}`;
        const editId = `su-edit-${id}`;
        if (open === id) {
          return (
            <AgentEditor key={a.key} agent={a} ctx={ctx}
                         onSave={(na) => {
                           const agents = plan.agents.map((x) => (x.key === a.key ? na : x));
                           edit(renameAgent({ ...plan, agents }, a.name, na.name));
                           closeEditor(editId);
                         }}
                         onCancel={() => closeEditor(editId)} />
          );
        }
        return (
          <div className="su-item" key={a.key}>
            {a.optional
              ? <Switch checked={a.enabled} disabled={busy}
                        onChange={(on) => edit({ ...plan, agents: plan.agents.map((x) => (x.key === a.key ? { ...x, enabled: on } : x)) })}>
                  <span className="su-item-what">{a.sentence || a.name}</span>
                </Switch>
              : <span className="su-item-what">{a.sentence || a.name}</span>}
            {a.optional && !a.enabled && <span className="help">Off: it will not be set up.</span>}
            {a.existing && <span className="help">Already on Tares, shared with the projects that use it. This project only says when it runs and whom it hands over to.</span>}
            {a.mcp_servers.length > 0 && <span className="help">Can use {a.mcp_servers.map(toolLabel).join(", ")}.</span>}
            {a.slack && a.enabled && (
              <div className="field su-slack">
                <span className="lbl">Posts to the Slack channel</span>
                <SlackPick slack={ctx.cell.slack} value={a.slack_channel ?? ""} disabled={busy || !!open}
                           onChange={(ch) => edit({ ...plan, agents: plan.agents.map((x) => (x.key === a.key ? { ...x, slack_channel: ch } : x)) })} />
              </div>
            )}
            <ItemActions id={editId} what={a.name} disabled={busy || !!open}
                         onChange={() => openEditor(id)}
                         onRemove={() => { edit({ ...plan, agents: plan.agents.filter((x) => x.key !== a.key) }); closeEditor(addId); }} />
            <Problems list={probs[id]} />
          </div>
        );
      })}
      {tares && (open === "agents.new" ? (
        <AgentEditor ctx={ctx}
                     onSave={(na) => { edit({ ...plan, agents: [...plan.agents, na] }); closeEditor(`su-edit-agents.${na.key}`); }}
                     onCancel={() => closeEditor(addId)} />
      ) : (
        <div className="su-card-actions">
          <button type="button" id={addId} disabled={busy || !!open} onClick={() => openEditor("agents.new")}>Add an agent</button>
        </div>
      ))}

      {!tares && plan.own_agent && (open === "own_agent" ? (
        <OwnAgentEditor own={plan.own_agent}
                        onSave={(own) => { edit({ ...plan, own_agent: own }); closeEditor("su-edit-own_agent"); }}
                        onCancel={() => closeEditor("su-edit-own_agent")} />
      ) : (
        <div className="su-item">
          <span className="su-item-what">{plan.own_agent.sentence || plan.own_agent.name}</span>
          <span className="help">
            It joins with a project key you get in the next step, then is{" "}
            {plan.own_agent.wake === "webhook" ? "woken at its webhook" : "told when it next checks in"}.
          </span>
          <ItemActions id="su-edit-own_agent" what="your agent" disabled={busy || !!open}
                       onChange={() => openEditor("own_agent")} />
          <Problems list={probs["own_agent"]} />
        </div>
      ))}
    </li>
  );
}

/** The Slack channel an agent posts to: the channels the cell's bot is in, or why there are none
 *  (Slack not connected: where to connect it; the list unavailable: type the channel's ID). */
function SlackPick({ slack, value, onChange, disabled }: {
  slack: SlackChannels | undefined; value: string; onChange: (id: string) => void; disabled?: boolean;
}) {
  if (!slack) return <p className="help">Reading your Slack channels…</p>;
  if (slack.reason === "no_token") {
    return (
      <p className="help">
        Slack is not connected to Tares yet. <Link to="/settings">Connect it under Settings, Slack</Link>, then pick the channel here.
      </p>
    );
  }
  if (!slack.channels.length) {
    return (
      <label className="field">
        <span className="help">
          {slack.reason === "missing_scope" ? "Tares cannot list your channels." : "No channel to pick from: invite the Tares bot to a channel, or type its ID."}
        </span>
        <input type="text" value={value} placeholder="C0123456789" disabled={disabled} aria-label="Slack channel ID"
               onChange={(e) => onChange(e.target.value.trim())} />
      </label>
    );
  }
  const options = ["", ...slack.channels.map((c) => c.id)];
  if (value && !options.includes(value)) options.push(value);
  const labels: Record<string, string> = { "": "Pick a channel" };
  for (const c of slack.channels) labels[c.id] = c.is_private ? `${c.name} (private)` : `#${c.name}`;
  return <Picker value={value} options={options} labels={labels} ariaLabel="Slack channel" onChange={onChange} disabled={disabled} />;
}

function OwnAgentEditor({ own, onSave, onCancel }: {
  own: PlanOwnAgent; onSave: (o: PlanOwnAgent) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(own.name);
  const [wake, setWake] = useState(own.wake);
  return (
    <EditorFrame title="Change your agent" onCancel={onCancel} canSave={!!name.trim()}
                 onSave={() => onSave({ ...own, name: name.trim(), wake, sentence: "" })}>
      <label className="field">
        <span className="lbl">Name</span>
        <input type="text" value={name} maxLength={64} placeholder="claude-code" onChange={(e) => setName(e.target.value)} />
        <span className="help">Its project key is called this.</span>
      </label>
      <fieldset className="su-choices">
        <legend className="lbl">How it hears about a wake-up</legend>
        <Choice name="su-own-wake" checked={wake === "webhook"} onChange={() => setWake("webhook")} title="Tares calls its webhook">
          Tares posts what happened to an address your agent listens on.
        </Choice>
        <Choice name="su-own-wake" checked={wake === "poll"} onChange={() => setWake("poll")} title="It checks in itself">
          Your agent reads the project when it runs and records what it finds.
        </Choice>
      </fieldset>
    </EditorFrame>
  );
}

interface HandoffRow { verdict: string; agent: string; minutes: string }

function AgentEditor({ agent, ctx, onSave, onCancel }: {
  agent?: PlanAgent; ctx: CardCtx; onSave: (a: PlanAgent) => void; onCancel: () => void;
}) {
  const { plan, cell } = ctx;
  // agents already on the cell, not in the plan yet: picked from a list, shown one at a time
  const reusable = (cell.resources?.agents ?? []).filter((x) => !plan.agents.some((a) => a.name === x.name));
  const [reuse, setReuse] = useState("");
  const picked = reusable.find((x) => x.name === reuse);
  const [key] = useState(() => agent?.key ?? newKey("a", plan.agents.map((a) => a.key)));
  const [name, setName] = useState(agent?.name ?? "");
  const [onTrigger, setOnTrigger] = useState(agent?.on_trigger ?? true);
  const [trigger, setTrigger] = useState(agent?.trigger ?? plan.wakes[0]?.name ?? "");
  const [prompt, setPrompt] = useState(agent?.prompt ?? "");
  const [provider, setProvider] = useState(agent?.provider ?? "");
  const [model, setModel] = useState(agent?.model ?? "");
  const [mcp, setMcp] = useState<string[]>(agent?.mcp_servers ?? []);
  const [slack, setSlack] = useState(!!agent?.slack);
  const [channel, setChannel] = useState(agent?.slack_channel ?? "");
  const [handoffs, setHandoffs] = useState<HandoffRow[]>((agent?.handoffs ?? []).map((h) => ({
    verdict: h.verdict, agent: h.agent, minutes: String(minutesOf(h.cooldown, 30)),
  })));

  const others = plan.agents.filter((a) => a.key !== agent?.key).map((a) => a.name);
  const nm = name.trim();
  const bad = !nm ? "Give the agent a name."
    : others.includes(nm) ? `Another agent is already called ${nm}.`
    : handoffs.some((h) => !VERDICT_RE.test(h.verdict.trim().toLowerCase()) || !h.agent)
      ? "Each handoff needs a one-word verdict, such as investigate, and an agent."
    : handoffs.some((h) => !(Number(h.minutes) >= 0)) ? "A handoff waits 0 minutes or more."
    : null;

  // Provider first, then a model from that provider's list; "" is the cell default on both
  // (the agent form's picker, without saving anything)
  const providers = cell.providers ?? [];
  const configured = providers.filter((p) => p.configured);
  const defaultProv = providers.find((p) => p.id === cell.defaultProvider);
  const providerOptions = ["", ...configured.map((p) => p.id).filter((id) => id !== cell.defaultProvider)];
  if (provider && !providerOptions.includes(provider)) providerOptions.push(provider);
  const providerLabels: Record<string, string> = { "": defaultProv ? `${defaultProv.name}, the default` : "the default" };
  for (const p of configured) providerLabels[p.id] = p.name;
  const effectiveId = provider || cell.defaultProvider || "";
  const effective = providers.find((p) => p.id === effectiveId);
  const effDefault = (effectiveId && cell.defaultModels?.[effectiveId]) || cell.defaultModel || "";
  const modelOptions = ["", ...((effective ? effective.models : cell.models) ?? []).filter((m) => m !== effDefault)];
  if (model && !modelOptions.includes(model)) modelOptions.push(model);
  const modelLabels: Record<string, string> = { "": effDefault ? `${effDefault}, the default` : "the default" };

  const wakeLabels: Record<string, string> = {};
  for (const w of plan.wakes) wakeLabels[w.name] = w.sentence ? w.sentence.replace(/\.$/, "") : w.name;
  const wakeOptions = plan.wakes.map((w) => w.name);
  if (trigger && !wakeOptions.includes(trigger)) { wakeOptions.push(trigger); wakeLabels[trigger] = `${trigger} (no longer in the plan)`; }

  const save = () => onSave({
    key, name: nm, trigger: onTrigger ? trigger : (agent?.trigger ?? trigger), on_trigger: onTrigger, prompt,
    provider: provider || null, model: model || null, mcp_servers: mcp,
    slack, slack_channel: slack ? channel : "",
    handoffs: handoffs.map((h) => ({ verdict: h.verdict.trim().toLowerCase(), agent: h.agent, cooldown: `${Number(h.minutes)}m` })),
    sentence: agent?.sentence ?? "", optional: agent?.optional ?? false, enabled: agent?.enabled ?? true,
    ...(agent?.existing ? { existing: true } : {}),
  });
  const setHandoff = (i: number, patch: Partial<HandoffRow>) =>
    setHandoffs((cur) => cur.map((h, j) => (j === i ? { ...h, ...patch } : h)));

  return (
    <EditorFrame title={agent ? `Change ${agent.name}` : "Add an agent"} onSave={save} onCancel={onCancel}
                 canSave={!bad} note={bad && <p className="help">{bad}</p>}>
      {!agent && reusable.length > 0 && (
        <div className="field">
          <span className="lbl">Use one already on Tares</span>
          <Picker value={reuse} options={["", ...reusable.map((x) => x.name)]}
                  labels={{ "": "Pick an agent" }} ariaLabel="Agent already on Tares" onChange={setReuse} />
          {picked && (
            <div className="su-reuse-card">
              <span className="su-item-what mono">{picked.name}</span>
              <span className="help">{picked.what}</span>
              <span className="help">{picked.used_by.length ? `Used by ${picked.used_by.map((u) => u.name).join(", ")}.` : "No project uses it yet."}</span>
              <div className="btnrow">
                <button type="button" className="su-small"
                        onClick={() => onSave({
                          key, name: picked.name, existing: true, trigger: plan.wakes[0]?.name ?? null, on_trigger: true,
                          prompt: "", provider: null, model: null, mcp_servers: [], handoffs: [],
                          sentence: "", optional: false, enabled: true,
                        })}>Use it</button>
              </div>
            </div>
          )}
        </div>
      )}
      {!agent && (cell.presets?.length ?? 0) > 0 && (
        <div className="field">
          <span className="lbl">Start from</span>
          <div className="su-chips">
            {cell.presets!.map((p) => (
              <button type="button" key={p.id} className="su-chip"
                      onClick={() => {
                        setPrompt(p.prompt);
                        if (!nm) setName(uniqueName(kebab(p.label) || "agent", plan.agents.map((a) => a.name)));
                      }}>{p.label}</button>
            ))}
          </div>
        </div>
      )}
      <label className="field">
        <span className="lbl">Name</span>
        <input type="text" value={name} maxLength={60} placeholder="checkout-triage" onChange={(e) => setName(e.target.value)} />
      </label>

      <fieldset className="su-choices">
        <legend className="lbl">When it runs</legend>
        <Choice name={`su-runs-${key}`} checked={onTrigger} onChange={() => setOnTrigger(true)} title="When the project wakes">
          It looks first.
        </Choice>
        <Choice name={`su-runs-${key}`} checked={!onTrigger} onChange={() => setOnTrigger(false)} title="Only when another agent hands it over">
          It digs in on another agent's verdict.
        </Choice>
      </fieldset>
      {onTrigger && wakeOptions.length > 1 && (
        <div className="field">
          <span className="lbl">On the wake-up</span>
          <Picker value={trigger} options={wakeOptions} labels={wakeLabels} ariaLabel="Wake-up that starts it" onChange={setTrigger} />
        </div>
      )}

      {agent?.existing ? (
        <p className="help">Its instructions, model and tools are the agent's own, shared with every project that uses it; change them on its page after setting up.</p>
      ) : (<>
      <label className="field">
        <span className="lbl">Instructions</span>
        <textarea rows={10} className="mono" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <span className="help">What to look at, what a useful finding says, and the one-word verdict to end with. It gets what happened when it runs.</span>
      </label>

      <div className="su-row">
        <div className="field">
          <span className="lbl">Model provider</span>
          <Picker value={provider} options={providerOptions} labels={providerLabels} ariaLabel="Model provider"
                  onChange={(v) => { setProvider(v); setModel(""); }} />
        </div>
        <div className="field">
          <span className="lbl">Model</span>
          <Picker value={model} options={modelOptions} ariaLabel="Model" labels={modelLabels} onChange={setModel} />
        </div>
      </div>

      {plan.tools.length > 0 && (
        <fieldset className="su-pick-list">
          <legend className="lbl">Tools it may use</legend>
          {plan.tools.map((t) => (
            <label key={t.key} className="su-check">
              <input type="checkbox" checked={mcp.includes(t.name)}
                     onChange={(e) => setMcp((cur) => (e.target.checked ? [...cur, t.name] : cur.filter((n) => n !== t.name)))} />
              <span><span className="mono">{t.name}</span>{!t.enabled && <span className="help"> (off until you turn it on under Tools)</span>}</span>
            </label>
          ))}
        </fieldset>
      )}
      </>)}

      <div className="field">
        <label className="su-check">
          <input type="checkbox" checked={slack} onChange={(e) => setSlack(e.target.checked)} />
          <span>Post what it finds to a Slack channel</span>
        </label>
        {slack && <SlackPick slack={cell.slack} value={channel} onChange={setChannel} />}
      </div>

      <fieldset className="su-filters">
        <legend className="lbl">When it concludes</legend>
        {handoffs.length === 0 && <p className="help">It records its finding and stops. Add a handoff to have another agent dig in on a verdict.</p>}
        {handoffs.map((h, i) => (
          <div className="su-filter" key={i} role="group" aria-label={`Handoff ${i + 1}`}>
            <label className="su-filter-value">
              <span className="su-small">On the verdict</span>
              <input type="text" value={h.verdict} placeholder="investigate" onChange={(e) => setHandoff(i, { verdict: e.target.value })} />
            </label>
            <div>
              <span className="su-small">hand over to</span>
              <Picker value={h.agent} options={others.length ? others : [""]} ariaLabel="Agent that takes over"
                      labels={{ "": "no other agent yet" }} onChange={(v) => setHandoff(i, { agent: v })} />
            </div>
            <label className="su-filter-value su-num-field">
              <span className="su-small">at most once every, minutes</span>
              <input type="number" min={0} value={h.minutes} onChange={(e) => setHandoff(i, { minutes: e.target.value })} />
            </label>
            <button type="button" className="linklike su-small" aria-label={`Remove handoff ${i + 1}`}
                    onClick={() => setHandoffs((cur) => cur.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <div>
          <button type="button" disabled={others.length === 0}
                  onClick={() => setHandoffs((cur) => [...cur, { verdict: "investigate", agent: others[0] ?? "", minutes: "30" }])}>
            Add a handoff
          </button>
          {others.length === 0 && <span className="help"> Add another agent first.</span>}
        </div>
      </fieldset>
    </EditorFrame>
  );
}
