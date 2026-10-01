import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import AgentForm from "../AgentForm";
import ConfirmDialog from "../ConfirmDialog";
import { ExternalAgentsPanel } from "../ProjectKeys";
import { TimeAgo, fmtCost } from "../bits";
import { RunsPanel, WakesOn, handedBy, statusBadge } from "../../pages/AgentDetail";
import type { BuiltinAgent } from "../../types";
import { Facts, NotHere, VLink, ViewHead, type AgentsData, type Ctx } from "./common";

/** The form's instance-wide choices, as every AgentForm on this page takes them. */
function formProps(d: AgentsData) {
  return {
    presets: d.presets, models: d.models, defaultModel: d.default_model,
    providers: d.providers ?? [], defaultProvider: d.default_provider ?? null,
    defaultModels: d.default_models ?? {}, slackWorkspace: d.slack_workspace,
    defaultMaxRounds: d.default_max_rounds, defaultMaxRoundsWithMcp: d.default_max_rounds_with_mcp,
    maxRoundsLimit: d.max_rounds_limit,
  };
}

/** Who hands off to `name`, one entry per verdict. */
function handoffsInto(all: BuiltinAgent[], name: string) {
  return all.flatMap((a) => (a.handoffs ?? []).filter((h) => h.agent === name).map((h) => ({ from: a.name, h })));
}

