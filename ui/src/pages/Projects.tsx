import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorState, TimeAgo, projectGoal, usePolling } from "../components/bits";
import { DeleteDraft } from "../components/setup/deleteDraft";
import type { Project } from "../types";

// Projects are the unit: a named set of sources, triggers, agents and MCP servers with one page.
// The Default project holds whatever was made outside another project. This page lists them; Create new (/projects/new) holds the template gallery.

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
  const projects = inst?.projects ?? [];

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Projects</h1>
          <p className="subtitle">
            Each project is a goal and the sources, wake-ups and agents that work toward it. The
            Default project holds whatever was made outside another one.
          </p>
        </div>
        <span className="btnrow">
          <Link className="btn primary" to="/projects/new">Create new</Link>
        </span>
      </div>

      {instError && <ErrorState error={instError} what="your projects" onRetry={reloadInst} />}
      {!inst && !instError && <div className="dim">loading…</div>}

      {inst && projects.length === 0 && (
        <div className="empty">
          no projects yet · <Link to="/projects/new">create one</Link> from a template or from
          objects you already have
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
                  <td><Link to={to}><strong>{u.name}</strong></Link>
                    {u.default && <span className="chip" style={{ marginLeft: 8 }}>default</span>}</td>
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
    </>
  );
}
