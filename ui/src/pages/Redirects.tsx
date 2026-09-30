import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";
import { api } from "../api";

// The cell-wide Triggers, Tares agents, Firings and MCP servers pages are gone: every trigger,
// agent and MCP server belongs to one project, whose Advanced setup shows it. Their links still
// arrive (Slack posts and write-backs link /agents/<name> and /dispatches/<id>, older bookmarks
// the rest), so each one lands on the same thing inside its project.

const projectUrl = (project: string, view: string, extra: Record<string, string> = {}) => {
  const q = new URLSearchParams({ view, ...extra });
  return `/projects/${encodeURIComponent(project)}?${q.toString()}`;
};

function Resolving({ find, what }: { find: () => Promise<string | null>; what: string }) {
  const [to, setTo] = useState<string | null>();
  useEffect(() => {
    let live = true;
    find().then((u) => { if (live) setTo(u); }).catch(() => { if (live) setTo(null); });
    return () => { live = false; };
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  if (to) return <Navigate to={to} replace />;
  if (to === null) {
    return (
      <div className="empty">
        Tares has no {what} by that name any more. <Link to="/projects">See the projects</Link>.
      </div>
    );
  }
  return <p className="help">Opening {what}…</p>;
}

/** /agents/<name>[?run=…] -> the agent in its project's Advanced setup. */
export function AgentRedirect() {
  const { name = "" } = useParams();
  const q = new URLSearchParams(useLocation().search);
  return <Resolving what="that agent" find={async () => {
    const { agents } = await api.builtinAgents();
    const a = agents.find((x) => x.name === name);
    if (!a?.project) return null;
    const run = q.get("run");
    return projectUrl(a.project, `agent:${name}`, run ? { run } : {});
  }} />;
}

/** /triggers/<name> -> the trigger in its project's Advanced setup. */
export function TriggerRedirect() {
  const { name = "" } = useParams();
  return <Resolving what="that trigger" find={async () => {
    const t = (await api.triggers()).find((x) => x.name === name);
    return t?.project ? projectUrl(t.project, `trigger:${name}`) : null;
  }} />;
}

/** /dispatches/<id> (Slack's "Open in Tares") -> the firing's thread in its project's Activity. */
export function DispatchRedirect() {
  const { id = "" } = useParams();
  return <Resolving what="that firing" find={async () => {
    const d = await api.dispatch(id);
    const t = (await api.triggers()).find((x) => x.name === d.trigger);
    return t?.project ? projectUrl(t.project, "activity", { thread: id }) : null;
  }} />;
}

/** /firings[?dispatch=…] -> that firing's thread, or the projects. */
export function FiringsRedirect() {
  const q = new URLSearchParams(useLocation().search);
  const id = q.get("dispatch");
  return id ? <Navigate to={`/dispatches/${encodeURIComponent(id)}`} replace />
            : <Navigate to="/projects" replace />;
}

/** /agents/new and /triggers/new, with ?project=: the add form in that project; else the projects. */
export function NewInProjectRedirect({ kind }: { kind: "agents" | "triggers" }) {
  const q = new URLSearchParams(useLocation().search);
  const project = q.get("project");
  if (!project) return <Navigate to="/projects" replace />;
  const trigger = q.get("trigger");
  return <Navigate to={projectUrl(project, kind, { add: "1", ...(trigger ? { trigger } : {}) })} replace />;
}