/** Agents: one row per Tares agent of the project, then the agents that joined from outside. */
export function AgentsView({ ctx }: { ctx: Ctx }) {
  const adding = ctx.params.get("add") !== null;
  const preset = ctx.params.get("trigger");     // the trigger to preselect, from a trigger's page
  const close = () => ctx.go({ kind: "agents" }, undefined, true);
  const d = ctx.agentsData;
  const all = d?.agents ?? [];
  return (
    <>
      <ViewHead title="Agents" sub="Tares agents run when their trigger fires or another agent hands off to them.">
        {!adding && (
          <button type="button" className="primary" disabled={!ctx.triggers.length}
                  title={ctx.triggers.length ? undefined : "an agent runs on a trigger; add a trigger first"}
                  onClick={() => ctx.go({ kind: "agents" }, { add: "1" }, true)}>Add agent</button>
        )}
      </ViewHead>
      {adding && d && (
        <div style={{ marginBottom: 12 }}>
          <AgentForm project={ctx.id}
                     presetTrigger={preset || ctx.triggers[0]?.name}
                     triggers={ctx.triggers.map((t) => t.name)}
                     {...formProps(d)}
                     onSaved={(name) => { ctx.refresh(); ctx.go({ kind: "agent", name }, undefined, true); }}
                     onCancel={close} />
        </div>
      )}
      {!d ? <div className="dim">loading…</div>
        : ctx.agents.length ? (
          <table>
            <thead><tr><th>agent</th><th>runs on</th><th>status</th><th>hands off to</th><th>last run</th><th className="num">runs</th></tr></thead>
            <tbody>
              {ctx.agents.map((a) => (
                <tr key={a.name} className="clickable" onClick={() => ctx.go({ kind: "agent", name: a.name })}>
                  <td><VLink v={{ kind: "agent", name: a.name }} className="mono">{a.name}</VLink></td>
                  <td><WakesOn agent={a} from={handedBy(all, a.name)} /></td>
                  <td>{a.enabled ? <span className="badge ok">on</span> : <span className="badge">off</span>}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {(a.handoffs ?? []).length
                      ? (a.handoffs ?? []).map((h) => (
                          <span key={`${h.verdict}-${h.agent}`} style={{ marginRight: 8, whiteSpace: "nowrap" }}>
                            <span className="help">{h.verdict}: </span>
                            <VLink v={{ kind: "agent", name: h.agent }} className="mono">{h.agent}</VLink>
                          </span>))
                      : <span className="dim">none</span>}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {a.last_run ? <><TimeAgo ts={a.last_run.started_at} /> {statusBadge(a.last_run)}</> : <span className="dim">never</span>}
                  </td>
                  <td className="num">{a.stats?.runs ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : !adding && (
          <div className="empty">
            {ctx.triggers.length
              ? "No Tares agents yet. Add an agent to act when this project's trigger fires."
              : <>No agents yet. An agent runs on a trigger, so <VLink v={{ kind: "triggers" }} extra={{ add: "1" }}>add a trigger</VLink> first.</>}
          </div>
        )}
      <div style={{ marginTop: 28 }}>
        <ExternalAgentsPanel project={ctx.id} onOpenKeys={() => ctx.go({ kind: "settings", name: "keys" })} />
      </div>
    </>
  );
}

/** One Tares agent: what wakes it, where it hands off, how it is configured, and its runs. */
export function AgentView({ ctx, name }: { ctx: Ctx; name: string }) {
  const [editing, setEditing] = useState(false);
  const [promptOpen, setPromptOpen] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [openRun, setOpenRun] = useState<string>();
  const d = ctx.agentsData;
  if (!d) return <div className="dim">loading…</div>;
  const agent = ctx.agents.find((a) => a.name === name);
  if (!agent) return <NotHere what="agent" name={name} back={{ kind: "agents" }} />;
  const all = d.agents;
  const from = handedBy(all, name);
  const into = handoffsInto(all, name);
  const loaded = (ctx.skills ?? []).filter((sk) => sk.loaded_by.includes(name));
  const stats = agent.stats;
  const agentLink = (n: string) => (ctx.agentNames.includes(n)
    ? <VLink v={{ kind: "agent", name: n }} className="mono">{n}</VLink>
    : <Link to={`/agents/${encodeURIComponent(n)}`} className="mono">{n}</Link>);
  const toggle = async () => {
    try {
      // on or off in this project (its wiring); the agent's other projects are left alone
      if (agent.enabled) await api.disableBuiltinAgent(name, ctx.id); else await api.enableBuiltinAgent(name, ctx.id);
      ctx.refresh();
    } catch (e) { ctx.fail(e); }
  };
  const handoffOnly = !agent.enabled && from.length > 0;
  const noTrigger = !agent.trigger;   // nothing wakes it on its own; only a handoff starts it
  return (
    <>
      <ViewHead title={<><span className="mono">{agent.name}</span>{" "}
                         {noTrigger ? (from.length > 0 && <span className="badge ok">on handoff</span>)
                           : agent.enabled ? <span className="badge ok">on</span> : <span className="badge">off</span>}</>}
                sub={noTrigger
                  ? (from.length > 0 ? <>Only <WakesOn agent={agent} from={from} /> starts it.</>
                    : <>Nothing starts it yet: it has no trigger, and no agent hands off to it.</>)
                  : <>Runs on <WakesOn agent={agent} from={from} />.{handoffOnly && " Its trigger is off, so only a handoff starts it."}</>}>
        {!editing && <>
          {!noTrigger && <button onClick={toggle}>{agent.enabled ? "Disable" : "Enable"}</button>}
          <button className="primary" onClick={() => setEditing(true)}>Edit</button>
          <button className="danger" onClick={() => setConfirmDel(true)}>Delete</button>
        </>}
      </ViewHead>
      {!d.key_configured && (
        <div className="alert warn">No model provider is configured, so this agent cannot run. Add one under <Link to="/settings?tab=anthropic">Settings, Model providers</Link>.</div>
      )}
      {editing ? (
        <AgentForm initial={agent}
                   triggers={[...new Set([agent.trigger, ...ctx.triggers.map((t) => t.name)])].filter(Boolean)}
                   {...formProps(d)}
                   onSaved={() => { setEditing(false); ctx.refresh(); }}
                   onCancel={() => setEditing(false)} />
      ) : (
        <Facts rows={([
          ["trigger", noTrigger ? <span className="dim">none; pick one under Edit to wake it on its own</span> : <>{ctx.triggers.some((t) => t.name === agent.trigger)
              ? <VLink v={{ kind: "trigger", name: agent.trigger }} className="chip mono">{agent.trigger}</VLink>
              : <Link to={`/triggers/${encodeURIComponent(agent.trigger)}`} className="chip mono">{agent.trigger}</Link>}
            {!agent.enabled && <span className="help"> · off; enable the agent to run on it</span>}</>],
          into.length ? ["handed off from", into.map(({ from: f, h }) => (
                <div key={`${f}-${h.verdict}`}>{agentLink(f)}<span className="help"> when it concludes </span>
                  <span className="mono">{h.verdict}</span></div>))] : null,
          ["hands off to", agent.handoffs?.length
            ? agent.handoffs.map((h) => (
                <div key={`${h.verdict}-${h.agent}`}>
                  <span className="mono">{h.verdict}</span><span className="help"> goes to </span>{agentLink(h.agent)}
                  <span className="help"> · once per entity per {h.cooldown}</span>
                </div>))
            : <span className="dim">no handoffs</span>],
          ["model", <><span className="mono">{agent.model || d.default_models?.[agent.effective_provider ?? ""] || d.default_model}</span>
            {!agent.model && <span className="help"> · provider default</span>}
            <span className="help"> · up to {agent.effective_max_rounds} rounds</span>
            {agent.budget_usd ? <span className="help"> · budget ${agent.budget_usd}</span> : null}</>],
          ["delivers to", <>
            <span className="help">the entity's timeline</span>
            {agent.slack_channel && <span className="help">, a Slack channel</span>}
            {!agent.slack_channel && agent.slack_configured && <span className="help">, a Slack webhook</span>}
            {agent.webhook_url && <>, <span className="mono" style={{ wordBreak: "break-all" }}>{agent.webhook_url}</span></>}</>],
          ...(agent.mcp_servers.length ? [["external tools", agent.mcp_servers.map((m) => <span key={m} className="chip mono">{m}</span>)] as [string, React.ReactNode]] : []),
          ["skills loaded", loaded.length
            ? loaded.map((sk) => <VLink key={sk.name} v={{ kind: "skill", name: sk.name }} className="chip mono">{sk.name}</VLink>)
            : <span className="dim">none in the last 7 days</span>],
          ["runs", stats && stats.runs
            ? <>{stats.runs}{stats.finished > 0 && <span className="help"> · {stats.ok} of {stats.finished} concluded</span>}
                <span className="help"> · {fmtCost(stats.cost_usd)} spent</span></>
            : <span className="dim">none yet</span>],
          ["prompt", promptOpen
            ? <><pre className="mono" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{agent.prompt}</pre>
                <button type="button" className="linklike" onClick={() => setPromptOpen(false)}>hide</button></>
            : <button type="button" className="linklike" onClick={() => setPromptOpen(true)}>
                show ({agent.prompt.split("\n").length} line{agent.prompt.split("\n").length === 1 ? "" : "s"})</button>],
        ] as ([string, React.ReactNode] | null)[]).filter((r): r is [string, React.ReactNode] => r !== null)} />
      )}
      <h3 style={{ margin: "20px 0 8px" }}>Runs</h3>
      <RunsPanel name={name} agent={agent} from={from} project={ctx.id}
                 focusDispatch={ctx.params.get("dispatch") ?? undefined}
                 focusRun={ctx.params.get("run") ?? undefined}
                 openRun={openRun} setOpenRun={setOpenRun} />
      {confirmDel && (
        <ConfirmDialog title={`Delete agent ${agent.name}?`}
          message="Findings already written stay on their timelines; this stops new ones. This can't be undone."
          confirmLabel="Delete" danger
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setConfirmDel(false);
            try { await api.deleteBuiltinAgent(agent.name); ctx.refresh(); ctx.go({ kind: "agents" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }} />
      )}
    </>
  );
}

/** One agent that joined from outside Tares: where the project's triggers deliver to it. */
export function ExternalView({ ctx, id }: { ctx: Ctx; id: string }) {
  const [revoking, setRevoking] = useState(false);
  if (!ctx.externals) return <div className="dim">loading…</div>;
  const a = ctx.externals.find((x) => x.subscription_id === id);
  if (!a) return <NotHere what="external agent" name={id} back={{ kind: "agents" }} />;
  return (
    <>
      <ViewHead title={a.name} sub="An agent outside Tares. Every trigger of this project delivers to it, and it records its findings here.">
        <button className="danger" onClick={() => setRevoking(true)}>Revoke</button>
      </ViewHead>
      <Facts rows={[
        ["delivers to", <span className="mono" style={{ wordBreak: "break-all" }}>{a.url}</span>],
        ["key", a.key_name ? <VLink v={{ kind: "settings", name: "keys" }}>{a.key_name}</VLink> : <span className="dim">none</span>],
        ...(a.created_at ? [["joined", <TimeAgo ts={a.created_at} />] as [string, React.ReactNode]] : []),
        ["last delivery", a.last_delivery
          ? <><TimeAgo ts={a.last_delivery.at} />
              {a.last_delivery.ok === false &&
                <span style={{ color: "var(--err)" }}> · failed{a.last_delivery.error ? `: ${a.last_delivery.error}` : ""}</span>}
              {" · "}<Link to={`/dispatches/${encodeURIComponent(a.last_delivery.dispatch_id)}`}>the firing</Link></>
          : <span className="dim">none yet</span>],
      ]} />
      {revoking && (
        <ConfirmDialog title={`Stop delivering to ${a.name}?`}
          message="The project's triggers stop delivering to this webhook. The key keeps working; revoke it under Settings, Keys to cut the agent off entirely."
          confirmLabel="Revoke" danger
          onCancel={() => setRevoking(false)}
          onConfirm={async () => {
            setRevoking(false);
            try { await api.unsubscribeProject(ctx.id, a.subscription_id); ctx.refresh(); ctx.go({ kind: "agents" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }} />
      )}
    </>
  );
}
