import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, projectGoal, usePolling } from "../components/bits";
import { ReadyReminder } from "../components/readiness";
import { DeleteDraft } from "../components/setup/deleteDraft";
import type { Project } from "../types";

// Projects are the unit: a named set of sources, triggers, agents and MCP servers with one page.
// This page lists them; Create new (/projects/new) is the goal-first setup. The default project
// (whatever was made outside another project) is not listed as a project: when it holds
// something, one line under the list points to All resources. What is left of the Start list
// (the demo, model provider, GitHub, Slack) sits on top until done or hidden.

function statusClass(s: Project["status"]) {
  return s === "active" ? "ok" : s === "paused" || s === "draft" ? "paused" : "error";
}

export function projectKindCounts(u: Project) {
  const c: Record<string, number> = {};
  for (const o of u.objects) c[o.kind] = (c[o.kind] ?? 0) + 1;
  return c;
}

export default function Projects() {
  const { data: inst, error: instError, reload: reloadInst } = usePolling(() => api.projects(), 10000);
  const projects = (inst?.projects ?? []).filter((p) => !p.default);
  const loose = inst?.projects.find((p) => p.default)?.objects.length ?? 0;

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
        <table>
          <thead>
            <tr>
              <th>name</th><th>goal</th><th>status</th><th>objects</th><th>updated</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {projects.map((u) => {
              const c = projectKindCounts(u);
              const missing = u.objects.filter((o) => o.missing).length;
              // a draft opens where its setup stopped: nothing of it exists yet
              const to = u.status === "draft" ? `/projects/${encodeURIComponent(u.id)}/setup`
                : `/projects/${encodeURIComponent(u.id)}`;
              return (
                <tr key={u.id}>
                  <td>
                    <Link to={to}><strong>{u.name}</strong></Link>
                    {u.kind === "software_factory" && <span className="fo-tag" style={{ marginLeft: 8 }}>Software factory</span>}
                  </td>
                  <td className="help"><span className="proj-goal">{projectGoal(u)}</span></td>
                  <td>
                    <span className={`badge ${statusClass(u.status)}`}>{u.status}</span>
                    {missing > 0 && <span className="help" style={{ marginLeft: 6 }}>{missing} missing</span>}
                  </td>
                  <td className="help">
                    {u.status === "draft" ? "Not set up yet" : <>
                    {c.source ?? 0} source{c.source === 1 ? "" : "s"}
                    {c.trigger ? `, ${c.trigger} trigger${c.trigger === 1 ? "" : "s"}` : ""}
                    {c.agent ? `, ${c.agent} agent${c.agent === 1 ? "" : "s"}` : ""}
                    </>}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={u.updated_at} /></td>
                  <td>
                    <div className="btnrow" style={{ justifyContent: "flex-end" }}>
                      <Link className="btn" to={to}>{u.status === "draft" ? "Finish setting up" : "Open"}</Link>
                      {u.status === "draft" && <DeleteDraft id={u.id} name={u.name} onDeleted={reloadInst} />}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
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
