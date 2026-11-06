import * as React from "react";
import { Link, useParams } from "react-router-dom";

import { Banner, Button, Skeleton } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { PR_CHIP, PrSize, n } from "./prParts";
import { factoryProjectGitHub, type ProjectGitHubData, type ProjectPr, type ProjectRepoPrs } from "./FactoryProject.api";

// /projects/:id/github: what happened to the code. Every pull request the project's tickets opened,
// by repo: the open ones first, then merged, then closed without merging. Each says its ticket, CI,
// the crew's review, how big the change was and who merged it, as GitHub said when Tares last read it.

const POLL_MS = 30000;
const MUTED = "text-xs text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";
const CARD = "border border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.07)]";


/** "3 open, 12 merged, 1 closed without merging", leaving out what is none. */
export function countsText(c: { open: number; merged: number; closed: number }): string {
  const parts = [c.open && `${c.open} open`, c.merged && `${c.merged} merged`, c.closed && `${c.closed} closed without merging`].filter(Boolean);
  return parts.length ? parts.join(", ") : "no pull requests";
}

function PrRow({ p }: { p: ProjectPr }) {
  const facts: React.ReactNode[] = [];
  if (p.branch) facts.push(<span key="b" className="font-mono">{p.branch}{p.base ? ` → ${p.base}` : ""}</span>);
  if (p.when) facts.push(<span key="w">{p.when}</span>);
  if (p.ci) facts.push(<span key="c" className={p.ci === "CI failing" ? "text-[color:var(--err)]" : undefined}>{p.ci}</span>);
  if (p.review) facts.push(<span key="r">review {p.review}</span>);
  if (p.size) facts.push(<PrSize key="s" s={p.size} />);
  return (
    <div className={cn(CARD, "px-5 py-4")} data-testid="pr-row" data-state={p.state} data-number={p.number ?? ""}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1.5">
        <div className="flex min-w-0 flex-[1_1_320px] items-baseline gap-2.5">
          <span className="flex-none font-mono text-xs font-medium">{p.number ? `#${p.number}` : "PR"}</span>
          {p.url ? (
            <a href={p.url} target="_blank" rel="noopener noreferrer" data-testid="pr-title" data-user-text
               className="font-semibold text-foreground! no-underline! [overflow-wrap:anywhere] hover:underline!">
              {p.title} <span aria-hidden className="font-normal text-muted-foreground">{"↗"}</span>
            </a>
          ) : <span className="font-semibold [overflow-wrap:anywhere]" data-user-text>{p.title}</span>}
        </div>
        <span data-testid="pr-state"
              className={cn("flex-none border px-2 py-0.5 font-mono text-[11px] font-medium uppercase tracking-[0.06em]", PR_CHIP[p.state])}>
          {p.state_label}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[13px] text-muted-foreground" data-testid="pr-facts">
        <Link to={p.ticket_href} className={LINK} data-testid="pr-ticket">{p.ticket}</Link>
        {facts.map((f, i) => <React.Fragment key={i}><span aria-hidden>{"·"}</span>{f}</React.Fragment>)}
      </div>
      {p.error && <div className="mt-1 text-[13px] text-[color:var(--err)]" data-testid="pr-error">Not read from GitHub: {p.error}</div>}
    </div>
  );
}

function Repo({ r }: { r: ProjectRepoPrs }) {
  const changed = r.added || r.removed;
  return (
    <section aria-label={r.name ?? "Pull requests"} className="min-w-0" data-testid="pr-repo">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="m-0! text-base! font-mono!">
          {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-foreground! no-underline! hover:underline!">{r.name} <span aria-hidden className="text-muted-foreground">{"↗"}</span></a>
            : (r.name ?? "Repo unknown")}
        </h2>
        <span className="text-[13px] text-muted-foreground" data-testid="pr-repo-counts">
          {countsText(r.counts)}
          {changed ? <>, merged <span className="text-[color:var(--ok)]">+{n(r.added)}</span> <span className="text-[color:var(--err)]">{"−"}{n(r.removed)}</span></> : null}
        </span>
      </div>
      <div className="grid gap-3">{r.prs.map((p) => <PrRow key={p.url ?? `${p.ticket}`} p={p} />)}</div>
    </section>
  );
}

export default function FactoryProjectGitHub() {
  const { id = "" } = useParams();
  const { data, error, reload } = usePagePolling<ProjectGitHubData>(() => factoryProjectGitHub(id), POLL_MS);
  const crumb = (
    <div className={MUTED} data-testid="crumb">
      <Link to="/projects" className={LINK}>Projects</Link>
      {data && <> / <Link to={`/projects/${encodeURIComponent(id)}`} className={LINK} data-user-text>{data.name}</Link></>}
      {" / "}GitHub
    </div>
  );
  if (!data) {
    return (
      <div className="grid min-w-0 gap-3" data-testid="github-page-wait">
        {crumb}
        {error ? (
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
            <b>We could not load the pull requests.</b> <span className="text-foreground">Check your connection, then try again.</span>
          </Banner>
        ) : <Skeleton className="h-40" />}
      </div>
    );
  }
  return (
    <div className="grid min-w-0 max-w-[980px] gap-8" data-testid="github-page">
      <div className="grid gap-3">
        {crumb}
        <div>
          <h1 className="m-0! text-[22px]! md:text-[28px]!">GitHub</h1>
          <p className={cn(MUTED, "m-0! mt-1!")} data-testid="github-summary">
            {countsText(data.counts)} on <span data-user-text>{data.name}</span>.
            {data.checked && <> Last read from GitHub {data.checked}.</>}
          </p>
        </div>
        {data.github_error && (
          <Banner tone="warn" data-testid="github-error">
            <b>Some pull requests could not be read from GitHub.</b> <span className="text-foreground">{data.github_error}</span>
          </Banner>
        )}
      </div>
      {data.repos.length === 0 ? (
        <div className={cn(CARD, "px-5 py-4 text-muted-foreground")} data-testid="github-empty">
          No pull requests yet. Each one the crew opens for a ticket shows here.
        </div>
      ) : data.repos.map((r) => <Repo key={r.name ?? ""} r={r} />)}
    </div>
  );
}
