import { useState } from "react";
import { Link } from "react-router-dom";

import type { api } from "../../api";
import { TimeAgo } from "../bits";
import type {
  BuiltinAgent, ConnectorSpec, DispatchLogEntry, ExternalAgent, ProjectSummary, SkillSummary, Source,
  SourceEvent, Trigger,
} from "../../types";

// What the project page shows in its main pane, and how that sits in the URL:
//   (no view)            the Overview: the goal, whether it works, what the agents found
//   ?view=result:<run>   one result: the next step, the note, how Tares got there
//   ?view=how            how this project works, in four plain steps
// and the full setup, the left-nav views for when something does not work as expected:
//   ?view=activity       a section: activity, events, sources, triggers, agents, skills, sessions
//   ?view=agent:<name>   one item: source:, trigger:, agent:, external:<subscription id>, skill:
//   ?view=settings:keys  one settings page: mcp, subscribers, keys, history
// plus `add=1` (or add=<trigger> on agents) to open a section's Add form, `run=` / `dispatch=`
// on an agent to open that run, and `thread=` on activity to open one thread.

export type ViewKind = "overview" | "result" | "how"
  | "activity" | "events" | "sources" | "source" | "triggers" | "trigger" | "agents"
  | "agent" | "external" | "skills" | "skill" | "sessions" | "settings";
export interface View { kind: ViewKind; name?: string }

const SECTIONS: ViewKind[] = ["overview", "how", "activity", "events", "sources", "triggers", "agents", "skills", "sessions"];
const ITEMS: ViewKind[] = ["result", "source", "trigger", "agent", "external", "skill"];
export const SETTINGS: [string, string][] = [
  ["mcp", "MCP servers"], ["subscribers", "Subscribers"], ["keys", "Keys"], ["history", "Change history"],
];

/** The views of the goal-first page; every other view is the full setup, with the left nav. */
export const isSetup = (v: View) => v.kind !== "overview" && v.kind !== "result" && v.kind !== "how";

/** A view named the way ?view= names it (the health API's `view`); undefined when it names none. */
export function viewFrom(raw: string | null | undefined, hasSessions: boolean): View | undefined {
  if (!raw) return undefined;
  const v = parseView(new URLSearchParams({ view: raw }), hasSessions);
  return v.kind === "overview" && raw !== "overview" ? undefined : v;
}

/** The view a URL asks for. Links from before the side navigation (?tab=) land on the nearest
 *  setup view; a bare project link lands on the Overview. */
export function parseView(p: URLSearchParams, hasSessions: boolean): View {
  const raw = p.get("view");
  if (raw) {
    const i = raw.indexOf(":");
    const kind = (i < 0 ? raw : raw.slice(0, i)) as ViewKind;
    const name = i < 0 ? undefined : raw.slice(i + 1);
    if (kind === "settings") return { kind, name: SETTINGS.some(([k]) => k === name) ? name : "mcp" };
    if (name && ITEMS.includes(kind)) return { kind, name };
    if (!name && SECTIONS.includes(kind) && (kind !== "sessions" || hasSessions)) return { kind };
  }
  switch (p.get("tab")) {
    case "setup": return { kind: "sources" };
    case "events": return { kind: "events" };
    case "agents": return { kind: "agents" };
    case "skills": return { kind: "skills" };
    case "sessions": if (hasSessions) return { kind: "sessions" }; break;
  }
  if (p.get("session") && hasSessions) return { kind: "sessions" };
  return { kind: "overview" };
}

export const viewParam = (v: View) => (v.name ? `${v.kind}:${v.name}` : v.kind);
export const sameView = (a: View, b: View) => a.kind === b.kind && (a.name ?? "") === (b.name ?? "");

