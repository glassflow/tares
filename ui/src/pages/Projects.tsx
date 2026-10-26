import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, projectGoal, usePolling } from "../components/bits";
import { Bolt, Search } from "../components/icons";
import { ProgressRing, progressText } from "../components/ProgressRing";
import { ReadyReminder } from "../components/readiness";
import { DeleteDraft } from "../components/setup/deleteDraft";
import type { Project } from "../types";
import {
  SHOWS, SORTS, matchesQuery, matchesShow, pendingWork, sortProjects,
  type Show, type Sort,
} from "./projectsList";

// Projects are the unit: a named set of sources, triggers, agents and MCP servers with one page.
// This page lists them; Create new (/projects/new) is the goal-first setup. The default project
// (whatever was made outside another project) is not listed as a project: when it holds
// something, one line under the list points to All resources. What is left of the Start list
// (the demo, model provider, GitHub, Slack) sits on top until done or hidden.
//
// The list is a work queue, not an inventory: done projects are hidden until asked for, the name
// is the link, the goal is a tooltip (or a line under the name on request), and the filter, sort
// and search sit in the address so a view can be shared and survives a reload.

const STATUS_WORD: Record<Project["status"], string> = {
  active: "Active", done: "Done", paused: "Paused", error: "Needs attention", draft: "Draft",
};

const SHOW_LABEL: Record<Show, string> = { active: "Active", done: "Done", all: "All" };

function pick<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.find((a) => a === value) ?? fallback;
}

function StatusCell({ u }: { u: Project }) {
  const word = STATUS_WORD[u.status];
  const missing = u.objects.filter((o) => o.missing).length;
  const label = u.progress ? `${word}. ${progressText(u.progress)}` : word;
  return (
    <span className={`pj-status s-${u.status}`} role="img" aria-label={label} title={label}>
      <span className="pj-dot" />
      {u.progress && <ProgressRing progress={u.progress} />}
      {u.progress && u.progress.total > 0 && (
        <span className="pj-count" aria-hidden="true">{u.progress.done}/{u.progress.total}</span>
      )}
      {missing > 0 && <span className="help" aria-hidden="true">{missing} missing</span>}
    </span>
  );
}

function PendingCell({ u }: { u: Project }) {
  if (u.status === "draft") return <span className="dim">Not set up yet</span>;
  const g = u.progress;
  if (!g || g.total === 0) return <span className="dim">No tickets</span>;
  const n = pendingWork(u);
  return (
    <span className="pj-pending">
      <span className={n ? "pj-n" : "dim"}>{n || "None"}</span>
      {g.doing > 0 && <span className="pj-sub doing">{g.doing} in progress</span>}
      {g.blocked > 0 && <span className="pj-sub blocked">{g.blocked} blocked</span>}
    </span>
  );
}

