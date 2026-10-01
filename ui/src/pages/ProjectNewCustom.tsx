import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { Combo } from "../components/bits";
import type { McpServer, Project, ProjectObjectKind } from "../types";

// A project assembled by hand: name it, pick the objects it is made of, Start. Nothing is created.
// Sources are shared, so any source can be picked. A trigger, agent or MCP server belongs to one
// project: the ones still in the Default project (or in none) can be moved here. Edit reopens this
// page with the current list; removing an object from the project hands it back, it is never
// deleted.

type Pick = { kind: ProjectObjectKind; name: string };
type Choice = { name: string; ownedBy: string | null | undefined; missing?: boolean };

const KINDS: { kind: ProjectObjectKind; label: string }[] = [
  { kind: "source", label: "Sources" },
  { kind: "trigger", label: "Triggers" },
  { kind: "agent", label: "Tares agents" },
  { kind: "mcp_server", label: "MCP servers" },
];

const keyOf = (p: Pick) => `${p.kind}:${p.name}`;

export default function ProjectNewCustom() {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const editId = search.get("edit");
  const [existing, setExisting] = useState<Project>();
  const [name, setName] = useState("");
  const [choices, setChoices] = useState<Record<ProjectObjectKind, Choice[]>>();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState<Partial<Record<ProjectObjectKind, string>>>({});
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [sources, triggers, agents, mcp, current] = await Promise.all([
          api.sources(), api.triggers(), api.builtinAgents(), api.mcpServers(),
          editId ? api.project(editId) : Promise.resolve(undefined),
        ]);
        if (cancelled) return;
        const mine = current?.id;
        // every part on the cell can be used (P-TR-216: parts are shared between projects)
        const any = (rows: { name: string; owned_by?: string | null }[]) =>
          rows.map((r) => ({ name: r.name, ownedBy: r.owned_by }));
        const byKind: Record<ProjectObjectKind, Choice[]> = {
          source: any(sources), trigger: any(triggers),
          agent: any(agents.agents), mcp_server: any(mcp.servers as McpServer[]),
        };
        // an object of this project that was deleted by hand is still on its list: show it as
        // missing so saving does not silently drop it
        for (const o of current?.objects ?? []) {
          if (!byKind[o.kind].some((c) => c.name === o.name)) byKind[o.kind].push({ name: o.name, ownedBy: mine, missing: true });
        }
        for (const k of KINDS) byKind[k.kind].sort((a, b) => a.name.localeCompare(b.name));
        setChoices(byKind);
        if (current) {
          setExisting(current);
          setName(current.name);
          setPicked(new Set(current.objects.map((o) => keyOf({ kind: o.kind, name: o.name }))));
        }
      } catch (e) {
        if (!cancelled) setErr(String((e as Error).message ?? e));
      }
    })();
    return () => { cancelled = true; };
  }, [editId]);

  const total = useMemo(() => choices ? KINDS.reduce((n, k) => n + choices[k.kind].length, 0) : 0, [choices]);

  const toggle = (p: Pick) => {
    const next = new Set(picked);
    const k = keyOf(p);
    if (next.has(k)) next.delete(k); else next.add(k);
    setPicked(next);
  };

  const submit = async () => {
    setBusy(true); setErr(undefined);
    const objects: Pick[] = [];
    for (const k of KINDS) for (const c of choices?.[k.kind] ?? []) {
      if (picked.has(keyOf({ kind: k.kind, name: c.name }))) objects.push({ kind: k.kind, name: c.name });
    }
    try {
      if (existing) {
        await api.updateProject(existing.id, { objects, name: name.trim() || undefined });
        navigate(`/projects/${encodeURIComponent(existing.id)}`);
      } else {
        const created = await api.createProject({ template: "custom", name: name.trim() || undefined, objects });
        navigate(`/projects/${encodeURIComponent(created.id)}`);
      }
    } catch (e) {
      setErr(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>{existing ? `Edit ${existing.name}` : "From existing objects"}</h1>
          <p className="subtitle">
            A project assembled from sources, triggers, agents and MCP servers you already have.
            Nothing is created; the project page shows their runs and firings. Sources can be in several
            projects; a trigger, agent or MCP server moves here from the Default project.
          </p>
        </div>
      </div>
      {err && <div className="alert error">{err}</div>}
      <div className="panel">
        <label className="field">
          <span className="lbl">name<span className="req"> *</span></span>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="what this project is for" required />
        </label>
        {!choices && !err && <div className="dim">loading…</div>}
        {choices && total === 0 && (
          <div className="empty">Nothing to pick yet. Connect a source first.</div>
        )}
        {choices && KINDS.map((k) => {
          const all = choices[k.kind];
          if (all.length === 0) return null;
          const chosen = all.filter((c) => picked.has(keyOf({ kind: k.kind, name: c.name })));
          const free = all.filter((c) => !picked.has(keyOf({ kind: k.kind, name: c.name }))).map((c) => c.name);
          return (
            <div className="field" key={k.kind}>
              <span className="lbl">{k.label}</span>
              {chosen.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
                  {chosen.map((c) => (
                    <span key={c.name} className="chip">
                      <span className="mono">{c.name}</span>
                      {c.missing && <span className="badge error" style={{ marginLeft: 6 }}>missing</span>}
                      <button type="button" aria-label={`remove ${c.name}`} title="remove from the project"
                              onClick={() => toggle({ kind: k.kind, name: c.name })}
                              style={{ border: 0, background: "none", cursor: "pointer", padding: "0 0 0 6px" }}>×</button>
                    </span>
                  ))}
                </div>
              )}
              {free.length > 0 ? (
                <Combo value={adding[k.kind] ?? ""} options={free}
                       placeholder={`add a ${k.label.toLowerCase().replace(/s$/, "")}…`}
                       onChange={(v) => {
                         if (free.includes(v)) { toggle({ kind: k.kind, name: v }); setAdding({ ...adding, [k.kind]: "" }); }
                         else setAdding({ ...adding, [k.kind]: v });
                       }} />
              ) : chosen.length === 0 ? null : <span className="help">all of them are in the project</span>}
            </div>
          );
        })}
        <div className="btnrow">
          <button className="primary" disabled={busy || picked.size === 0 || !name.trim()} onClick={submit}>
            {busy ? (existing ? "saving…" : "starting…") : existing ? "Save" : "Start"}
          </button>
          <Link className="btn" to={existing ? `/projects/${encodeURIComponent(existing.id)}` : "/projects"}>Cancel</Link>
        </div>
      </div>
    </>
  );
}
