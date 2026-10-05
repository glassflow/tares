import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { cloudLink, useCloud, useWorkspaceOverview, type Cloud } from "../cloud";
import ConfirmDialog from "./ConfirmDialog";
import { usePolling } from "./bits";
import type { Project } from "../types";

// What a new cell has ready and what helps next, read from the state it is in: a model provider
// (on Tares Cloud the included credit), GitHub and Slack. The Start page shows the whole list;
// Projects shows what is left as one line until it is done or dismissed. Nothing here stores
// anything: a row is done because the thing it names exists.

/** Projects the person made: not the demo, not the default project, not a draft still being
 *  planned. undefined while loading. */
export function ownProjects(projects: Project[] | undefined): Project[] | undefined {
  return projects?.filter((p) => p.template !== "ai_sre_demo" && !p.default && p.status !== "draft");
}

export function useOwnProjects(intervalMs = 30000) {
  const { data, reload } = usePolling(() => api.projects(), intervalMs);
  return { projects: data?.projects, own: ownProjects(data?.projects), reload };
}

export type ReadyRow = {
  key: "model" | "github" | "slack";
  done: boolean;
  /** its state is not known yet: the row stands in its place, saying so, until it is */
  pending?: boolean;
  title: string;
  text: string;
  /** what to do about it: a button, a link, or the reason the person cannot */
  action: () => React.ReactNode;
};

const usd = (n: number) => `$${n.toFixed(2)}`;

/** The rows, each in its place from the first render: one whose state is not known yet says
 *  "Checking…" until its answer arrives, so the list never shows as an empty box on a slow cell.
 *  `back` is the page a Tares Cloud connect returns to. */
export function useReadiness(back: string): { rows: ReadyRow[]; loading: boolean } {
  const cloud = useCloud();
  const { data: prov } = usePolling(() => api.providers(), 30000);
  const { data: gh } = usePolling(() => api.githubCredentials(), 30000);
  const { data: slack } = usePolling(() => api.slackTokenStatus(), 30000);
  const ov = useWorkspaceOverview(cloud.health?.workspace_api_url || undefined);
  const trial = ov.result?.status === "ok" ? ov.result.data.trial : undefined;

  const rows: ReadyRow[] = [];
  rows.push(prov ? modelRow(prov.providers, prov.default, trial) : checking("model", "Model provider"));
  rows.push(gh ? githubRow(gh.credentials.map((c) => c.account).filter(Boolean), cloud, back)
    : checking("github", "GitHub"));
  rows.push(slack ? slackRow(slack.configured, slack.team?.name, cloud, back) : checking("slack", "Slack"));
  return { rows, loading: !prov || !gh || !slack };
}

const checking = (key: ReadyRow["key"], title: string): ReadyRow =>
  ({ key, done: false, pending: true, title, text: "Checking…", action: () => null });

function modelRow(providers: { id: string; name: string; configured: boolean }[], dflt: string | null,
                  trial: { state: string; spend_usd: number | null; credit_usd: number } | undefined): ReadyRow {
  const settings = "/settings?tab=anthropic";
  if (trial?.state === "exhausted") {
    return { key: "model", done: false, title: "Add a model provider",
             text: `The included ${usd(trial.credit_usd)} of Anthropic credit is used up. Add your own key so agents keep running.`,
             action: () => <Link className="btn primary" to={settings}>Add a model key</Link> };
  }
  if (trial?.state === "active") {
    return { key: "model", done: true, title: "Model provider ready",
             text: `${usd(trial.credit_usd)} of included Anthropic credit, ${usd(trial.spend_usd ?? 0)} used. Agents and Ask run on it.`,
             action: () => <Link className="btn link-btn" to={settings}>Use your own key</Link> };
  }
  const on = providers.filter((p) => p.configured);
  if (on.length) {
    const name = (on.find((p) => p.id === dflt) ?? on[0]).name;
    return { key: "model", done: true, title: "Model provider ready",
             text: `${name}. Agents and Ask run on it.`,
             action: () => <Link className="btn link-btn" to={settings}>Change</Link> };
  }
  return { key: "model", done: false, title: "Add a model provider",
           text: "Agents and the planner need one: Anthropic, OpenAI, or any OpenAI-compatible endpoint.",
           action: () => <Link className="btn primary" to={settings}>Add a model provider</Link> };
}

/** Connect on Tares Cloud (a control-plane link that comes back to `back`), or the Settings tab. */
function connectAction(what: "GitHub" | "Slack", url: string | undefined | null, cloud: Cloud,
                       back: string): React.ReactNode {
  const tab = what === "GitHub" ? "github" : "slack";
  if (url) {
    if (cloud.isOwner === false) {
      const email = cloud.current?.owner_email;
      return <span className="help">Only the workspace owner{email ? <>, <strong>{email}</strong>,</> : ""} can connect {what}.</span>;
    }
    return <a className="btn" href={cloudLink(url, {}, tab, back)}>Connect {what}</a>;
  }
  return <Link className="btn" to={`/settings?tab=${tab}`}>Connect {what}</Link>;
}

function githubRow(accounts: string[], cloud: Cloud, back: string): ReadyRow {
  if (accounts.length) {
    return { key: "github", done: true, title: "GitHub connected",
             text: `${[...new Set(accounts)].join(", ")}. Tares reads your repositories and suggests projects from them.`,
             action: () => <Link className="btn link-btn" to="/settings?tab=github">Manage</Link> };
  }
  return { key: "github", done: false, title: "Connect GitHub",
           text: "Tares reads your repositories and suggests a first project from them.",
           action: () => connectAction("GitHub", cloud.health?.github_connect_url, cloud, back) };
}