/** The search string for a view; the Overview is the bare page. */
export function viewSearch(v: View, extra?: Record<string, string>): string {
  const parts: string[] = [];
  if (v.kind !== "overview") {
    parts.push(`view=${v.kind}${v.name ? `:${encodeURIComponent(v.name)}` : ""}`);
  }
  for (const [k, val] of Object.entries(extra ?? {})) parts.push(`${k}=${encodeURIComponent(val)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

/** A link to a view of this project page. */
export function VLink({ v, extra, className, children, title, ariaLabel, ariaCurrent }: {
  v: View; extra?: Record<string, string>; className?: string; children: React.ReactNode;
  title?: string; ariaLabel?: string; ariaCurrent?: boolean;
}) {
  return (
    <Link to={{ search: viewSearch(v, extra) }} className={className} title={title}
          aria-label={ariaLabel} aria-current={ariaCurrent ? "page" : undefined}>{children}</Link>
  );
}

type Api = typeof api;
export type AgentsData = Awaited<ReturnType<Api["builtinAgents"]>>;
export type Roster = Awaited<ReturnType<Api["agents"]>>;
export type Mcp = Awaited<ReturnType<Api["mcpServers"]>>;
export type SlackChannels = Awaited<ReturnType<Api["slackChannels"]>>["channels"];

/** Everything the views share: the project, its objects as the live APIs report them, and how to
 *  move between views and refresh. Loaded once by the page so the nav and the views agree. */
export interface Ctx {
  id: string;
  s: ProjectSummary;
  view: View;
  params: URLSearchParams;
  go: (v: View, extra?: Record<string, string>, replace?: boolean) => void;
  fail: (e: unknown) => void;
  refresh: () => void;                    // everything: summary, sources, triggers, agents, ...
  sources: Source[] | undefined;          // every source of the instance
  mySources: Source[];
  specs: Record<string, ConnectorSpec> | undefined;
  triggers: Trigger[];                    // this project's
  triggersLoaded: boolean;
  agentsData: AgentsData | undefined;
  agents: BuiltinAgent[];                 // this project's Tares agents
  agentNames: string[];
  externals: ExternalAgent[] | undefined; // undefined while loading
  skills: SkillSummary[] | undefined;
  channelLabel: (raw: string) => string;  // a Slack channel id as its #name, when the bot sees it
  mcpNames: string[];
  dispatches: DispatchLogEntry[] | undefined;
  roster: Roster | undefined;
  slack: SlackChannels;
  projectName: (id: string) => string;
  otherProjects: (x: Source) => string[];
}

/** The health of a source as one word: what the nav's dot and the tables' badge say. */
export function sourceState(x: Source): "paused" | "error" | "ok" {
  return x.paused ? "paused" : x.health?.last_error ? "error" : "ok";
}

export function SourceBadge({ x }: { x: Source }) {
  const st = sourceState(x);
  return st === "paused" ? <span className="badge paused">paused</span>
    : st === "error" ? <span className="badge error" title={x.health?.last_error ?? undefined}>error</span>
    : <span className="badge ok">ok</span>;
}

/** Stored events, newest first; a long one opens in place. */
export function EventsTable({ events, showSource = true }: { events: SourceEvent[]; showSource?: boolean }) {
  const [openEvent, setOpenEvent] = useState<number>();
  return (
    <table>
      <thead><tr><th style={{ width: 110 }}>when</th>{showSource && <th>source</th>}<th>entity</th><th>type</th><th>event</th></tr></thead>
      <tbody>
        {events.map((e, i) => {
          const long = e.text.length > 160;
          const open = openEvent === i;
          return (
            <tr key={i} className={long ? "clickable" : undefined}
                onClick={() => long && setOpenEvent(open ? undefined : i)}
                title={long && !open ? "click for the whole event" : undefined}>
              <td style={{ whiteSpace: "nowrap", verticalAlign: "top" }}><TimeAgo ts={e.ingest_time} /></td>
              {showSource && (
                <td style={{ verticalAlign: "top" }} onClick={(ev) => ev.stopPropagation()}>
                  <VLink v={{ kind: "source", name: e.source }} className="mono">{e.source}</VLink></td>
              )}
              <td className="mono" style={{ verticalAlign: "top" }}>{e.key}</td>
              <td style={{ verticalAlign: "top" }}><span className="chip">{e.event_type}</span></td>
              <td className="mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {open || !long ? e.text : e.text.slice(0, 160)}
                {long && !open && <span className="dim"> … ▸</span>}
                {open && <span className="dim"> ▾</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** A small key/value table, the shape every item view opens with. */
export function Facts({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <table className="pv-facts">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}><td className="help">{k}</td><td>{v}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

/** The heading row of a view: the title, what it is, and its actions. */
export function ViewHead({ title, sub, children }: {
  title: React.ReactNode; sub?: React.ReactNode; children?: React.ReactNode;
}) {
  return (
    <div className="pagehead pv-head">
      <div style={{ minWidth: 0 }}>
        <h2 style={{ margin: 0 }}>{title}</h2>
        {sub && <p className="help" style={{ margin: "4px 0 0", whiteSpace: "normal" }}>{sub}</p>}
      </div>
      {children && <span className="btnrow">{children}</span>}
    </div>
  );
}

/** The item a view names is gone (deleted, renamed, or never in this project). */
export function NotHere({ what, name, back }: { what: string; name: string; back: View }) {
  return (
    <div className="empty">
      There is no {what} named <span className="mono">{name}</span> in this project.{" "}
      <VLink v={back}>See all of them</VLink>.
    </div>
  );
}
