import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { cloudLink, useCloud, useWorkspaceOverview, type Cloud } from "../cloud";
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
  title: string;
  text: string;
  /** what to do about it: a button, a link, or the reason the person cannot */
  action: () => React.ReactNode;
};

const usd = (n: number) => `$${n.toFixed(2)}`;

/** The rows, once each has answered (a row whose state is not known yet is left out). `back` is
 *  the page a Tares Cloud connect returns to. */
export function useReadiness(back: string): { rows: ReadyRow[]; loading: boolean } {
  const cloud = useCloud();
  const { data: prov } = usePolling(() => api.providers(), 30000);
  const { data: gh } = usePolling(() => api.githubCredentials(), 30000);
  const { data: slack } = usePolling(() => api.slackTokenStatus(), 30000);
  const ov = useWorkspaceOverview(cloud.health?.workspace_api_url || undefined);
  const trial = ov.result?.status === "ok" ? ov.result.data.trial : undefined;

  const rows: ReadyRow[] = [];
  if (prov) rows.push(modelRow(prov.providers, prov.default, trial));
  if (gh) rows.push(githubRow(gh.credentials.map((c) => c.account).filter(Boolean), cloud, back));
  if (slack) rows.push(slackRow(slack.configured, slack.team?.name, cloud, back));
  return { rows, loading: !prov || !gh || !slack };
}

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
export function ReadyItem({ done, title, text, action }: {
  done: boolean; title: string; text: React.ReactNode; action?: React.ReactNode;
}) {
  return (
    <li className={"ready-row" + (done ? " done" : "")}>
      <span className="ready-mark" aria-hidden="true">{done ? "✓" : ""}</span>
      <span className="ready-text">
        <strong>{title}</strong>
        <span className="help">{text}</span>
      </span>
      {action && <span className="ready-action">{action}</span>}
      <span className="sr-only">{done ? "done" : "not done yet"}</span>
    </li>
  );
}

const DISMISS_KEY = "tares_ready_dismissed";
const dismissed = () => { try { return localStorage.getItem(DISMISS_KEY) === "1"; } catch { return false; } };

/** The list on Projects: the same rows as Start (model provider, GitHub, Slack), done ones
 *  ticked, until every one is done or the person dismisses it. Dismissing is remembered in this
 *  browser only (a per-viewer convenience). */
export function ReadyReminder() {
  const { rows } = useReadiness("/projects");
  const [hidden, setHidden] = useState(dismissed);
  if (hidden || !rows.length || rows.every((r) => r.done)) return null;
  return (
    <section className="ready-box" aria-label="Still to set up">
      <ul className="ready-list">
        {rows.map((r) => <ReadyItem key={r.key} done={r.done} title={r.title} text={r.text} action={r.action()} />)}
      </ul>
      <button type="button" className="dim ready-dismiss"
              onClick={() => { try { localStorage.setItem(DISMISS_KEY, "1"); } catch { /* private mode */ } setHidden(true); }}>
        Hide this list
      </button>
    </section>
  );
}
