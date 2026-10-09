import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { Picker, TimeAgo, usePolling } from "../components/bits";
import { ProjectLink } from "../components/ProjectBadge";
import type { Project } from "../types";

// The MCP connections registry: external tool servers a Tares agent can opt into. This page owns
// the connection (URL + credential, entered once); which agent uses which server is chosen on the
// agent's own form. HTTP transport only — stdio would mean the daemon spawning arbitrary
// commands, which is off the table on a hosted cell and a footgun anywhere.

type Server = { name: string; url: string; auth_header: string;
                auth_value_configured: boolean; auth_credential: string;
                headers: Record<string, string>; updated_at: string;
                owned_by?: string | null; customized?: boolean; project?: string };
type Tool = { name: string; description: string };

const headersToText = (h: Record<string, string> | undefined) =>
  Object.entries(h ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n");
const textToHeaders = (t: string): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const line of t.split("\n")) {
    const i = line.indexOf(":");
    if (i <= 0) continue;
    const k = line.slice(0, i).trim(); const v = line.slice(i + 1).trim();
    if (k) out[k] = v;
  }
  return out;
};

/** Add or edit one server. A server belongs to one project (its agents may use it); `project` fixes
 *  it (a project's page), otherwise a new server asks, the Default project preselected. */
export function ServerForm({ initial, project, onSaved, onCancel }: {
  initial?: Server; project?: string; onSaved: () => void; onCancel: () => void;
}) {
  const isNew = !initial;
  const [projects, setProjects] = useState<Project[]>([]);
  const [proj, setProj] = useState(project ?? initial?.project ?? "");
  useEffect(() => {
    if (!isNew || project) return;
    let live = true;
    api.projects().then((r) => {
      if (!live) return;
      setProjects(r.projects);
      setProj((cur) => cur || (r.projects.find((p) => p.default) ?? r.projects[0])?.id || "");
    }).catch(() => {});
    return () => { live = false; };
  }, [isNew, project]);
  const [name, setName] = useState(initial?.name ?? "");
  const [url, setUrl] = useState(initial?.url ?? "");
  const [header, setHeader] = useState(initial?.auth_header ?? "");
  const [value, setValue] = useState("");
  // auth can be a literal header value, or a stored GitHub credential (Settings > GitHub) that
  // the daemon resolves at connect time, so rotating the token there rotates this server too
  const [credential, setCredential] = useState(initial?.auth_credential ?? "");
  const [credentials, setCredentials] = useState<string[]>([]);
  const [headersText, setHeadersText] = useState(headersToText(initial?.headers));
  const [advanced, setAdvanced] = useState(!!initial && Object.keys(initial.headers ?? {}).length > 0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    api.githubCredentials().then((r) => setCredentials(r.credentials.map((c) => c.name))).catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true); setErr(undefined);
    const body = { name: name.trim(), url: url.trim(),
                   auth_header: credential ? "" : header.trim(),
                   auth_value: credential ? `credential:github/${credential}` : value,
                   headers: textToHeaders(headersText),
                   ...(proj ? { project: proj } : {}) };
    try {
      if (isNew) await api.createMcpServer(body);
      else await api.updateMcpServer(initial!.name, body);
      onSaved();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <div className="panel">
      {err && <div className="alert error">{err}</div>}
      {isNew && !project && projects.length > 0 && (
        <div className="field" style={{ maxWidth: 340 }}>
          <span className="lbl">project</span>
          <Picker value={proj} onChange={setProj} ariaLabel="project"
                  options={projects.map((p) => p.id)}
                  labels={Object.fromEntries(projects.map((p) => [p.id, p.default ? `${p.name} (default)` : p.name]))} />
          <span className="help">agents of this project can use it</span>
        </div>
      )}
      <div className="row2">
        <label className="field">
          <span className="lbl">name</span>
          <input type="text" value={name} placeholder="e.g. github" disabled={!isNew}
                 onChange={(e) => setName(e.target.value)} />
          {!isNew && <span className="help">fixed; delete and recreate to rename</span>}
        </label>
        <label className="field">
          <span className="lbl">url</span>
          <input type="text" className="mono" value={url}
                 placeholder="https://mcp.example.com/mcp"
                 onChange={(e) => setUrl(e.target.value)} />
          <span className="help">stdio servers are not supported</span>
        </label>
      </div>
      <div className="field">
        <span className="lbl">authentication <span className="help">(optional)</span></span>
        {credentials.length > 0 && (
          <div style={{ marginBottom: 6 }}>
            <select value={credential} onChange={(e) => setCredential(e.target.value)}>
              <option value="">a header value entered here</option>
              {credentials.map((c) => (
                <option key={c} value={c}>GitHub credential: {c}</option>
              ))}
            </select>
            <span className="help" style={{ marginLeft: 8 }}>
              a stored credential sends <code>Authorization: Bearer &lt;token&gt;</code> and follows rotations
            </span>
          </div>
        )}
        {!credential && (
          <div className="hook-group">
            <input type="text" className="mono" placeholder="header name (default: Authorization)"
                   value={header} onChange={(e) => setHeader(e.target.value)} />
            <input type="password" className="mono" autoComplete="new-password" placeholder={
              initial?.auth_value_configured && !initial?.auth_credential
                ? "•••• configured, leave blank to keep"
                : "header value, e.g. Bearer sk-…"}
                   value={value} onChange={(e) => setValue(e.target.value)} />
          </div>
        )}
        {!credential && <span className="help">stored as a secret, never shown again</span>}
      </div>
      <details className="fld-group adv-group" open={advanced}
               onToggle={(e) => setAdvanced((e.target as HTMLDetailsElement).open)}>
        <summary>Advanced: extra headers<span className="caret">›</span></summary>
        <div>
          <label className="field">
            <span className="lbl">extra headers <span className="help">(one per line, name: value)</span></span>
            <textarea className="mono" rows={3} value={headersText}
                      placeholder={"X-MCP-Toolsets: repos,pull_requests\nX-MCP-Readonly: true"}
                      onChange={(e) => setHeadersText(e.target.value)} />
            <span className="help">
              sent on every request; not secrets. GitHub's hosted MCP uses <code>X-MCP-Toolsets</code> to
              limit which tools it offers and <code>X-MCP-Readonly</code> for read-only access
            </span>
          </label>
        </div>
      </details>
      <div className="btnrow">
        <button className="primary" onClick={save}
                disabled={busy || !name.trim() || !url.trim()}>
          {isNew ? "Add server" : "Save changes"}
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

export default function McpServers() {
  const { data, error, reload } = usePolling(() => api.mcpServers(), 30000);
  const [editing, setEditing] = useState<Server | "new">();
  const [tests, setTests] = useState<Record<string, { busy?: boolean; ok?: boolean;
                                                     error?: string; tools?: Tool[] }>>({});
  const [open, setOpen] = useState<string>();

  const servers = useMemo(() => data?.servers ?? [], [data]);

  const test = async (name: string) => {
    setTests((t) => ({ ...t, [name]: { busy: true } }));
    setOpen(name);
    try {
      const r = await api.testMcpServer(name);
      setTests((t) => ({ ...t, [name]: { ok: r.ok, error: r.error, tools: r.tools } }));
    } catch (e) {
      setTests((t) => ({ ...t, [name]: { ok: false, error: String((e as Error).message ?? e) } }));
    }
  };

  const remove = async (name: string) => {
    try { await api.deleteMcpServer(name); reload(); }
    catch { /* the next poll shows the truth */ }
  };

  return (
    <>
      <h1>MCP servers</h1>
      <p className="subtitle">
        Outside MCP servers, connected once. Pick them for a Tares agent to give it those tools.
      </p>

      {error && <div className="alert error">{error}</div>}

      {editing !== undefined ? (
        <ServerForm initial={editing === "new" ? undefined : editing}
                    onSaved={() => { setEditing(undefined); reload(); }}
                    onCancel={() => setEditing(undefined)} />
      ) : (
        <div className="btnrow" style={{ marginBottom: 12 }}>
          <button className="primary" onClick={() => setEditing("new")}>Add server</button>
        </div>
      )}

      {!data ? <div className="dim">loading…</div>
        : servers.length === 0 ? (
          <div className="empty">
            no servers yet. Add one, then pick it on an agent's form to give that agent its tools.
          </div>
        ) : (
          <table>
            <thead><tr><th>name</th><th>project</th><th>url</th><th>auth</th><th>updated</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {servers.map((s) => {
                const t = tests[s.name];
                return (
                  <>
                    <tr key={s.name}>
                      <td className="mono"><strong>{s.name}</strong></td>
                      <td><ProjectLink id={s.project ?? s.owned_by} /></td>
                      <td className="mono">{s.url}</td>
                      <td>{s.auth_credential
                        ? <span className="badge ok">GitHub credential {s.auth_credential}</span>
                        : s.auth_value_configured
                        ? <span className="badge ok">{s.auth_header || "Authorization"}</span>
                        : <span className="dim">none</span>}
                        {Object.keys(s.headers ?? {}).length > 0 && (
                          <span className="help" style={{ marginLeft: 6 }}>
                            +{Object.keys(s.headers).length} header{Object.keys(s.headers).length === 1 ? "" : "s"}
                          </span>
                        )}</td>
                      <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={s.updated_at} /></td>
                      <td>
                        <div className="btnrow" style={{ justifyContent: "flex-end" }}>
                          <button onClick={() => test(s.name)} disabled={t?.busy}>
                            {t?.busy ? "testing…" : "Test"}
                          </button>
                          <button onClick={() => setEditing(s)}>Edit</button>
                          <button className="danger" onClick={() => remove(s.name)}>Delete</button>
                        </div>
                      </td>
                    </tr>
                    {open === s.name && t && !t.busy && (
                      <tr key={s.name + "-test"}>
                        <td colSpan={6} style={{ background: "var(--wash)" }}>
                          {t.ok ? (
                            <div style={{ padding: "6px 4px" }}>
                              <span className="badge ok">connected</span>{" "}
                              <span className="help">{t.tools?.length ?? 0} tools</span>
                              <ul style={{ margin: "6px 0 2px", paddingLeft: 22 }}>
                                {(t.tools ?? []).map((tool) => (
                                  <li key={tool.name}>
                                    <span className="mono">{tool.name}</span>
                                    {tool.description && (
                                      <span className="help"> {tool.description.slice(0, 140)}</span>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          ) : (
                            <div style={{ padding: "6px 4px" }}>
                              <span className="badge error">failed</span>{" "}
                              <span className="help mono">{t.error}</span>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        )}
    </>
  );
}
