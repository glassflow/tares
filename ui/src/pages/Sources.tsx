import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { Search, Settings as SettingsIco } from "../components/icons";
import { ErrorState, Picker, StatusBadge, TimeAgo, formatBytes, usePolling } from "../components/bits";
import { ProjectLink } from "../components/ProjectBadge";

// Gear dropdown next to "Add source" — catalog import/export, each on its own page.
function CatalogMenu() {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ position: "relative", display: "inline-block" }}>
      <button className="btn" onClick={() => setOpen((o) => !o)} title="Import / export catalog"
              aria-label="Catalog menu" aria-haspopup="menu" aria-expanded={open}>
        <SettingsIco className="ico" />
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: "fixed", inset: 0, zIndex: 20 }} />
          <div className="menu-pop" role="menu"
               style={{ position: "absolute", right: 0, top: "calc(100% + 4px)", zIndex: 21 }}>
            <Link className="menu-item" role="menuitem" to="/sources/export" onClick={() => setOpen(false)}>Export catalog</Link>
            <Link className="menu-item" role="menuitem" to="/sources/import" onClick={() => setOpen(false)}>Import catalog</Link>
          </div>
        </>
      )}
    </div>
  );
}

// Just the source list. Instance-wide numbers (storage, totals, agent runs) used to sit at the
// bottom of this page; they now live on Overview (pages/Home.tsx), because on a real cell this
// list is long enough to push them below the fold — and they were never about the sources anyway.
export default function Sources() {
  const nav = useNavigate();
  const { data: sources, error } = usePolling(() => api.sources());
  // Host capabilities gate local-only actions: hide Auto-discover where Docker isn't reachable
  // (a hosted cell, or a local box without Docker). Shown until known false.
  const { data: caps } = usePolling(() => api.capabilities());
  // for the project filter: id -> name, and which ids own a source at all
  const { data: projectList } = usePolling(() => api.projects(), 30000);

  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const [connector, setConnector] = useState("all");
  const [project, setProject] = useState("all");

  const statuses = useMemo(
    () => Array.from(new Set((sources ?? []).map((s) => s.health?.status ?? "starting"))).sort(),
    [sources],
  );
  const connectors = useMemo(
    () => Array.from(new Set((sources ?? []).map((s) => s.connector))).sort(),
    [sources],
  );
  const projectName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of projectList?.projects ?? []) m[p.id] = p.name;
    return m;
  }, [projectList]);
  // a source can be in several projects: `projects` lists them all
  const memberOf = (s: { projects?: string[]; owned_by?: string | null }) =>
    s.projects ?? (s.owned_by ? [s.owned_by] : []);
  const owningProjects = useMemo(
    () => Array.from(new Set((sources ?? []).flatMap(memberOf))).sort(
      (a, b) => (projectName[a] ?? a).localeCompare(projectName[b] ?? b)),
    [sources, projectName],
  );

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (sources ?? []).filter((s) => {
      const st = s.health?.status ?? "starting";
      return (status === "all" || st === status) &&
        (connector === "all" || s.connector === connector) &&
        (project === "all" || (project === "none" ? !memberOf(s).length : memberOf(s).includes(project))) &&
        (!needle || s.name.toLowerCase().includes(needle) || s.connector.toLowerCase().includes(needle));
    });
  }, [sources, q, status, connector, project]);

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Sources</h1>
          <p className="subtitle">everything Tares ingests; <em>lossless, normalized, one project</em></p>
        </div>
        <span className="btnrow">
          {caps?.discover_docker !== false && (
            <Link to="/sources/discover" className="btn">✦ Auto-discover</Link>
          )}
          <Link to="/sources/new" className="btn primary">Add source</Link>
          <CatalogMenu />
        </span>
      </div>

      {error && <div className="alert error">daemon unreachable: {error}</div>}

      {sources && sources.length === 0 && (
        <div className="empty">
          No sources yet; <Link to="/sources/new">add one by hand</Link> or <Link to="/sources/import">import a catalog YAML</Link>
          {caps?.discover_docker !== false && (
            <>, or <Link to="/sources/discover">auto-discover from Docker</Link></>
          )}.
        </div>
      )}

      {sources && sources.length > 0 && (
        <>
          <div className="toolbar">
            <div className="search-box">
              <Search />
              <input
                type="text"
                className="search"
                placeholder="Filter sources…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            {/* Fixed width so the search box and count don't shift when you pick a status —
                the picker sizes to its selected text, unlike a native select. */}
            <Picker value={status} onChange={setStatus} ariaLabel="filter by status"
                    style={{ width: 150 }}
                    options={["all", ...statuses]}
                    labels={{ all: "All statuses" }} />
            <Picker value={connector} onChange={setConnector} ariaLabel="filter by connector"
                    style={{ width: 160 }}
                    options={["all", ...connectors]}
                    labels={{ all: "All connectors" }} />
            {owningProjects.length > 0 && (
              <Picker value={project} onChange={setProject} ariaLabel="filter by project"
                      style={{ width: 180 }}
                      options={["all", "none", ...owningProjects]}
                      labels={{ all: "All projects", none: "No project",
                                ...Object.fromEntries(owningProjects.map((id) => [id, projectName[id] ?? id])) }} />
            )}
            <span className="grow" />
            <span className="count">{shown.length} of {sources.length}</span>
          </div>

          <table>
            <thead>
              <tr>
                <th>name</th><th>projects</th><th>connector</th><th>status</th><th>poll</th>
                <th className="num">events</th><th>last ingest</th><th>last error</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => (
                <tr key={s.name} className="clickable" onClick={() => nav(`/sources/${s.name}`)}>
                  <td className="mono">{s.name}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {memberOf(s).length
                      ? memberOf(s).map((id, k) => <span key={id}>{k > 0 && ", "}<ProjectLink id={id} /></span>)
                      : <span className="dim">none</span>}
                  </td>
                  <td>{s.connector}</td>
                  <td><StatusBadge status={s.health?.status} /></td>
                  <td className="mono">{s.health?.status === "push" ? <span className="dim">push</span> : s.poll}</td>
                  <td className="num">{(s.health?.events_total ?? 0).toLocaleString()}</td>
                  <td><TimeAgo ts={s.health?.last_ingest} /></td>
                  <td className="mono" style={{ maxWidth: 260, color: "var(--err)" }}>
                    {s.health?.last_error ? s.health.last_error.slice(0, 80) : <span className="dim">—</span>}
                  </td>
                </tr>
              ))}
              {!shown.length && (
                <tr><td colSpan={8} className="dim" style={{ textAlign: "center", padding: 24 }}>
                  no sources match the current filter
                </td></tr>
              )}
            </tbody>
          </table>
        </>
      )}

    </>
  );
}
