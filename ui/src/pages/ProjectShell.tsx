import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Combo, Picker, TimeAgo, usePolling } from "../components/bits";
import InfoDialog, { HelpButton } from "../components/InfoDialog";
import { SessionsPanel } from "../components/ChallengerSessions";
import ProjectActivity from "../components/ProjectActivity";
import ProjectNav from "../components/project/ProjectNav";
import { parseView, viewParam, viewSearch, type Ctx, type View } from "../components/project/common";
import { EventsView, SourceView, SourcesView } from "../components/project/sources";
import { TriggerView, TriggersView } from "../components/project/triggers";
import { AgentView, AgentsView, ExternalView } from "../components/project/agents";
import { SkillView, SkillsView } from "../components/project/skills";
import { HistoryView, KeysView, McpView, SubscribersView } from "../components/project/settings";
import type { ProjectSummary, Template, RecipeActionOption, Source } from "../types";

// The page for every project, driven by the live APIs plus the template's summary. The header
// (name, status, Pause/Edit/Delete, the counters) sits over two columns: the project's own
// navigation on the left, filled with its real sources, triggers, agents and skills, and one view
// on the right. Activity, the timeline of every agent's runs, is where it lands. The view lives
// in the URL (see components/project/common.tsx), so links and the back button work.
// Template-specific content arrives as data (actions, facts, panels, cards), never as
// template-specific markup; actions and panels head the Activity view.