function slackRow(configured: boolean, team: string | undefined, cloud: Cloud, back: string): ReadyRow {
  if (configured) {
    return { key: "slack", done: true, title: "Slack connected",
             text: `${team ? `${team}. ` : ""}Agents post what they find to the channels you pick.`,
             action: () => <Link className="btn link-btn" to="/settings?tab=slack">Manage</Link> };
  }
  return { key: "slack", done: false, title: "Connect Slack",
           text: "Agents post what they find to a channel you pick.",
           action: () => connectAction("Slack", cloud.health?.slack_connect_url, cloud, back) };
}

/** One row of the list: a mark, what it is, and what to do. */
export function ReadyItem({ done, pending, title, text, action }: {
  done: boolean; pending?: boolean; title: string; text: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <li className={"ready-row" + (done ? " done" : "") + (pending ? " pending" : "")} aria-busy={pending || undefined}>
      <span className="ready-mark" aria-hidden="true">{done ? "✓" : ""}</span>
      <span className="ready-text">
        <strong>{title}</strong>
        <span className="help">{text}</span>
      </span>
      {action && <span className="ready-action">{action}</span>}
      {!pending && <span className="sr-only">{done ? "done" : "not done yet"}</span>}
    </li>
  );
}

const DISMISS_KEY = "tares_ready_dismissed";
const dismissed = () => { try { return localStorage.getItem(DISMISS_KEY) === "1"; } catch { return false; } };

/** The list on Projects: the same rows as Start (the demo, model provider, GitHub, Slack), done
 *  ones ticked, until model provider, GitHub and Slack are done (the demo never counts) or the
 *  person hides it. Dismissing is remembered in this
 *  browser only (a per-viewer convenience). */
export function ReadyReminder() {
  const { rows, loading } = useReadiness("/projects");
  const { projects, reload } = useOwnProjects();
  const [hidden, setHidden] = useState(dismissed);
  // shown once every row has answered: a list that may turn out finished must not flash up
  if (hidden || loading || rows.every((r) => r.done)) return null;
  return (
    <section className="ready-box" aria-label="Still to set up">
      <ul className="ready-list">
        <DemoRow projects={projects} onChange={reload} />
        {rows.map((r) => <ReadyItem key={r.key} done={r.done} pending={r.pending} title={r.title} text={r.text} action={r.action()} />)}
      </ul>
      <button type="button" className="dim ready-dismiss"
              onClick={() => { try { localStorage.setItem(DISMISS_KEY, "1"); } catch { /* private mode */ } setHidden(true); }}>
        Hide this list
      </button>
    </section>
  );
}

/** The demo, first on the list: an offer, never started on its own. One click creates it from
 *  what detection finds (on Tares Cloud the hosted demo stack) and opens its page; when the stack
 *  is not reachable, the step-by-step setup takes over with the steps to start it. */
export function DemoRow({ projects, onChange }: { projects: Project[] | undefined; onChange: () => void }) {
  const navigate = useNavigate();
  const { data: rec } = usePolling(() => api.templates(), 60000);
  const template = rec?.templates.find((t) => t.key === "ai_sre_demo");
  const demo = projects?.find((p) => p.template === "ai_sre_demo");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [confirm, setConfirm] = useState(false);
  // the templates or the projects not in yet: its place, saying so; no demo on this cell: no row
  if (!rec || !projects) return <ReadyItem done={false} pending title="Watch an agent handle an incident" text="Checking…" />;
  if (!template) return null;

  const start = async () => {
    setBusy(true); setErr(undefined);
    try {
      const d = await api.detectRecipe(template.key);
      const required = Object.entries(template.params).filter(([, p]) => p.required).map(([k]) => k);
      if (required.some((k) => d.params[k] === undefined || d.params[k] === "")) {
        navigate(`/projects/new/${template.key}`);
        return;
      }
      const made = await api.createProject({ template: template.key, params: d.params });
      navigate(`/projects/${encodeURIComponent(made.id)}`);
    } catch (e) {
      setErr(String((e as Error).message ?? e));
      setBusy(false);
    }
  };
  // the demo's own sources go with it; one another project uses is kept (the daemon decides)
  const remove = async () => {
    if (!demo) return;
    setConfirm(false); setBusy(true); setErr(undefined);
    try {
      await api.deleteProject(demo.id, true, demo.objects.filter((o) => o.kind === "source").map((o) => o.name));
      onChange();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const text = err
    ? <>Could not start the demo: {err}. <Link to={`/projects/new/${template.key}`}>Set it up step by step</Link> instead.</>
    : demo
      ? "Cause an incident from its page and watch the agent write the incident note."
      : "A small live service, an agent watching it, and a button to break it. Ready in a few seconds.";
  return (
    <>
      <ReadyItem done={!!demo} title={demo ? "Demo running" : "Watch an agent handle an incident"} text={text}
                 action={demo
                   ? <span className="btnrow">
                       <Link className="btn" to={`/projects/${encodeURIComponent(demo.id)}`}>Open the demo</Link>
                       <button type="button" className="dim" disabled={busy} onClick={() => setConfirm(true)}>Remove</button>
                     </span>
                   : <button type="button" disabled={busy} onClick={start}>{busy ? "Starting…" : "Start the demo"}</button>} />
      {confirm && (
        <ConfirmDialog title="Remove the demo?" danger confirmLabel="Remove"
                       message="Its agent stops, and its sources and their events are deleted. You can start it again from here."
                       onConfirm={remove} onCancel={() => setConfirm(false)} />
      )}
    </>
  );
}
