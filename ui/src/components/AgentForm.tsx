import { useEffect, useState } from "react";

import { Link } from "react-router-dom";

import { api } from "../api";
import type { SlackChannels } from "../api";
import type { ModelProvider } from "../types";
import { Combo, Picker } from "./bits";
import type { AgentPreset, BuiltinAgent, Handoff, Verdict } from "../types";

const VERDICT_RE = /^[a-z0-9][a-z0-9_-]*$/;
const DURATION_RE = /^\d+(\.\d+)?[smhd]$/;

/** One delivery option: a collapsed row whose title and description read before it is opened.
 *  Opening the row shows an explicit on/off toggle; the fields appear only when it is on. */
function OptionRow({ title, desc, on, disabled, disabledHint, onToggle, children }: {
  title: string; desc: string; on: boolean;
  disabled?: boolean; disabledHint?: string;
  onToggle: (on: boolean) => void;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(on);
  return (
    <div className="opt-row">
      <button type="button" className="opt-head" onClick={() => setOpen((o) => !o)}>
        <span className="opt-caret">{open ? "▾" : "▸"}</span>
        <span>
          <span className="opt-title">{title}</span>
          <span className="opt-desc help">{desc}</span>
        </span>
        <span className={"badge" + (on ? " ok" : "")}>{on ? "on" : "off"}</span>
      </button>
      {open && (
        <div className="opt-body">
          {disabled
            ? <span className="help">{disabledHint}</span>
            : (
              <label className="opt-toggle">
                <input type="checkbox" checked={on} onChange={(e) => onToggle(e.target.checked)} />
                <span>{on ? "enabled" : "enable"}</span>
              </label>
            )}
          {on && !disabled && children}
        </div>
      )}
    </div>
  );
}

// Create/edit a Tares agent. The prompt is the substance; the model is the one runtime choice an
// agent may pin (default: follow the instance). Tools and budgets stay Tares's decisions: more
// knobs turn a data-plane feature into an agent builder (docs/design/tares-agents.md). The trigger
// is chosen at creation and fixed thereafter (move an agent by deleting and recreating).
//
// Delivery is a list of collapsed option rows, each with an explicit toggle: the finding always
// lands on the entity's timeline; these rows only deliver it elsewhere too. The write-back URL and
// its bearer token render as one connected control (.hook-group): they are one credential pair,
// not two settings.
export default function AgentForm({ initial, prefill, deliveryKind, presetTrigger, triggers, project,
                                    presets, models, defaultModel, slackWorkspace, onSaved,
                                    onCancel, defaultMaxRounds = 6, defaultMaxRoundsWithMcp = 12,
                                    maxRoundsLimit = 24, providers = [], defaultProvider = null,
                                    defaultModels = {} }: {
  initial?: BuiltinAgent;              // absent = create
  prefill?: boolean;                   // initial is a proposal for a NEW agent: create, editable name
  deliveryKind?: "slack" | "webhook" | "none";   // prefill: which delivery row starts open (and on)
  presetTrigger?: string;              // create: trigger preselected (came from a trigger page)
  triggers: string[];                  // the project's triggers: an agent wakes on one in its own project
  project?: string;                    // the project it is made in (create); edit keeps initial.project
  presets: AgentPreset[];
  models: string[];                    // curated choices; [0] is the instance default
  defaultModel: string;
  providers?: ModelProvider[];         // the cell's providers (Settings); each lists its models
  defaultProvider?: string | null;     // the cell default's id
  defaultModels?: Record<string, string>;   // per provider id, the model "" resolves to
  slackWorkspace: boolean;             // a workspace bot token is configured
  defaultMaxRounds?: number;           // round cap when the agent has no external MCP servers
  defaultMaxRoundsWithMcp?: number;    // round cap once it does
  maxRoundsLimit?: number;             // upper bound for a per-agent override
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const isNew = !initial || !!prefill;
  const projectId = project ?? initial?.project ?? "";
  const [name, setName] = useState(initial?.name ?? "");
  const [trigger, setTrigger] = useState(initial?.trigger ?? presetTrigger ?? "");
  const [prompt, setPrompt] = useState(initial?.prompt ?? "");
  const [model, setModel] = useState(initial?.model ?? "");
  const [provider, setProvider] = useState(initial?.provider ?? "");

  // Delivery options: each is a toggle plus its fields. Off at save time means off, even if the
  // fields still hold text.
  const [channelOn, setChannelOn] = useState(!!initial?.slack_channel || (deliveryKind === "slack" && slackWorkspace));
  const [channel, setChannel] = useState(initial?.slack_channel ?? "");
  const [hookOn, setHookOn] = useState(!!initial?.slack_configured);
  const [slack, setSlack] = useState("");
  const [writebackOn, setWritebackOn] = useState(!!initial?.webhook_url || deliveryKind === "webhook");
  const [webhookUrl, setWebhookUrl] = useState(initial?.webhook_url ?? "");
  const [webhookKeyLabel, setWebhookKeyLabel] = useState(initial?.webhook_key_label ?? "");
  const [webhookToken, setWebhookToken] = useState("");
  const [mcpSel, setMcpSel] = useState<string[]>(initial?.mcp_servers ?? []);
  // "" = default (6 rounds, or 12 once the agent uses external MCP servers).
  const [maxRounds, setMaxRounds] = useState<string>(
    initial?.max_rounds ? String(initial.max_rounds) : "");
  const [budget, setBudget] = useState<string>(
    initial?.budget_usd ? String(initial.budget_usd) : "");
  const [advancedOpen, setAdvancedOpen] = useState(!!initial?.max_rounds || !!initial?.budget_usd);
  // When it concludes: verdict -> the project agent that takes over (TR-334)
  const [handoffs, setHandoffs] = useState<Handoff[]>(initial?.handoffs ?? []);
  // How a run ends: always with conclude, and the verdicts it may give (its words, each with when)
  const [concludes, setConcludes] = useState(!!initial?.concludes);
  const [verdicts, setVerdicts] = useState<Verdict[]>(initial?.verdicts ?? []);
  const [peers, setPeers] = useState<string[]>();
  useEffect(() => {
    let live = true;
    api.builtinAgents().then((r) => {
      // any agent on the cell can take a handoff (P-TR-216: parts are shared)
      if (live) setPeers(r.agents.map((a) => a.name));
    }).catch(() => { if (live) setPeers([]); });
    return () => { live = false; };
  }, [projectId]);
  const others = (peers ?? []).filter((n) => n !== name.trim());
  const setHandoff = (i: number, patch: Partial<Handoff>) =>
    setHandoffs((cur) => cur.map((h, j) => (j === i ? { ...h, ...patch } : h)));
  const handoffBad = (h: Handoff) => !VERDICT_RE.test(h.verdict.trim().toLowerCase()) || !h.agent
    || (!!h.cooldown.trim() && !DURATION_RE.test(h.cooldown.trim()));
  const handoffKey = (h: Handoff) => `${h.verdict.trim().toLowerCase()}\u0000${h.agent}`;
  const handoffDup = handoffs.some((h, i) => handoffs.findIndex((o) => handoffKey(o) === handoffKey(h)) !== i);
  const setVerdict = (i: number, patch: Partial<Verdict>) =>
    setVerdicts((cur) => cur.map((v, j) => (j === i ? { ...v, ...patch } : v)));
  const word = (v: Verdict) => v.verdict.trim().toLowerCase();
  const verdictBad = (v: Verdict, i: number) => !VERDICT_RE.test(word(v)) || word(v) === "no_op"
    || verdicts.findIndex((o) => word(o) === word(v)) !== i;
  // the words its handoffs can key on; none listed: any word
  const verdictWords = concludes ? verdicts.map(word).filter((w) => VERDICT_RE.test(w)) : [];

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  // Provider first, then a model from that provider's list. "" is a real option on both (the cell
  // default provider; that provider's default model), so each picker has one more entry than the
  // list and the default reads as what it is rather than as a copy of a name.
  const configured = providers.filter((p) => p.configured);
  const defaultProv = providers.find((p) => p.id === defaultProvider);
  const providerOptions = ["", ...configured.map((p) => p.id).filter((id) => id !== defaultProvider)];
  const providerLabels: Record<string, string> = { "": defaultProv ? `${defaultProv.name} · cell default` : "cell default (none configured yet)" };
  for (const p of configured) providerLabels[p.id] = p.name;
  const missingProvider = !!provider && !configured.some((p) => p.id === provider);
  const effectiveId = provider || defaultProvider || "";
  const effective = providers.find((p) => p.id === effectiveId);
  const providerModels = effective ? effective.models : models;
  const effectiveDefaultModel = (effectiveId && defaultModels[effectiveId]) || defaultModel;
  const modelOptions = ["", ...providerModels.filter((m) => m !== effectiveDefaultModel)];
  if (model && !modelOptions.includes(model)) modelOptions.push(model);
  const modelLabels: Record<string, string> = { "": effectiveDefaultModel ? `${effectiveDefaultModel} · ${effective ? effective.name : "instance"} default` : "pick a model (this provider lists none)" };
  const pickProvider = (id: string) => { setProvider(id); setModel(""); };

  // The channel list comes from the workspace bot, exactly like the trigger page's picker: only
  // channels the bot is in are offered, because anything else fails at the first post.
  const [channels, setChannels] = useState<SlackChannels>();
  useEffect(() => {
    if (!slackWorkspace) return;
    let live = true;
    api.slackChannels().then((c) => { if (live) setChannels(c); })
      .catch(() => { if (live) setChannels({ channels: [], reason: "error" }); });
    return () => { live = false; };
  }, [slackWorkspace]);
  // The MCP registry: which servers exist is managed on its own page; here the agent only picks
  // from them.
  const [mcpAvail, setMcpAvail] = useState<{ name: string; url: string }[]>();
  useEffect(() => {
    let live = true;
    // any MCP server on the cell (P-TR-216: parts are shared)
    api.mcpServers().then((r) => {
      if (live) setMcpAvail(r.servers);
    }).catch(() => { if (live) setMcpAvail([]); });
    return () => { live = false; };
  }, [projectId]);
  const toggleMcp = (name: string, on: boolean) =>
    setMcpSel((cur) => (on ? [...cur, name] : cur.filter((n) => n !== name)));

  const chanList = channels?.reason === null ? channels.channels : [];
  const chanLabels: Record<string, string> = { "": "pick a channel…" };
  for (const c of chanList) chanLabels[c.id] = (c.is_private ? "🔒 " : "#") + c.name;

  const save = async () => {
    setBusy(true); setErr(undefined);
    const body = {
      name: name.trim(), trigger, prompt: prompt.trim(), model, provider,
      ...(projectId ? { project: projectId } : {}),
      slack_channel: channelOn ? channel : "",
      slack_webhook: hookOn ? slack.trim() : "",
      slack_webhook_clear: !hookOn,
      webhook_url: writebackOn ? webhookUrl.trim() : "",
      webhook_token: writebackOn ? webhookToken.trim() : "",
      webhook_key_label: writebackOn ? webhookKeyLabel.trim() : "",
      mcp_servers: mcpSel,
      max_rounds: maxRounds.trim() ? Number(maxRounds) : null,
      budget_usd: budget.trim() ? Number(budget) : null,
      handoffs: handoffs.map((h) => ({ verdict: h.verdict.trim().toLowerCase(), agent: h.agent,
                                      cooldown: h.cooldown.trim() || "30m" })),
      concludes,
      verdicts: concludes ? verdicts.map((v) => ({ verdict: word(v), when: (v.when ?? "").trim() })) : [],
    };
    try {
      if (isNew) await api.createBuiltinAgent(body);
      else await api.updateBuiltinAgent(initial!.name, body);
      onSaved(body.name);
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  // The trigger that wakes it on its own, or none: then only a handoff from another agent starts
  // it (it stays on the projects that hand off to it). A trigger that is gone shows as itself.
  const triggerPicker = (
    <>
      <Picker value={trigger}
              options={trigger && !triggers.includes(trigger) ? ["", ...triggers, trigger] : ["", ...triggers]}
              labels={{ "": "No trigger: only a handoff starts it" }}
              ariaLabel="trigger" onChange={setTrigger} />
      {!triggers.length && <span className="help">This project has no triggers yet. Without one, only a handoff starts this agent.</span>}
    </>
  );

  return (
    <div className="panel">
      {err && <div className="alert error">{err}</div>}
      {isNew ? (
        <div className="row2">
          <label className="field">
            <span className="lbl">name</span>
            <input type="text" value={name} placeholder="e.g. incident-first-look"
                   onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="field">
            <span className="lbl">trigger</span>
            {triggerPicker}
          </div>
        </div>
      ) : (
        // The name is fixed after creation; the trigger can change, or go: an agent without one
        // is started only by a handoff.
        // overflow visible: the trigger picker's menu opens below the table, not clipped by it
        <table style={{ marginBottom: 12, overflow: "visible" }}>
          <tbody>
            <tr><td className="help" style={{ width: 120 }}>name</td>
                <td className="mono">{name} <span className="help">fixed</span></td></tr>
            <tr><td className="help">trigger</td>
                <td>{triggerPicker}</td></tr>
          </tbody>
        </table>
      )}
      <label className="field">
        <span className="lbl">prompt</span>
        <textarea rows={12} className="mono" value={prompt}
                  onChange={(e) => setPrompt(e.target.value)} />
        <span className="help">
          the correlated timeline is supplied at firing time; the final message becomes the finding
        </span>
      </label>
      {isNew && presets.length > 0 && (
        <div className="btnrow" style={{ marginBottom: 8 }}>
          <span className="help" style={{ alignSelf: "center" }}>start from:</span>
          {presets.map((p) => (
            <button key={p.id} onClick={() => {
              setPrompt(p.prompt);
              if (p.concludes !== undefined) { setConcludes(!!p.concludes); setVerdicts(p.verdicts ?? []); }
            }}>{p.label}</button>
          ))}
        </div>
      )}
      <div className="row2">
        <div className="field">
          <span className="lbl">provider</span>
          <Picker value={provider} onChange={pickProvider}
                  options={missingProvider ? [...providerOptions, provider] : providerOptions}
                  labels={missingProvider ? { ...providerLabels, [provider]: `${provider} · not configured` } : providerLabels}
                  ariaLabel="provider" />
          {missingProvider && <span className="help">this provider is not on this cell; runs use the default until you pick one. Add it under Settings, Model providers.</span>}
          {configured.length === 0 && <span className="help">no provider configured yet; add one under Settings, Model providers.</span>}
        </div>
        <div className="field">
          <span className="lbl">model</span>
          {providerModels.length === 0 && effective
            ? <>
                <input type="text" className="mono" value={model} placeholder="the model id, as the endpoint names it"
                       onChange={(e) => setModel(e.target.value)} />
                <span className="help">{effective.name} listed no models; type the id, or refresh its list under Settings, Model providers.</span>
              </>
            : <Picker value={model} onChange={setModel} options={modelOptions} labels={modelLabels}
                      ariaLabel="model" />}
        </div>
      </div>

      <div className="field">
        <h3 style={{ margin: "10px 0 2px", fontSize: 16 }}>External tools</h3>
        <span className="help" style={{ display: "block", margin: "0 0 10px" }}>
          MCP servers this agent may call, alongside its built-in reads. Tools from these servers
          can act on your systems; enable only what this agent should touch.
        </span>
        {mcpAvail === undefined ? <span className="dim">loading…</span>
          : mcpAvail.length === 0 ? (
            <span className="help">
              none in this project yet. Add one under <Link to={projectId ? `/projects/${encodeURIComponent(projectId)}?view=settings:mcp` : "/projects"}>MCP servers</Link>, then
              pick it here.
            </span>
          ) : (
            <>
              {mcpSel.length > 0 && (
                <div className="btnrow" style={{ marginBottom: 8, flexWrap: "wrap" }}>
                  {mcpSel.map((name) => (
                    <span key={name} className="chip mono" title={mcpAvail.find((m) => m.name === name)?.url}>
                      {name}
                      <button type="button" className="chip-x" aria-label={`remove ${name}`}
                              onClick={() => toggleMcp(name, false)}>×</button>
                    </span>
                  ))}
                </div>
              )}
              {mcpAvail.some((m) => !mcpSel.includes(m.name)) && (
                <Picker value="" ariaLabel="add an MCP server"
                        options={mcpAvail.filter((m) => !mcpSel.includes(m.name)).map((m) => m.name)}
                        labels={{ "": "add a server…" }}
                        onChange={(name) => { if (name) toggleMcp(name, true); }} />
              )}
              <span className="help">
                manage connections under <Link to={projectId ? `/projects/${encodeURIComponent(projectId)}?view=settings:mcp` : "/projects"}>MCP servers</Link>
              </span>
            </>
          )}
      </div>

      <div className="field">
        <h3 style={{ margin: "10px 0 2px", fontSize: 16 }}>Deliver findings</h3>
        <span className="help" style={{ display: "block", margin: "0 0 10px" }}>
          every finding lands on the entity's timeline; these options also deliver it elsewhere
        </span>
        <OptionRow title="Slack channel"
                   desc="the workspace bot posts the full finding to a channel"
                   on={channelOn} disabled={!slackWorkspace}
                   disabledHint="connect a workspace bot under Settings to enable this"
                   onToggle={setChannelOn}>
          <Picker value={channel} onChange={setChannel}
                  options={["", ...chanList.map((c) => c.id)]} labels={chanLabels}
                  ariaLabel="Slack channel" />
          <span className="help">
            {channels?.reason === "missing_scope" && "reconnect Slack to list channels"}
            {channels?.reason === null && chanList.length === 0 && "add the bot to a channel in Slack first"}
          </span>
        </OptionRow>
        <OptionRow title="Slack incoming webhook"
                   desc="legacy per-agent webhook; used only when no channel is set"
                   on={hookOn} onToggle={setHookOn}>
          <input type="text" className="mono" placeholder={
            initial?.slack_configured ? "•••• configured, leave blank to keep" : "https://hooks.slack.com/services/…"}
                 value={slack} onChange={(e) => setSlack(e.target.value)} />
        </OptionRow>
        <OptionRow title="Write-back webhook"
                   desc="POST each finding as JSON with its run metadata to your own automation"
                   on={writebackOn} onToggle={setWritebackOn}>
          <div className="hook-group">
            <input type="text" className="mono" placeholder="https://your-automation.example.com/findings"
                   value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} />
            <input type="password" className="mono" autoComplete="new-password" placeholder={
              initial?.webhook_token_configured ? "bearer token: •••• configured, leave blank to keep" : "bearer token (optional)"}
                   value={webhookToken} onChange={(e) => setWebhookToken(e.target.value)} />
          </div>
          <span className="help">
            finding + run metadata as JSON; the token is sent as a bearer header
          </span>
          <label className="field" style={{ marginTop: 8 }}>
            <span className="lbl">report as key</span>
            <input type="text" className="mono" placeholder="the entity (default)"
                   value={webhookKeyLabel} onChange={(e) => setWebhookKeyLabel(e.target.value)} />
            <span className="help">
              a label name; the POST's <span className="mono">key</span> becomes that label's value on
              the firing that woke the run, for a system that files reports by its own id. Empty
              reports the entity.
            </span>
          </label>
        </OptionRow>
      </div>

      <div className="field">
        <h3 style={{ margin: "10px 0 2px", fontSize: 16 }}>How it ends</h3>
        <label className="su-check">
          <input type="checkbox" checked={concludes} onChange={(e) => setConcludes(e.target.checked)} />
          <span>Ends every run with a conclusion</span>
        </label>
        <span className="help" style={{ display: "block", margin: "2px 0 8px" }}>
          {concludes
            ? "Each run ends with nothing to report, or a finding with a verdict. If the agent stops without one, Tares asks it again."
            : prompt.includes("conclude")
              ? "Its prompt mentions conclude, so it can already end with one; tick this to make it required and to set its verdicts."
              : "Off: the agent's last message is its finding, with no verdict."}
        </span>
        {concludes && (
          <>
            <span className="lbl">Verdicts it can give</span>
            {verdicts.length > 0 && (
              <div className="kv-rows verdict-rows" style={{ margin: "4px 0 8px" }}>
                {verdicts.map((v, i) => (
                  <div key={i} className="verdict-row">
                    <input type="text" className="mono" value={v.verdict} placeholder="one word, e.g. investigate"
                           aria-label="verdict" onChange={(e) => setVerdict(i, { verdict: e.target.value })} />
                    <input type="text" value={v.when ?? ""} placeholder="when to give it, e.g. a service looks broken"
                           aria-label="when to give it" onChange={(e) => setVerdict(i, { when: e.target.value })} />
                    <button type="button" aria-label="remove this verdict"
                            onClick={() => setVerdicts((cur) => cur.filter((_, j) => j !== i))}>×</button>
                  </div>
                ))}
              </div>
            )}
            {verdicts.length < 10 && (
              <button type="button" onClick={() => setVerdicts((cur) => [...cur, { verdict: "", when: "" }])}>
                Add a verdict
              </button>
            )}
            <span className="help" style={{ display: "block", marginTop: 4 }}>
              {verdicts.length
                ? "A finding must carry one of these; any other word goes back to the agent to fix. Nothing to report is always possible."
                : "None listed: a finding can carry any verdict, or none."}
            </span>
            {verdicts.some(verdictBad) && (
              <span className="help" style={{ display: "block", marginTop: 4 }}>
                each verdict is one word (letters, digits, dashes), listed once, and not no_op
              </span>
            )}
          </>
        )}
      </div>

      <div className="field">
        <h3 style={{ margin: "10px 0 2px", fontSize: 16 }}>When it concludes</h3>
        <span className="help" style={{ display: "block", margin: "0 0 10px" }}>
          when a run ends with a finding of this verdict, the agent you pick takes over on the same
          entity and is handed the finding. It runs whether or not it is on for its own trigger,
          at most once per entity within the cooldown.
        </span>
        {handoffs.length > 0 && (
          <div className="kv-rows" style={{ marginBottom: 8 }}>
            {handoffs.map((h, i) => (
              <div key={i} className="handoff-row">
                <Combo value={h.verdict} className="mono" placeholder="verdict, e.g. investigate"
                       options={verdictWords} onChange={(v) => setHandoff(i, { verdict: v })} />
                <Picker value={h.agent} ariaLabel="agent that takes over"
                        options={["", ...others, ...(h.agent && !others.includes(h.agent) ? [h.agent] : [])]}
                        labels={{ "": others.length ? "pick an agent…" : "no other agent in this project yet" }}
                        onChange={(v) => setHandoff(i, { agent: v })} />
                <input type="text" className="mono" value={h.cooldown} placeholder="30m"
                       aria-label="cooldown" title="at most one handoff per entity within this time"
                       onChange={(e) => setHandoff(i, { cooldown: e.target.value })} />
                <button type="button" aria-label="remove this handoff"
                        onClick={() => setHandoffs((cur) => cur.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
          </div>
        )}
        {handoffs.length < 10 && (
          <button type="button" disabled={peers !== undefined && others.length === 0}
                  title={peers !== undefined && others.length === 0 ? "add another agent to this project first" : undefined}
                  onClick={() => setHandoffs((cur) => [...cur, { verdict: "", agent: others.length === 1 ? others[0] : "", cooldown: "30m" }])}>
            Add a handoff
          </button>
        )}
        {verdictWords.length > 0 && handoffs.some((h) => h.verdict.trim() && !verdictWords.includes(h.verdict.trim().toLowerCase())) && (
          <span className="help" style={{ display: "block", marginTop: 4 }}>
            {name.trim() || "this agent"} never gives {handoffs.filter((h) => h.verdict.trim() && !verdictWords.includes(h.verdict.trim().toLowerCase()))
              .map((h) => h.verdict.trim().toLowerCase()).join(", ")}; add it to its verdicts above, or that handoff never runs
          </span>
        )}
        {handoffs.some(handoffBad) && (
          <span className="help" style={{ display: "block", marginTop: 4 }}>
            each handoff needs a one-word verdict, an agent, and a cooldown such as 30m or 2h
          </span>
        )}
        {handoffDup && (
          <span className="help" style={{ display: "block", marginTop: 4 }}>
            the same verdict and agent appear twice; keep one
          </span>
        )}
      </div>

      <div className="field">
        <button type="button" onClick={() => setAdvancedOpen((o) => !o)}
                style={{ padding: 0, border: 0, background: "none", cursor: "pointer" }}
                className="help">
          {advancedOpen ? "Hide advanced" : "Advanced"}
        </button>
        {advancedOpen && (
          <div style={{ marginTop: 8 }}>
            <span className="lbl">max rounds</span>
            <input type="number" min={1} max={maxRoundsLimit} value={maxRounds}
                   placeholder={String(mcpSel.length ? defaultMaxRoundsWithMcp : defaultMaxRounds)}
                   onChange={(e) => setMaxRounds(e.target.value)}
                   style={{ width: 90 }} aria-label="max rounds" />
            <span className="help" style={{ display: "block", marginTop: 4 }}>
              model rounds per run; raise it for agents that use external MCP servers.
              Blank means the default: {defaultMaxRounds}, or {defaultMaxRoundsWithMcp} when
              external MCP servers are enabled. Limit {maxRoundsLimit}. One extra call is made
              when the budget runs out, to ask for a conclusion.
            </span>
          </div>
        )}
      </div>

      <div className="btnrow">
        <button className="primary" onClick={save}
                disabled={busy || !name.trim() || !prompt.trim() || (concludes && verdicts.some(verdictBad))
                          || (writebackOn && !webhookUrl.trim())
                          || (channelOn && !channel)
                          || handoffs.some(handoffBad) || handoffDup
                          || (!!maxRounds.trim() && (Number(maxRounds) < 1
                              || Number(maxRounds) > maxRoundsLimit
                              || !Number.isInteger(Number(maxRounds))))}>
          {isNew ? "Create agent" : "Save changes"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
