import { useEffect, useState } from "react";

import { Link } from "react-router-dom";

import { api } from "../api";
import type { SlackChannels } from "../api";
import { Combo, Picker } from "./bits";
import type { AgentPreset, BuiltinAgent } from "../types";

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
export default function AgentForm({ initial, prefill, deliveryKind, presetTrigger, triggers,
                                    presets, models, defaultModel, slackWorkspace, onSaved,
                                    onCancel, defaultMaxRounds = 6, defaultMaxRoundsWithMcp = 12,
                                    maxRoundsLimit = 24 }: {
  initial?: BuiltinAgent;              // absent = create
  prefill?: boolean;                   // initial is a proposal for a NEW agent: create, editable name
  deliveryKind?: "slack" | "webhook" | "none";   // prefill: which delivery row starts open (and on)
  presetTrigger?: string;              // create: trigger preselected (came from a trigger page)
  triggers: string[];
  presets: AgentPreset[];
  models: string[];                    // curated choices; [0] is the instance default
  defaultModel: string;
  slackWorkspace: boolean;             // a workspace bot token is configured
  defaultMaxRounds?: number;           // round cap when the agent has no external MCP servers
  defaultMaxRoundsWithMcp?: number;    // round cap once it does
  maxRoundsLimit?: number;             // upper bound for a per-agent override
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const isNew = !initial || !!prefill;
  const [name, setName] = useState(initial?.name ?? "");
  const [trigger, setTrigger] = useState(initial?.trigger ?? presetTrigger ?? "");
  const [prompt, setPrompt] = useState(initial?.prompt ?? "");
  const [model, setModel] = useState(initial?.model ?? "");

  // Delivery options: each is a toggle plus its fields. Off at save time means off, even if the
  // fields still hold text.
  const [channelOn, setChannelOn] = useState(!!initial?.slack_channel || (deliveryKind === "slack" && slackWorkspace));
  const [channel, setChannel] = useState(initial?.slack_channel ?? "");
  const [hookOn, setHookOn] = useState(!!initial?.slack_configured);
  const [slack, setSlack] = useState("");
  const [writebackOn, setWritebackOn] = useState(!!initial?.webhook_url || deliveryKind === "webhook");
  const [webhookUrl, setWebhookUrl] = useState(initial?.webhook_url ?? "");
  const [webhookToken, setWebhookToken] = useState("");
  const [mcpSel, setMcpSel] = useState<string[]>(initial?.mcp_servers ?? []);
  // "" = default (6 rounds, or 12 once the agent uses external MCP servers).
  const [maxRounds, setMaxRounds] = useState<string>(
    initial?.max_rounds ? String(initial.max_rounds) : "");
  const [budget, setBudget] = useState<string>(
    initial?.budget_usd ? String(initial.budget_usd) : "");
  const [advancedOpen, setAdvancedOpen] = useState(!!initial?.max_rounds || !!initial?.budget_usd);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  // "" is a real option (follow the instance default), so the picker always has one more entry
  // than the curated list, and the default reads as what it is rather than as a copy of a model.
  const modelOptions = ["", ...models.filter((m) => m !== defaultModel)];
  const modelLabels: Record<string, string> = { "": `${defaultModel} · instance default` };

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
    api.mcpServers().then((r) => { if (live) setMcpAvail(r.servers); }).catch(() => { if (live) setMcpAvail([]); });
    return () => { live = false; };
  }, []);
  const toggleMcp = (name: string, on: boolean) =>
    setMcpSel((cur) => (on ? [...cur, name] : cur.filter((n) => n !== name)));

  const chanList = channels?.reason === null ? channels.channels : [];
  const chanLabels: Record<string, string> = { "": "pick a channel…" };
  for (const c of chanList) chanLabels[c.id] = (c.is_private ? "🔒 " : "#") + c.name;

  const save = async () => {
    setBusy(true); setErr(undefined);
    const body = {
      name: name.trim(), trigger, prompt: prompt.trim(), model,
      slack_channel: channelOn ? channel : "",
      slack_webhook: hookOn ? slack.trim() : "",
      slack_webhook_clear: !hookOn,
      webhook_url: writebackOn ? webhookUrl.trim() : "",
      webhook_token: writebackOn ? webhookToken.trim() : "",
      mcp_servers: mcpSel,
      max_rounds: maxRounds.trim() ? Number(maxRounds) : null,
      budget_usd: budget.trim() ? Number(budget) : null,
    };
    try {
      if (isNew) await api.createBuiltinAgent(body);
      else await api.updateBuiltinAgent(initial!.name, body);
      onSaved(body.name);
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

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
            <Combo value={trigger} options={triggers}
                   placeholder="the trigger that wakes this agent" onChange={setTrigger} />
          </div>
        </div>
      ) : (
        // Name and trigger are fixed after creation: shown as read-only facts, not fields, so it's
        // clear they can't be edited here (to move the agent, delete and recreate).
        <table style={{ marginBottom: 12 }}>
          <tbody>
            <tr><td className="help" style={{ width: 120 }}>name</td>
                <td className="mono">{name} <span className="help">fixed</span></td></tr>
            <tr><td className="help">trigger</td>
                <td className="mono">{trigger} <span className="help">fixed; delete and recreate to move</span></td></tr>
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
            <button key={p.id} onClick={() => setPrompt(p.prompt)}>{p.label}</button>
          ))}
        </div>
      )}
      <div className="field">
        <span className="lbl">model</span>
        <Picker value={model} onChange={setModel} options={modelOptions} labels={modelLabels}
                ariaLabel="model" />
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
              none connected yet. Add one under <Link to="/mcp-servers">MCP servers</Link>, then
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
                manage connections under <Link to="/mcp-servers">MCP servers</Link>
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
        </OptionRow>
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
                disabled={busy || !name.trim() || !trigger.trim() || !prompt.trim()
                          || (writebackOn && !webhookUrl.trim())
                          || (channelOn && !channel)
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