export default function Projects() {
  const { data: inst, error: instError, reload: reloadInst } = usePolling(() => api.projects(), 10000);
  const [params, setParams] = useSearchParams();
  const show = pick(params.get("show"), SHOWS, "active");
  const sort = pick(params.get("sort"), SORTS.map((s) => s.key), "recent");
  const query = params.get("q") ?? "";
  const goals = params.get("goals") === "1";
  const set = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) v ? next.set(k, v) : next.delete(k);
    setParams(next, { replace: true });
  };

  const projects = (inst?.projects ?? []).filter((p) => !p.default);
  const loose = inst?.projects.find((p) => p.default)?.objects.length ?? 0;
  const counts: Record<Show, number> = {
    all: projects.length,
    done: projects.filter((p) => p.status === "done").length,
    active: projects.filter((p) => p.status !== "done").length,
  };
  const shown = sortProjects(projects.filter((p) => matchesShow(p, show) && matchesQuery(p, query)), sort);
  const hiddenDone = show === "active" ? counts.done : 0;
  const factory = shown.some((p) => p.kind === "software_factory");

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Projects</h1>
          <p className="subtitle">
            Each project is a goal and the sources, wake-ups and agents that work toward it.
          </p>
        </div>
        <span className="btnrow">
          <Link className="btn primary" to="/projects/new">Create new</Link>
        </span>
      </div>

      <ReadyReminder />

      {instError && <ErrorState error={instError} what="your projects" onRetry={reloadInst} />}
      {!inst && !instError && <div className="dim">loading…</div>}

      {inst && projects.length === 0 && (
        <div className="empty">
          No projects yet. <Link to="/start">Start here</Link>: say what your first one should
          achieve and Tares plans it with you.
        </div>
      )}

      {projects.length > 0 && (
        <>
          <div className="pj-bar" role="toolbar" aria-label="Filter and sort projects">
            <div className="seg" role="group" aria-label="Show">
              {SHOWS.map((s) => (
                <button key={s} type="button" className={show === s ? "active" : ""} aria-pressed={show === s}
                        data-testid={`show-${s}`} onClick={() => set({ show: s === "active" ? "" : s })}>
                  {SHOW_LABEL[s]} <span className="pj-badge">{counts[s]}</span>
                </button>
              ))}
            </div>
            <label className="pj-search">
              <Search />
              <input type="text" placeholder="Filter projects" aria-label="Filter projects by name or goal"
                     value={query} onChange={(e) => set({ q: e.target.value })} />
            </label>
            <span className="pj-bar-end">
              <label className="pj-sort">
                <span className="help">Sort</span>
                <select aria-label="Sort projects" value={sort} data-testid="sort"
                        onChange={(e) => set({ sort: e.target.value === "recent" ? "" : e.target.value })}>
                  {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
                </select>
              </label>
              <button type="button" className={`pj-toggle${goals ? " active" : ""}`} aria-pressed={goals}
                      data-testid="goals-toggle" onClick={() => set({ goals: goals ? "" : "1" })}>
                Goals
              </button>
            </span>
          </div>

          {shown.length === 0 ? (
            <div className="empty" data-testid="no-match">
              {query ? <>No project matches “{query}”.</>
                : show === "done" ? <>No project is done yet.</>
                : <>Every project is done.</>}{" "}
              {(show !== "all" || query) && (
                <button type="button" className="linklike" onClick={() => set({ show: "all", q: "" })}>
                  Show all projects
                </button>
              )}
            </div>
          ) : (
            <div className="pj-scroll">
            <table className="pj-table">
              <thead>
                <tr>
                  <th>name</th><th>status</th><th>pending</th><th>updated</th>
                  <th aria-label="actions" />
                </tr>
              </thead>
              <tbody>
                {shown.map((u) => {
                  // a draft opens where its setup stopped: nothing of it exists yet
                  const to = u.status === "draft" ? `/projects/${encodeURIComponent(u.id)}/setup`
                    : `/projects/${encodeURIComponent(u.id)}`;
                  const goal = projectGoal(u);
                  return (
                    <tr key={u.id} data-testid="project-row" data-status={u.status}>
                      <td className="pj-name-cell">
                        <div className="pj-name-row">
                          <Link to={to} className="pj-name" title={`${u.name}\n${goal}`}><strong>{u.name}</strong></Link>
                          {u.kind === "software_factory" && (
                            <span className="pj-chip" title="Software factory project: a crew builds it from tickets"
                                  aria-label="Software factory" data-testid="factory-chip">
                              <Bolt /><span className="pj-chip-label">Factory</span>
                            </span>
                          )}
                        </div>
                        {goals && <div className="pj-goal help" data-testid="goal-line">{goal}</div>}
                      </td>
                      <td><StatusCell u={u} /></td>
                      <td><PendingCell u={u} /></td>
                      <td className="pj-updated"><TimeAgo ts={u.activity_at ?? u.updated_at} /></td>
                      <td className="pj-actions">
                        {u.status === "draft" && <DeleteDraft id={u.id} name={u.name} onDeleted={reloadInst} />}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          )}

          <p className="help pj-foot">
            {hiddenDone > 0 && !query && (
              <>
                {hiddenDone} done project{hiddenDone === 1 ? " is" : "s are"} hidden.{" "}
                <button type="button" className="linklike" data-testid="show-done-link" onClick={() => set({ show: "all" })}>
                  Show {hiddenDone === 1 ? "it" : "them"}
                </button>.{" "}
              </>
            )}
            {factory && (
              <span className="pj-legend">
                <span className="pj-chip"><Bolt /><span>Factory</span></span> a software factory project, built
                by the crew from tickets. Hover a name for its goal.
              </span>
            )}
          </p>
        </>
      )}

      {loose > 0 && (
        <p className="help">
          {loose} part{loose === 1 ? " is" : "s are"} not in any project.{" "}
          <Link to="/resources">See {loose === 1 ? "it" : "them"} in All resources</Link>.
        </p>
      )}
    </>
  );
}
