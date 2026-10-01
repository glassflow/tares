import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Combo, Picker, TimeAgo, usePolling } from "../components/bits";
import { SessionsPanel } from "../components/ChallengerSessions";
import ProjectActivity from "../components/ProjectActivity";
import ProjectNav from "../components/project/ProjectNav";
import { isSetup, parseView, viewParam, viewSearch, type Ctx, type View } from "../components/project/common";
import { HowView, Overview, ResultView } from "../components/project/overview";
import { EventsView, SourceView, SourcesView } from "../components/project/sources";
import { TriggerView, TriggersView } from "../components/project/triggers";
import { AgentView, AgentsView, ExternalView } from "../components/project/agents";
import { SkillView, SkillsView } from "../components/project/skills";
import { HistoryView, KeysView, McpView, SubscribersView } from "../components/project/settings";
import type { ProjectSummary, Template, RecipeAction, RecipeActionOption, Source } from "../types";

// The page for every project, driven by the live APIs plus the template's summary. It lands on the
// goal-first Overview (the goal, whether the project works, what the agents found); a result and
// "How it works" open from there, all without the left nav. "Open the full setup" is the second
// mode: the header (name, status, Pause/Edit/Delete, the counters) over two columns, the project's
// own navigation on the left and one view on the right, for when something does not work. The
// view lives in the URL (see components/project/common.tsx), so links and the back button work.
// Template-specific content arrives as data (actions, facts, panels, cards), never as
// template-specific markup: actions sit in the Overview header, panels on How it works.

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
  const [actionOpen, setActionOpen] = useState<RecipeAction>();   // an action with choices, asking for them
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
  // this project's sources another project uses as well: pausing them stops both
  const sharedSources = (sources ?? [])
    .filter((x) => (x.projects ?? []).includes(id) && (x.projects ?? []).some((p) => p !== id))
    .map((x) => x.name);
  const { data: specs } = usePolling(() => api.connectors(), 600000);
  const { data: triggers, reload: reloadTriggers } = usePolling(() => api.triggers(), 10000);
  // the agents as this project wires them (P-TR-216: trigger, handoffs and on/off are its own)
  const { data: agentsData, reload: reloadAgents } = usePolling(() => api.builtinAgents(id), 10000);
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
    ...(agentsData?.agents ?? []).filter((a) => (a.projects ?? []).includes(id)).map((a) => a.name)])];
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

  // What the template declares to do and to show: its actions (e.g. cause an incident) sit in the
  // Overview header next to Pause, where what they set off shows up as results; its label/value
  // panels sit on How it works. An action with choices asks for them in a dialog first.
  const actionFields = (a: RecipeAction) => Object.entries(a.params ?? {}).map(([k, spec]) => {
    const o = opts(spec.options);
    if (!o.length) {
      const suggestions = k === "session" ? (s.sessions ?? []) : [];
      return (
        <label className="field" key={k}>
          <span className="lbl">{spec.label ?? k}</span>
          <Combo value={actionArgs[`${a.name}.${k}`] ?? ""} placeholder={spec.label ?? k}
                 options={suggestions.map((x) => x.session)} style={{ width: "100%" }}
                 hints={Object.fromEntries(suggestions.map((x) => [x.session,
                   [x.repo, x.branch, x.ended ? "ended" : "live"].filter(Boolean).join(" · ")]))}
                 onChange={(v) => setActionArgs({ ...actionArgs, [`${a.name}.${k}`]: v })} />
        </label>
      );
    }
    const sel = actionArgs[`${a.name}.${k}`] ?? o[0]?.value ?? "";
    const h = o.find((x) => x.value === sel)?.help;
    return (
      <div className="field" key={k}>
        <span className="lbl">{spec.label ?? k}</span>
        <Picker value={sel}
                onChange={(v) => setActionArgs({ ...actionArgs, [`${a.name}.${k}`]: v })}
                options={o.map((x) => x.value)}
                labels={Object.fromEntries(o.map((x) => [x.value, x.label ?? x.value]))}
                ariaLabel={spec.label ?? k} style={{ width: "100%" }} />
        {h && <span className="help">{h}</span>}
      </div>
    );
  });
  const templateActions = (template?.actions ?? []).map((a) => {
    const asks = Object.keys(a.params ?? {}).length > 0;
    return (
      <button key={a.name} type="button" title={a.help}
              disabled={!!actionBusy || s.status !== "active"}
              aria-haspopup={asks ? "dialog" : undefined}
              onClick={() => (asks ? setActionOpen(a) : runAction(a.name, a.params))}>
        {actionBusy === a.name ? "Working…" : a.label}
      </button>
    );
  });
  const pauseButton = s.status === "paused"
    ? <button type="button" onClick={act(() => api.resumeProject(id))}>Resume</button>
    : <button type="button" onClick={() => { setPauseSources(false); setConfirmPause(true); }}>Pause</button>;
  // Edit and Delete: on How it works and in the full setup's header
  const manage = <>
    {!s.default && <Link className="btn" to={`/projects/new/${custom ? "custom" : encodeURIComponent(s.template)}?edit=${encodeURIComponent(id)}`}>Edit</Link>}
    {/* the Default project holds whatever was made outside a project; it is never deleted */}
    {!s.default && <button type="button" className="danger" onClick={() => { setPurge(false); setDelSources(new Set()); setConfirmDel(true); }}>Delete</button>}
  </>;
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
                  <td className={row.mono ? "mono" : undefined} style={{ overflowWrap: "anywhere" }}>
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
      case "overview":
      case "result":
      case "how":
        return null;   // the goal-first views render without the setup's frame, below
      case "activity":
        return <>
          <h2 style={{ margin: "0 0 10px" }}>Activity</h2>
          <ProjectActivity id={id} triggers={myTriggers.map((t) => t.name)} agents={agentNames}
                           onShowTrigger={(n) => go({ kind: "trigger", name: n })}
                           openThread={params.get("thread") ?? undefined} />
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

  // what needs saying on every view: a failed action, the project's own error, objects deleted by hand
  const alerts = <>
    {actionError && <div className="alert error">{actionError}</div>}
    {actionMsg && view.kind !== "sessions" && (
      <div className="alert ok" role="status">
        {actionMsg}{" "}
        <button type="button" className="linklike" onClick={() => setActionMsg(undefined)}>Dismiss</button>
      </div>
    )}
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
  </>;

  const page = !isSetup(view) ? (
    <>
      {alerts}
      {view.kind === "overview" && (
        <Overview ctx={ctx} actions={<>{pauseButton}{templateActions}</>}
                  onResume={act(() => api.resumeProject(id))} />
      )}
      {view.kind === "result" && <ResultView key={view.name} ctx={ctx} runId={view.name!} />}
      {view.kind === "how" && <HowView ctx={ctx} panels={templatePanels || null} manage={manage} />}
    </>
  ) : (
    <>
      <div className="pagehead">
        <div>
          <h1 style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {s.name}
            <span className={`badge ${s.status === "active" ? "ok" : s.status === "paused" ? "paused" : "error"}`}>{s.status}</span>
          </h1>
          <p className="subtitle">
            Advanced setup: every source, trigger, agent run, firing and raw event.
            {s.status === "paused" && (pausedSources.length
              ? ` Paused: ${pausedSources.length === sourceCount ? "sources" : `${pausedSources.length} of ${sourceCount} sources`}, triggers and agents are off.`
              : " Paused: triggers and agents are off; sources keep ingesting.")}
          </p>
        </div>
        <div className="btnrow">
          {pauseButton}
          {manage}
        </div>
      </div>

      {alerts}

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
    </>
  );

  return (
    <>
      {page}

      {actionOpen && (
        <ConfirmDialog title={actionOpen.label} message={actionOpen.intro ?? actionOpen.help}
          confirmLabel={actionOpen.label}
          onConfirm={() => { const a = actionOpen; setActionOpen(undefined); runAction(a.name, a.params); }}
          onCancel={() => setActionOpen(undefined)}>
          <div style={{ marginTop: 12 }}>{actionFields(actionOpen)}</div>
          {actionOpen.docs && (
            <p className="help" style={{ margin: 0 }}>
              Walkthrough: <a href={actionOpen.docs.url} target="_blank" rel="noreferrer">{actionOpen.docs.label}</a>
            </p>
          )}
        </ConfirmDialog>
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
          {pauseSources && sharedSources.length > 0 && (
            <p className="alert warn" style={{ marginTop: 8 }}>
              {sharedSources.join(", ")} {sharedSources.length === 1 ? "is" : "are"} also used by
              another project. Pausing {sharedSources.length === 1 ? "it" : "them"} stops that
              project's data too.
            </p>
          )}
        </ConfirmDialog>
      )}
      {confirmDel && (
        <ConfirmDialog title={`Delete project ${s.name}?`}
          message="Its triggers, agents, MCP servers and skills are deleted with it. Its sources stay unless you tick them below. Events already stored stay unless you purge them."
          confirmLabel={delSources.size ? `Delete project and ${delSources.size} source${delSources.size === 1 ? "" : "s"}` : "Delete project"} danger
          onConfirm={async () => {
            try { await api.deleteProject(id, purge, [...delSources]); navigate("/projects", { replace: true }); }
            catch (e) { fail(e); setConfirmDel(false); }
          }}
          onCancel={() => setConfirmDel(false)}>
          {(myTriggers.length + agentNames.length + mcpNames.length + (skills?.length ?? 0)) > 0 && (
            <div style={{ margin: "10px 0 4px" }}>
              <span className="lbl">deleted with it</span>
              {[...myTriggers.map((t) => ["trigger", t.name]), ...agentNames.map((n) => ["agent", n]),
                ...mcpNames.map((n) => ["MCP server", n]),
                ...(skills ?? []).map((k) => ["skill", k.name])].map(([k, n]) => (
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
                    {/* the name keeps its width; the note beside it wraps (a squeezed name broke mid-word) */}
                    <span className="mono" style={{ flex: "0 0 auto", maxWidth: "55%", overflowWrap: "anywhere" }}>{x.name}</span>
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
