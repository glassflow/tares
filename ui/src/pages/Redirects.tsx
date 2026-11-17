import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";
import { api } from "../api";
import { useEdition } from "../editions/edition";
import { Button, EmptyState } from "../components/edition";
import SourceDetail from "./SourceDetail";

// The cell-wide Triggers, Tares agents, Firings and MCP servers pages are gone: every trigger,
// agent and MCP server belongs to one project, whose Advanced setup shows it. Their links still
// arrive (Slack posts and write-backs link /agents/<name> and /dispatches/<id>, older bookmarks
// the rest), so each one lands on the same thing inside its project.

const projectUrl = (project: string, view: string, extra: Record<string, string> = {}) => {
  const q = new URLSearchParams({ view, ...extra });
  return `/projects/${encodeURIComponent(project)}?${q.toString()}`;
};

// What each address resolves, in the page words (src/editions/words.json): a trigger is a rule (a
// check rule on sre), a firing is a notification (an alert on sre).
type Kind = "agent" | "rule" | "notification";
const NOUN: Record<Kind, { factory: string; sre: string }> = {
  agent: { factory: "agent", sre: "agent" },
  rule: { factory: "rule", sre: "check rule" },
  notification: { factory: "notification", sre: "alert" },
};

function Resolving({ find, what }: { find: () => Promise<string | null>; what: Kind }) {
  const { edition } = useEdition();
  const noun = NOUN[what][edition === "sre" ? "sre" : "factory"];
  const [to, setTo] = useState<string | null>();
  useEffect(() => {
    let live = true;
    find().then((u) => { if (live) setTo(u); }).catch(() => { if (live) setTo(null); });
    return () => { live = false; };
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  if (to) return <Navigate to={to} replace />;
  if (to === null) {
    return (
      <EmptyState title={`No such ${noun}`} data-testid="redirect-gone"
                  action={<Button variant="secondary" asChild><Link to="/projects">See the projects</Link></Button>}>
        {what === "notification" ? `Tares has no record of that ${noun}.` : `Tares has no ${noun} by that name any more.`}
      </EmptyState>
    );
  }
  return <p role="status" className="m-0 text-sm text-muted-foreground" data-testid="redirect-opening">Opening the {noun}</p>;
}

/** /agents/<name>[?run=…] -> the agent in its project's Advanced setup. */
export function AgentRedirect() {
  const { name = "" } = useParams();
  const q = new URLSearchParams(useLocation().search);
  return <Resolving what="agent" find={async () => {
    const { agents } = await api.builtinAgents();
    const a = agents.find((x) => x.name === name);
    if (!a?.project) return null;
    // The address parameter.
    const run = q.get("run");   // words-ok
    return projectUrl(a.project, `agent:${name}`, run ? { run } : {});
  }} />;
}

/** /triggers/<name> -> the trigger in its project's Advanced setup. */
export function TriggerRedirect() {
  const { name = "" } = useParams();
  return <Resolving what="rule" find={async () => {
    const t = (await api.triggers()).find((x) => x.name === name);
    // The project page reads view=trigger:<name>.
    return t?.project ? projectUrl(t.project, `trigger:${name}`) : null;   // words-ok
  }} />;
}

/** /dispatches/<id> (Slack's "Open in Tares") -> the firing's thread in its project's Activity. */
export function DispatchRedirect() {
  const { id = "" } = useParams();
  return <Resolving what="notification" find={async () => {
    const d = await api.dispatch(id);
    const t = (await api.triggers()).find((x) => x.name === d.trigger);
    return t?.project ? projectUrl(t.project, "activity", { thread: id }) : null;
  }} />;
}

/** /firings[?dispatch=…] -> that firing's thread, or the projects. */
export function FiringsRedirect() {
  const q = new URLSearchParams(useLocation().search);
  // The address parameter.
  const id = q.get("dispatch");   // words-ok
  return id ? <Navigate to={`/dispatches/${encodeURIComponent(id)}`} replace />
            : <Navigate to="/projects" replace />;
}

/** /agents/new and /triggers/new, with ?project=: the add form in that project; else the projects. */
export function NewInProjectRedirect({ kind }: { kind: "agents" | "triggers" }) {
  const q = new URLSearchParams(useLocation().search);
  const project = q.get("project");
  if (!project) return <Navigate to="/projects" replace />;
  // The address parameter.
  const trigger = q.get("trigger");   // words-ok
  return <Navigate to={projectUrl(project, kind, { add: "1", ...(trigger ? { trigger } : {}) })} replace />;
}

/** A source's page, drawn fresh for each name: going from one source straight to another
 *  (search, a link on a source page) must not keep the first one's data. */
export function SourceByName() {
  const { name = "" } = useParams();
  return <SourceDetail key={name} />;
}

/** Any address the console does not know. */
export function NotFound() {
  return (
    <EmptyState title="Page not found" data-testid="not-found"
                action={<Button variant="secondary" asChild><Link to="/projects">See the projects</Link></Button>}>
      This page does not exist. The address may be old, or mistyped.
    </EmptyState>
  );
}
