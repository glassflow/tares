import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import NewInProject from "../components/NewInProject";
import { Search } from "../components/icons";
import { EmptyState, ErrorState, Picker, TimeAgo, conditionText, usePolling } from "../components/bits";
import type { AgentInfo, DispatchLogEntry, Project, Trigger } from "../types";

// Every trigger across every project. A trigger is made inside a project, so "Add trigger" asks
// which one first; the list links each row to its project.

export default function TriggersPage() {
  const { data: triggers, error, reload } = usePolling(() => api.triggers(), 10000);
  const { data: projects } = usePolling(() => api.projects(), 30000);
  const { data: dispatches } = usePolling(() => api.dispatches(100), 15000);
  const { data: roster } = usePolling(() => api.agents(), 15000);
  const { data: slack } = usePolling(() => api.slackChannels(), 60000);

  return (
    <>
      <h1>Triggers</h1>
      <TriggersSection triggers={triggers ?? []} projects={projects?.projects ?? []}
                       dispatches={dispatches ?? []}
                       roster={roster?.agents ?? []} slackChannels={slack?.channels ?? []}
                       loadError={error} onChange={reload} />
    </>
  );
}

// ── triggers ─────────────────────────────────────────────────────────────────

function TriggersSection({ triggers, projects, dispatches, roster, slackChannels, loadError, onChange }:
  { triggers: Trigger[]; projects: Project[]; dispatches: DispatchLogEntry[];
    roster: AgentInfo[];
    slackChannels: { id: string; name: string; is_private: boolean }[];
    loadError?: string; onChange: () => void }) {
  const channelLabel = (raw: string) => {
    const cid = raw.replace(/^#/, "");
    const hit = slackChannels.find((c) => c.id === cid);
    return hit ? (hit.is_private ? `🔒 ${hit.name}` : `#${hit.name}`) : raw;
  };
  // Everything subscribed to a trigger, from the same roster the Deliveries page shows. Active
  // trigger + nobody = the warning; a paused trigger has its subscriptions parked by design.
  const subscribers = (name: string) => roster.filter((a) => a.triggers.includes(name));
  const SubscriberCell = ({ t }: { t: Trigger }) => {
    if (t.paused) return <span className="dim">paused</span>;
    const subs = subscribers(t.name);
    if (!subs.length) return <span className="badge error">nobody</span>;
    return (
      <>
        {subs.slice(0, 2).map((a) =>
          a.kind === "tares"
            ? <Link key={a.name} to={`/agents/${encodeURIComponent(a.name)}`} className="chip mono">{a.name}</Link>
            : a.kind === "slack"
              ? <span key={a.name} className="chip">{channelLabel(a.name)}</span>
              : <Link key={a.name} to={`/agents?agent=${encodeURIComponent(a.name)}`} className="chip mono">{a.name}</Link>)}
        {subs.length > 2 && (
          <span className="chip dim" title={subs.slice(2).map((a) => a.name).join(", ")}>
            +{subs.length - 2} more</span>
        )}
      </>
    );
  };
  // newest first, so the first hit per trigger is its latest firing; older than the fetched
  // window shows as a dash, which for a trigger list reads correctly as "not lately"
  const lastFired = (name: string) => dispatches.find((d) => d.trigger === name)?.fired_at ?? null;
  const [error, setError] = useState<string>();
  const [q, setQ] = useState("");
  const [project, setProject] = useState("all");
  const [confirmDelName, setConfirmDelName] = useState<string | null>(null);

  const projectName = (id?: string | null) => projects.find((p) => p.id === id)?.name ?? id ?? "";
  const projectOptions = useMemo(
    () => Array.from(new Set(triggers.map((t) => t.project ?? "").filter(Boolean))), [triggers]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return triggers.filter((t) =>
      (project === "all" || t.project === project) &&
      (!needle || t.name.toLowerCase().includes(needle)));
  }, [triggers, q, project]);

  const del = async (name: string) => {
    setError(undefined);
    try { await api.deleteTrigger(name); onChange(); }
    catch (e) { setError(String((e as Error).message ?? e)); }
  };

  const togglePause = async (t: Trigger) => {
    setError(undefined);
    try { await (t.paused ? api.resumeTrigger(t.name) : api.pauseTrigger(t.name)); onChange(); }
    catch (e) { setError(String((e as Error).message ?? e)); }
  };

  return (
    <>
      <div className="pagehead">
        <span />
        <NewInProject label="Add trigger" what="trigger"
                      to={(p) => `/triggers/new?project=${encodeURIComponent(p)}`} />
      </div>
      {error && <div className="alert error">{error}</div>}
      {loadError && <ErrorState error={loadError} what="triggers" onRetry={onChange} />}

      {!triggers.length && !loadError && (
        <EmptyState>no triggers; nothing wakes agents yet</EmptyState>
      )}
      {!!triggers.length && (
        <>
          <div className="toolbar">
            <div className="search-box">
              <Search />
              <input type="text" className="search" placeholder="Filter by name…" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <Picker value={project} onChange={setProject} ariaLabel="filter by project"
                    style={{ width: 200 }}
                    options={["all", ...projectOptions]}
                    labels={{ all: "All projects", ...Object.fromEntries(projectOptions.map((p) => [p, projectName(p)])) }} />
            <span className="grow" />
            <span className="count">{shown.length} of {triggers.length}</span>
          </div>
          <table>
            <thead><tr><th>name</th><th>project</th><th>sources</th><th>condition</th><th>cooldown</th><th>last fired</th><th>subscribers</th><th></th></tr></thead>
            <tbody>
              {shown.map((t) => (
                <tr key={t.name} style={t.paused ? { opacity: 0.55 } : undefined}>
                  <td className="mono">
                    <Link to={`/triggers/${encodeURIComponent(t.name)}`}>{t.name}</Link>
                    {t.paused && <span className="badge starting" style={{ marginLeft: 8 }} title="paused; not evaluated, never fires until resumed">paused</span>}
                  </td>
                  <td>{t.project
                    ? <Link to={`/projects/${encodeURIComponent(t.project)}`}>{projectName(t.project)}</Link>
                    : <span className="dim">none</span>}</td>
                  <td>
                    {(t.sources ?? []).slice(0, 2).map((s) => <span className="chip mono" key={s}>{s}</span>)}
                    {(t.sources ?? []).length > 2 && (
                      <span className="chip dim" title={t.sources.slice(2).join(", ")}>+{t.sources.length - 2} more</span>
                    )}
                  </td>
                  <td className="mono">
                    {conditionText(t.condition)}
                  </td>
                  <td className="mono">{t.condition.every ? "—" : t.cooldown}</td>
                  <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={lastFired(t.name)} /></td>
                  <td><SubscriberCell t={t} /></td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <span className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                      <Link className="btn" to={`/triggers/${encodeURIComponent(t.name)}`}>agents</Link>
                      <button onClick={() => togglePause(t)} title={t.paused ? "resume evaluation" : "stop evaluating and firing this trigger"}>{t.paused ? "resume" : "pause"}</button>
                      <Link className="btn" to={`/triggers/${encodeURIComponent(t.name)}?edit=1`}>edit</Link>
                      <button className="danger" onClick={() => setConfirmDelName(t.name)}>delete</button>
                    </span>
                  </td>
                </tr>
              ))}
              {!shown.length && <tr><td colSpan={8} className="dim" style={{ textAlign: "center", padding: 24 }}>no triggers match the filter</td></tr>}
            </tbody>
          </table>
        </>
      )}

      {confirmDelName && (
        <ConfirmDialog
          title={`Delete trigger "${confirmDelName}"?`}
          message="It will stop firing. This can't be undone."
          confirmLabel="Delete trigger" danger
          onCancel={() => setConfirmDelName(null)}
          onConfirm={() => { const n = confirmDelName; setConfirmDelName(null); del(n); }}
        />
      )}
    </>
  );
}