export default function ProjectShell({ s, id, reload, template }: {
  s: ProjectSummary; id: string; reload: () => void; template?: Template;
}) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const custom = s.template === "custom";
  const view = parseView(params, !!s.sessions);
  const go = (v: View, extra?: Record<string, string>, replace = false) => {
    setActionError(undefined);
    setParams(new URLSearchParams(viewSearch(v, extra)), { replace });
  };

  const sourceCount = (s.objects ?? []).filter((o) => o.kind === "source" && !o.missing).length;
  const pausedSources = ((s.params as Record<string, unknown> | undefined)?.paused_sources as string[] | undefined) ?? [];
  const [busyKey, setBusyKey] = useState<string>();
  const [actionArgs, setActionArgs] = useState<Record<string, string>>({});
  const [actionMsg, setActionMsg] = useState<string>();
  const [actionBusy, setActionBusy] = useState<string>();
  const [actionHelp, setActionHelp] = useState(false);
  const [actionError, setActionError] = useState<string>();
  const [confirmDel, setConfirmDel] = useState(false);
  // Deleting a project always takes its triggers, agents and MCP servers. Sources are shared, so
  // the dialog lists them and the person ticks which go too; one another project uses is kept.
  const [delSources, setDelSources] = useState<Set<string>>(new Set());
  const [purge, setPurge] = useState(false);
  const [confirmPause, setConfirmPause] = useState(false);
  const [pauseSources, setPauseSources] = useState(false);

  const names = (kind: string) => s.objects.filter((o) => o.kind === kind).map((o) => o.name);
  const { data: sources, reload: reloadSources } = usePolling(() => api.sources(), 10000);
  const { data: specs } = usePolling(() => api.connectors(), 600000);
  const { data: triggers, reload: reloadTriggers } = usePolling(() => api.triggers(), 10000);
  const { data: agentsData, reload: reloadAgents } = usePolling(() => api.builtinAgents(), 10000);
  const { data: mcp, reload: reloadMcp } = usePolling(() => api.mcpServers(), 30000);
  const { data: projects } = usePolling(() => api.projects(), 30000);
  const { data: dispatches, reload: reloadDispatches } = usePolling(() => api.dispatches(100), 10000);
  const { data: roster, reload: reloadRoster } = usePolling(() => api.agents(), 15000);
  const { data: slack } = usePolling(() => api.slackChannels(), 60000);
  const { data: externals, reload: reloadExternals } = usePolling(() => api.externalAgents(id), 15000);
  const { data: skills, reload: reloadSkills } = usePolling(() => api.skills(id), 15000);

  // Membership from both sides: the summary's objects and each object's own `project`, so a trigger
  // made a moment ago shows before the summary is refetched.
  const mySources = (sources ?? []).filter((x) => names("source").includes(x.name) || (x.projects ?? []).includes(id));
  const myTriggers = (triggers ?? []).filter((x) => names("trigger").includes(x.name) || x.project === id);
  const agentNames = [...new Set([...names("agent"),
    ...(agentsData?.agents ?? []).filter((a) => a.project === id).map((a) => a.name)])];
  const myAgents = (agentsData?.agents ?? []).filter((a) => agentNames.includes(a.name));
  const mcpNames = [...new Set([...names("mcp_server"),
    ...(mcp?.servers ?? []).filter((m) => m.project === id).map((m) => m.name)])];
  const projectName = (pid: string) => projects?.projects.find((p) => p.id === pid)?.name ?? pid;
  const otherProjects = (x: Source) => (x.projects ?? []).filter((p) => p !== id);
  // a Slack channel id as its name, when the bot still sees it
  const channelLabel = (raw: string) => {
    const cid = raw.replace(/^#/, "");
    const hit = (slack?.channels ?? []).find((c) => c.id === cid);
    return hit ? (hit.is_private ? `🔒 ${hit.name}` : `#${hit.name}`) : raw;
  };
  const fail = (e: unknown) => setActionError(String((e as Error).message ?? e));
  const refresh = () => {
    reload(); reloadSources(); reloadTriggers(); reloadAgents(); reloadMcp(); reloadDispatches();
    reloadRoster(); reloadExternals(); reloadSkills();
  };

  const act = (fn: () => Promise<unknown>) => async () => {
    setActionError(undefined);
    // pause/resume flip source and trigger state too; refresh those now, not at the next poll
    try { await fn(); refresh(); } catch (e) { fail(e); }
  };
  const opts = (o?: (string | RecipeActionOption)[]): RecipeActionOption[] =>
    (o ?? []).map((x) => (typeof x === "string" ? { value: x } : x));
  const runActionWith = async (name: string, args: Record<string, unknown>) => {
    setActionBusy(name); setActionError(undefined); setActionMsg(undefined);
    try { const r = await api.projectAction(id, name, args); setActionMsg(r.message); reload(); }
    catch (e) { fail(e); }
    setActionBusy(undefined);
  };
  const runAction = (name: string, params?: Record<string, { options?: (string | RecipeActionOption)[] }>) => {
    const args: Record<string, unknown> = {};
    for (const [k, spec] of Object.entries(params ?? {})) args[k] = actionArgs[`${name}.${k}`] ?? opts(spec.options)[0]?.value;
    return runActionWith(name, args);
  };
  const repair = async (key: string) => {
    setBusyKey(key); setActionError(undefined);
    try { await api.repairProject(id, key); reload(); }
    catch (e) { fail(e); }
    setBusyKey(undefined);
  };
  const missing = s.objects.filter((o) => o.missing);

  const ctx: Ctx = {
    id, s, view, params, go, fail, refresh,
    sources, mySources, specs, triggers: myTriggers, triggersLoaded: triggers !== undefined,
    agentsData, agents: myAgents, agentNames, externals: externals?.agents, skills, mcpNames,
    dispatches, roster, slack: slack?.channels ?? [], projectName, otherProjects, channelLabel,
  };

  // What the template declares to do and to show: its actions (e.g. cause an incident) and its
  // label/value panels. They head Activity, where what an action sets off shows up.
  const templateActions = template?.actions && template.actions.length > 0 && (
    <div className="panel" style={{ marginBottom: 14 }}>
      {template.actions.some((a) => a.intro) && (
        <p className="help" style={{ margin: "0 0 10px" }}>
          {template.actions.find((a) => a.intro)?.intro}
          <HelpButton onClick={() => setActionHelp(true)} label="What do these do?" />
        </p>
      )}
      <div className="uc-actions">
        {template.actions.map((a) => (
          <span key={a.name} className="uc-actions" title={a.help}>
            {Object.entries(a.params ?? {}).map(([k, spec]) => {
              const o = opts(spec.options);
              if (!o.length) {
                const suggestions = k === "session" ? (s.sessions ?? []) : [];
                return (
                  <Combo key={k} value={actionArgs[`${a.name}.${k}`] ?? ""} placeholder={spec.label ?? k}
                         options={suggestions.map((x) => x.session)} style={{ width: 340 }}
                         hints={Object.fromEntries(suggestions.map((x) => [x.session,
                           [x.repo, x.branch, x.ended ? "ended" : "live"].filter(Boolean).join(" · ")]))}
                         onChange={(v) => setActionArgs({ ...actionArgs, [`${a.name}.${k}`]: v })} />
                );
              }
              return (
                <Picker key={k} value={actionArgs[`${a.name}.${k}`] ?? o[0]?.value ?? ""}
                        onChange={(v) => setActionArgs({ ...actionArgs, [`${a.name}.${k}`]: v })}
                        options={o.map((x) => x.value)}
                        labels={Object.fromEntries(o.map((x) => [x.value, x.label ?? x.value]))}
                        ariaLabel={spec.label ?? k} style={{ width: 200 }} />
              );
            })}
            <button className="primary" disabled={!!actionBusy || s.status !== "active"} onClick={() => runAction(a.name, a.params)}>
              {actionBusy === a.name ? "working…" : a.label}
            </button>
          </span>
        ))}
        {actionMsg && <span className="help">{actionMsg}</span>}
      </div>
      {(() => {
        const cur = template.actions.find((a) => a.params?.scenario);
        const o = cur ? opts(cur.params!.scenario.options) : [];
        const sel = cur ? (actionArgs[`${cur.name}.scenario`] ?? o[0]?.value) : undefined;
        const h = o.find((x) => x.value === sel)?.help;
        return h ? <p className="help" style={{ margin: "8px 0 0" }}>{h}</p> : null;
      })()}
    </div>
  );
  const templatePanels = (s.panels ?? []).length > 0 && (
    <div className="pv-panels">
      {(s.panels ?? []).map((pn) => (
        <div className="panel" key={pn.title}>
          <h3 style={{ margin: "0 0 8px" }}>{pn.title}</h3>
          <table className="pv-facts">
            <tbody>
              {pn.rows.map((row) => (
                <tr key={row.label}>
                  <td className="help">{row.label}</td>
                  <td className={row.mono ? "mono" : undefined} style={{ wordBreak: "break-all" }}>
                    {row.url
                      ? <a href={row.url} target="_blank" rel="noreferrer">{String(row.value)}</a>
                      : String(row.value)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );

  const main = (() => {
    switch (view.kind) {
      case "activity":
        return <>
          {templateActions}
          {templatePanels}
          <h2 style={{ margin: "0 0 10px" }}>Activity</h2>
          <ProjectActivity id={id} triggers={myTriggers.map((t) => t.name)} agents={agentNames}
                           onShowTrigger={(n) => go({ kind: "trigger", name: n })} />
        </>;
      case "events": return <EventsView key={mySources.map((x) => x.name).join(",")} ctx={ctx} />;
      case "sources": return <SourcesView ctx={ctx} />;
      case "source": return <SourceView ctx={ctx} name={view.name!} />;
      case "triggers": return <TriggersView ctx={ctx} />;
      case "trigger": return <TriggerView ctx={ctx} name={view.name!} />;
      case "agents": return <AgentsView ctx={ctx} />;
      case "agent": return <AgentView ctx={ctx} name={view.name!} />;
      case "external": return <ExternalView ctx={ctx} id={view.name!} />;
      case "skills": return <SkillsView ctx={ctx} />;
      case "skill": return <SkillView ctx={ctx} name={view.name!} />;
      case "sessions":
        return s.sessions ? (
          <SessionsPanel sessions={s.sessions} runs={s.runs} project={id}
                         onSummarize={s.status === "active"
                           ? (sid) => { setActionArgs({ ...actionArgs, "summarize.session": sid }); return runActionWith("summarize", { session: sid }); }
                           : undefined}
                         busy={actionBusy === "summarize"} message={actionMsg} />
        ) : null;
      case "settings":
        return view.name === "subscribers" ? <SubscribersView ctx={ctx} />
          : view.name === "keys" ? <KeysView ctx={ctx} />
          : view.name === "history" ? <HistoryView ctx={ctx} />
          : <McpView ctx={ctx} />;
    }
  })();

  return (
    <>
      <div className="pagehead">
        <div>
          <h1 style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {s.name}
            <span className={`badge ${s.status === "active" ? "ok" : s.status === "paused" ? "paused" : "error"}`}>{s.status}</span>
          </h1>
          <p className="subtitle">
            {s.template_title}
            {s.status === "paused" && (pausedSources.length
              ? ` · ${pausedSources.length === sourceCount ? "sources" : `${pausedSources.length} of ${sourceCount} sources`}, triggers and agents are off`
              : " · triggers and agents are off; sources keep ingesting")}
          </p>
        </div>
        <div className="btnrow">
          {s.status === "paused"
            ? <button onClick={act(() => api.resumeProject(id))}>Resume</button>
            : <button onClick={() => { setPauseSources(false); setConfirmPause(true); }}>Pause</button>}
          {!s.default && <Link className="btn" to={`/projects/new/${custom ? "custom" : encodeURIComponent(s.template)}?edit=${encodeURIComponent(id)}`}>Edit</Link>}
          {/* the Default project holds whatever was made outside a project; it is never deleted */}
          {!s.default && <button className="danger" onClick={() => { setPurge(false); setDelSources(new Set()); setConfirmDel(true); }}>Delete</button>}
        </div>
      </div>

      {actionError && <div className="alert error">{actionError}</div>}
      {s.last_error && s.status === "error" && (
        <div className="alert error"><strong>Last error</strong> · <span className="mono">{s.last_error}</span></div>
      )}
      {s.summary_error && <div className="alert warn">summary unavailable: <span className="mono">{s.summary_error}</span></div>}
      {missing.length > 0 && (
        <div className="alert warn">
          {missing.length} object{missing.length === 1 ? " was" : "s were"} deleted by hand.{" "}
          {custom
            ? "Edit the project to drop it from the list, or create it again under the same name."
            : <>Repair re-creates{missing.length === 1 ? " it" : " them"} from the plan; or leave as is if that was the intent.{" "}
                {missing.map((o) => (
                  <button key={o.key} onClick={() => repair(o.key)} disabled={busyKey === o.key} style={{ marginLeft: 6 }}>
                    {busyKey === o.key ? "repairing…" : `Repair ${o.name}`}</button>
                ))}</>}
        </div>
      )}

      <div className="cards">
        <div className="card"><div className="k">runs</div>
          <div className="v">{typeof s.runs_total === "number"
            ? <>{s.runs_total} {typeof s.runs_ok === "number" && s.runs_total > 0 && <small>{s.runs_ok} ok</small>}</>
            : <span className="dim">none</span>}</div></div>
        {(s.cards ?? []).map((c) => (
          <div className="card" key={c.label}><div className="k">{c.label}</div><div className="v">{String(c.value)}</div></div>
        ))}
        <div className="card"><div className="k">trigger last fired</div>
          <div className="v" style={{ fontSize: 15 }}>{s.trigger_last_fired ? <TimeAgo ts={s.trigger_last_fired} /> : <span className="dim">never</span>}</div></div>
      </div>

      <div className="project-body">
        <ProjectNav ctx={ctx} />
        <div className="pview" key={viewParam(view)}>{main}</div>
      </div>

      {actionHelp && template?.actions && (
        <InfoDialog title="What the actions do" onClose={() => setActionHelp(false)}>
          {template.actions.filter((a) => a.intro).map((a) => <p key={a.name} className="help" style={{ margin: 0 }}>{a.intro}</p>)}
          {template.actions.filter((a) => a.params?.scenario).map((a) => (
            <table key={a.name} className="perm-table">
              <thead><tr><th>scenario</th><th>what happens</th></tr></thead>
              <tbody>
                {opts(a.params!.scenario.options).map((x) => (
                  <tr key={x.value}><td className="mono">{x.value}</td><td>{x.help ?? ""}</td></tr>
                ))}
              </tbody>
            </table>
          ))}
          <ul className="help" style={{ margin: 0, paddingLeft: 18 }}>
            {template.actions.filter((a) => a.help).map((a) => <li key={a.name}><strong>{a.label}</strong>: {a.help}</li>)}
          </ul>
          {template.actions.find((a) => a.docs)?.docs && (
            <p className="help" style={{ margin: 0 }}>
              Walkthrough: <a href={template.actions.find((a) => a.docs)!.docs!.url} target="_blank" rel="noreferrer">{template.actions.find((a) => a.docs)!.docs!.label}</a>
            </p>
          )}
        </InfoDialog>
      )}

      {confirmPause && (
        <ConfirmDialog title={`Pause ${s.name}?`}
          message="Its triggers stop firing and its agents stop running. Sources keep ingesting unless you pause them too; resume brings back exactly what pause stopped."
          confirmLabel="Pause project"
          onConfirm={async () => { setConfirmPause(false); await act(() => api.pauseProject(id, pauseSources))(); }}
          onCancel={() => setConfirmPause(false)}>
          {sourceCount > 0 && (
            <label style={{ display: "block", marginTop: 8 }}>
              <input type="checkbox" checked={pauseSources} onChange={(e) => setPauseSources(e.target.checked)} />{" "}
              also pause its {sourceCount === 1 ? "source" : `${sourceCount} sources`}, so no data accumulates while it is paused
            </label>
          )}
        </ConfirmDialog>
      )}
      {confirmDel && (
        <ConfirmDialog title={`Delete project ${s.name}?`}
          message="Its triggers, agents and MCP servers are deleted with it. Its sources stay unless you tick them below. Events already stored stay unless you purge them."
          confirmLabel={delSources.size ? `Delete project and ${delSources.size} source${delSources.size === 1 ? "" : "s"}` : "Delete project"} danger
          onConfirm={async () => {
            try { await api.deleteProject(id, purge, [...delSources]); navigate("/projects", { replace: true }); }
            catch (e) { fail(e); setConfirmDel(false); }
          }}
          onCancel={() => setConfirmDel(false)}>
          {(myTriggers.length + agentNames.length + mcpNames.length) > 0 && (
            <div style={{ margin: "10px 0 4px" }}>
              <span className="lbl">deleted with it</span>
              {[...myTriggers.map((t) => ["trigger", t.name]), ...agentNames.map((n) => ["agent", n]),
                ...mcpNames.map((n) => ["MCP server", n])].map(([k, n]) => (
                <div key={`${k}:${n}`} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "2px 0", minWidth: 0 }}>
                  <span className="help" style={{ width: 76, flex: "0 0 auto" }}>{k}</span>
                  <span className="mono" style={{ minWidth: 0, wordBreak: "break-all" }}>{n}</span>
                </div>
              ))}
            </div>
          )}
          {mySources.length > 0 && (
            <div style={{ margin: "10px 0 4px" }}>
              <span className="lbl">its sources</span>
              {mySources.map((x) => {
                const others = otherProjects(x);
                return (
                  <label key={x.name} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "2px 0", minWidth: 0 }}>
                    <input type="checkbox" disabled={others.length > 0}
                           checked={!others.length && delSources.has(x.name)}
                           onChange={(e) => setDelSources((cur) => {
                             const next = new Set(cur);
                             if (e.target.checked) next.add(x.name); else next.delete(x.name);
                             return next;
                           })} />
                    <span className="mono" style={{ minWidth: 0, wordBreak: "break-all" }}>{x.name}</span>
                    {others.length > 0 && (
                      <span className="help">kept: also used by {others.map(projectName).join(", ")}</span>
                    )}
                  </label>
                );
              })}
              <p className="help" style={{ margin: "6px 0 0", whiteSpace: "normal" }}>
                Ticked sources are deleted; the rest stay connected. A source another project uses is always kept.
              </p>
            </div>
          )}
          <label style={{ display: "block", marginTop: 8 }}>
            <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} />{" "}
            also purge the events of the sources deleted and its triggers' firings
          </label>
        </ConfirmDialog>
      )}
    </>
  );
}
