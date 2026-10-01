import { useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, InternalName, TimeAgo, usePolling } from "../components/bits";
import { toolTitle } from "../components/setup/planEdit";
import type { ResourceKind, ResourceRow } from "../types";

// All resources (TR-351): every part on the cell, one table per kind, each row with the projects
// that use it. A second sidebar picks the kind. Read-only: it is where you check what is on the
// cell and who uses what (while parts move from one project each to shared between projects).

const KINDS: { kind: ResourceKind; label: string; what: string }[] = [
  { kind: "sources", label: "Sources", what: "Where events come from." },
  { kind: "triggers", label: "Wake-ups", what: "When something happens that wakes the agents (triggers)." },
  { kind: "agents", label: "Agents", what: "The Tares agents that look when a project wakes." },
  { kind: "tools", label: "Tools", what: "Outside services the agents can call (MCP servers)." },
  { kind: "skills", label: "Skills", what: "Know-how the agents read." },
  { kind: "keys", label: "Keys", what: "Keys for agents and services outside Tares." },
];

const enc = encodeURIComponent;

/** The row's plain title (a source's repo, a tool's service), or null when the name is all there is. */
function plainTitle(kind: ResourceKind, r: ResourceRow): string | null {
  const t = r.title ?? (kind === "tools" && r.url ? toolTitle(r.url).title : null);
  return t && t !== r.name ? t : null;
}

/** Where a row opens: the part's own page, in the first project that uses it when it has none. */
function linkFor(kind: ResourceKind, r: ResourceRow): string | null {
  const p = r.used_by[0]?.id;
  switch (kind) {
    case "sources": return `/sources/${enc(r.name)}`;
    case "triggers": return `/triggers/${enc(r.name)}`;
    case "agents": return `/agents/${enc(r.name)}`;
    case "tools": return p ? `/projects/${enc(p)}?view=settings:mcp` : null;
    case "skills": return p ? `/projects/${enc(p)}?view=skill:${enc(r.name)}` : null;
    case "keys": return p ? `/projects/${enc(p)}?view=settings:keys` : "/settings";
  }
}

function stateClass(state: string): string {
  if (["receiving", "active", "on", "set", "used"].includes(state)) return "ok";
  if (["error", "no credentials"].includes(state)) return "error";
  return "paused";
}

/** When it last did something, per kind. */
function lastAt(kind: ResourceKind, r: ResourceRow) {
  const ts = kind === "sources" ? r.last_event_at : kind === "triggers" ? r.last_fired_at
    : kind === "agents" ? r.last_run_at : kind === "keys" ? r.last_used_at : null;
  return ts ? <TimeAgo ts={ts} /> : <span className="dim">never</span>;
}
const LAST_LABEL: Partial<Record<ResourceKind, string>> = {
  sources: "last event", triggers: "last woke", agents: "last run", keys: "last used",
};

export default function Resources() {
  const { kind: raw } = useParams();
  const navigate = useNavigate();
  const kind: ResourceKind = (KINDS.find((k) => k.kind === raw)?.kind) ?? "sources";
  const { data, error, reload } = usePolling(() => api.resources(), 15000);
  const [filter, setFilter] = useState("");
  const [loose, setLoose] = useState(false);
  const meta = KINDS.find((k) => k.kind === kind)!;
  const rows = (data?.[kind] ?? []).filter((r) =>
    (!loose || r.used_by.length === 0)
    && (!filter.trim() || `${r.name} ${plainTitle(kind, r) ?? ""} ${r.what} ${r.used_by.map((u) => u.name).join(" ")}`
      .toLowerCase().includes(filter.trim().toLowerCase())));
  const last = LAST_LABEL[kind];

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>All resources</h1>
          <p className="subtitle">Every part on this Tares and the projects that use it.</p>
        </div>
      </div>
      {error && <ErrorState error={error} what="the resources" onRetry={reload} />}
      <div className="project-body">
        <nav className="pnav" aria-label="Kinds of resource">
          {KINDS.map((k) => (
            <NavLink key={k.kind} to={`/resources/${k.kind}`}
                     className={() => `pnav-link${k.kind === kind ? " active" : ""}`}>
              <span className="pnav-label">{k.label}</span>
              {data && <span className="pnav-hint">{data[k.kind].length}</span>}
            </NavLink>
          ))}
        </nav>
        <label className="pnav-select">
          <span className="lbl">show</span>
          <select value={kind} onChange={(e) => navigate(`/resources/${e.target.value}`)}>
            {KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </select>
        </label>

        <div className="pview">
          <div className="pv-head">
            <h2 style={{ margin: 0 }}>{meta.label}</h2>
            <p className="help" style={{ margin: "4px 0 0" }}>{meta.what}</p>
          </div>
          <div className="res-filters">
            <input type="search" placeholder="Filter by name, what it is or project" value={filter}
                   aria-label={`Filter the ${meta.label.toLowerCase()}`} onChange={(e) => setFilter(e.target.value)} />
            <label className="su-check">
              <input type="checkbox" checked={loose} onChange={(e) => setLoose(e.target.checked)} />
              <span>Only those in no project</span>
            </label>
          </div>
          {!data && !error && <div className="dim">loading…</div>}
          {data && rows.length === 0 && (
            <div className="empty">{filter || loose ? "Nothing matches." : `No ${meta.label.toLowerCase()} on this Tares yet.`}</div>
          )}
          {rows.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>name</th><th>what it is</th><th>state</th>{last && <th>{last}</th>}<th>used by</th></tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const to = linkFor(kind, r);
                    return (
                      <tr key={`${r.name}:${i}`}>
                        {(() => {
                          const title = plainTitle(kind, r);
                          if (!title) return <td className="mono res-name">{to ? <Link to={to}>{r.name}</Link> : r.name}</td>;
                          return <td className="res-name">{to ? <Link to={to}>{title}</Link> : title}<InternalName name={r.name} /></td>;
                        })()}
                        <td>{r.what}{r.detail && r.state !== "receiving" && <span className="help"> · {r.detail}</span>}</td>
                        <td><span className={`badge ${stateClass(r.state)}`}>{r.state}</span></td>
                        {last && <td style={{ whiteSpace: "nowrap" }}>{lastAt(kind, r)}</td>}
                        <td>
                          {r.used_by.length === 0 ? <span className="dim">no project</span>
                            : r.used_by.map((u, j) => (
                              <span key={u.id}>{j > 0 && ", "}<Link to={`/projects/${enc(u.id)}`}>{u.name}</Link></span>
                            ))}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
