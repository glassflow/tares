import * as React from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { Banner, Button, Collapse, EmptyState, Skeleton, STATUS_TONES, StatusLabel } from "../../components/edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { MergeQueuePanel } from "./FactoryMergeQueue";
import { mergeQueue, type MergeQueueData } from "./FactoryMergeQueue.api";
import { isSample, sampleLoad, samplePrs } from "./mergeQueueSamples";
import { PrSize, n } from "./prParts";
import { cn } from "../../lib/utils";
import { PageHead, ProjectShell } from "./ProjectShell";
import { factoryProjectGitHub, type ProjectGitHubData, type ProjectPr, type ProjectRepoPrs } from "./FactoryProject.api";

// /projects/:id/github, Pull requests in the project's frame (TR-1245): what happened to the code. Every pull request the project's tickets opened,
// by repo: the open ones first, then merged, then closed without merging. Each says its ticket, CI,
// the crew's review, how big the change was and who merged it, as GitHub said when Tares last read it.
// The page has two tabs (TR-869): Pull requests (this one) and the Merge queue, the pull requests in
// line to merge. The tab is in the address (?tab=queue) so a reload keeps it.

const POLL_MS = 30000;
const LINK = "text-foreground underline";
// On the paper (TR-1266): a 1px rule over each repo, rules between its pull requests, no cards.
const ROW = "border-b border-border py-4 last:border-b-0";

/** "3 open, 12 merged, 1 closed without merging", leaving out what is none. */
export function countsText(c: { open: number; merged: number; closed: number }): string {
  const parts = [c.open && `${c.open} open`, c.merged && `${c.merged} merged`, c.closed && `${c.closed} closed without merging`].filter(Boolean);
  return parts.length ? parts.join(", ") : "no pull requests";
}

function PrRow({ p }: { p: ProjectPr }) {
  const facts: React.ReactNode[] = [];
  if (p.branch) facts.push(<span key="b" className="font-mono">{p.branch}{p.base ? ` → ${p.base}` : ""}</span>);
  if (p.when) facts.push(<span key="w">{p.when}</span>);
  if (p.ci) facts.push(<span key="c" className={p.ci === "Checks failing" ? "text-err" : undefined}>{p.ci}</span>);
  if (p.review) facts.push(<span key="r">review {p.review}</span>);
  if (p.size) facts.push(<PrSize key="s" s={p.size} />);
  return (
    <div className={ROW} data-testid="pr-row" data-state={p.state} data-number={p.number ?? ""}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1.5">
        <div className="flex min-w-0 flex-[1_1_320px] items-baseline gap-2.5">
          <span className="flex-none font-mono text-xs font-medium">{p.number ? `#${p.number}` : "PR"}</span>
          {p.url ? (
            <a href={p.url} target="_blank" rel="noopener noreferrer" data-testid="pr-title" data-user-text
               className="font-semibold text-foreground no-underline [overflow-wrap:anywhere] hover:underline">
              {p.title} <span aria-hidden className="font-normal text-muted-foreground">{"↗"}</span>
            </a>
          ) : <span className="font-semibold [overflow-wrap:anywhere]" data-user-text>{p.title}</span>}
        </div>
        <StatusLabel data-testid="pr-state" tone={STATUS_TONES.pr[p.state]} className="flex-none">
          {p.state_label}
        </StatusLabel>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[13px] text-muted-foreground" data-testid="pr-facts">
        <Link to={p.ticket_href} className={LINK} data-testid="pr-ticket">{p.ticket}</Link>
        {facts.map((f, i) => <React.Fragment key={i}><span aria-hidden>{"·"}</span>{f}</React.Fragment>)}
      </div>
      {p.error && <div className="mt-1 text-[13px] text-err" data-testid="pr-error">Not read from GitHub: {p.error}</div>}
    </div>
  );
}

function Repo({ r }: { r: ProjectRepoPrs }) {
  const changed = r.added || r.removed;
  return (
    <section aria-label={r.name ?? "Pull requests"} className="min-w-0" data-testid="pr-repo">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-border pt-3">
        <h2 className="m-0 text-base">
          {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-foreground no-underline hover:underline">{r.name} <span aria-hidden className="text-muted-foreground">{"↗"}</span></a>
            : (r.name ?? "Repo unknown")}
        </h2>
        <span className="text-[13px] text-muted-foreground" data-testid="pr-repo-counts">
          {countsText(r.counts)}
          {changed ? <>, merged <span className="text-ok">+{n(r.added)}</span> <span className="text-err">{"−"}{n(r.removed)}</span></> : null}
        </span>
      </div>
      <div>{r.prs.map((p) => <PrRow key={p.url ?? `${p.ticket}`} p={p} />)}</div>
    </section>
  );
}

/** The merge queue's read. `refresh` asks again and answers once the new read has landed (or failed), so a
 *  row that was just answered can stay "saving" until the page shows what the cell now says. */
function useQueueRead(id: string, sample: string | null) {
  const started = React.useRef(0);
  const finished = React.useRef(0);
  const waiters = React.useRef<(() => void)[]>([]);
  const [settled, setSettled] = React.useState(0);
  const { data, error, reload } = usePagePolling<MergeQueueData>(async () => {
    started.current += 1;
    try {
      return await (sample ? sampleLoad(sample) : mergeQueue(id));
    } finally {
      finished.current += 1;
      // only when no newer read is still out: an older answer must not release the row early
      if (finished.current === started.current) setSettled((x) => x + 1);
    }
  }, POLL_MS);
  // after the render that shows the read (or its error), so the row never shows the old question again
  React.useEffect(() => {
    if (!settled) return;
    const w = waiters.current;
    waiters.current = [];
    w.forEach((f) => f());
  }, [settled]);
  const refresh = React.useCallback(() => new Promise<void>((resolve) => { waiters.current.push(resolve); reload(); }), [reload]);
  return { data, error, refresh };
}

function Tab({ to, on, tab, children }: { to: string; on: boolean; tab: "queue" | "prs"; children: React.ReactNode }) {
  return (
    <Link to={to} replace role="tab" id={`tab-${tab}`} aria-selected={on} aria-controls={on ? `panel-${tab}` : undefined} data-testid={`tab-${tab}`}
          className={cn("-mb-px flex items-center gap-2 whitespace-nowrap border-b-[3px] border-transparent px-3.5 py-2.5 text-sm text-muted-foreground no-underline hover:bg-row-hover hover:text-foreground",
                        on && "border-b-brand font-semibold text-foreground")}>
      {children}
    </Link>
  );
}

function PullRequests({ id }: { id: string }) {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const { pathname, search } = useLocation();
  const [params] = useSearchParams();
  const tab: "queue" | "prs" = params.get("tab") === "queue" ? "queue" : "prs";
  const { data, error, reload } = usePagePolling<ProjectGitHubData>(() => (sample ? samplePrs() : factoryProjectGitHub(id)), POLL_MS);
  // The merge queue is read on both tabs: the tab strip and the note on this tab say how many are in line.
  const q = useQueueRead(id, sample);
  const to = (t: "queue" | "prs") => {
    const p = new URLSearchParams(search);
    if (t === "queue") p.set("tab", "queue"); else p.delete("tab");
    const s = p.toString();
    return `${pathname}${s ? `?${s}` : ""}`;
  };
  const tabInfo = q.data?.tab;
  return (
    <div className="grid min-w-0 max-w-[980px] gap-5" data-testid="github-page">
      <PageHead title="Pull requests" sub={data ? (
        <span className="text-xs" data-testid="github-summary">
          {countsText(data.counts)} on <span data-user-text>{data.name}</span>.
          {data.checked && <> Last read from GitHub {data.checked}.</>}
        </span>) : undefined} />
      <div role="tablist" aria-label="Pull requests page" className="flex min-w-0 gap-1 overflow-x-auto border-b border-border" data-testid="pr-tabs">
        <Tab to={to("queue")} on={tab === "queue"} tab="queue">
          Merge queue
          {tabInfo && <span className="font-mono text-xs font-normal text-muted-foreground" data-testid="tab-queue-n">{tabInfo.line}</span>}
          {tabInfo && tabInfo.need > 0 && (
            <span className="bg-brand px-[7px] py-px font-mono text-[11px] font-semibold leading-normal tracking-[0.04em] text-brand-foreground" data-testid="tab-queue-need">
              {tabInfo.need_text}
            </span>
          )}
        </Tab>
        <Tab to={to("prs")} on={tab === "prs"} tab="prs">
          Pull requests
          {data && <span className="font-mono text-xs font-normal text-muted-foreground" data-testid="tab-prs-n">{data.counts.open}</span>}
        </Tab>
      </div>
      {tab === "queue" ? (
        <div role="tabpanel" id="panel-queue" aria-labelledby="tab-queue">
          <MergeQueuePanel id={id} data={q.data} error={q.error} reload={q.refresh} prsHref={to("prs")} sample={!!sample} />
        </div>
      ) : (
        <div role="tabpanel" id="panel-prs" aria-labelledby="tab-prs" className="grid min-w-0 gap-8">
          <p className="m-0 text-sm text-muted-foreground" data-testid="pr-queue-note">
            {tabInfo ? (
              tabInfo.line > 0 ? (
                <><b className="font-semibold text-foreground">{tabInfo.note}</b>{" "}
                  <Link to={to("queue")} replace className={LINK} data-testid="pr-queue-link">Open the merge queue</Link></>
              ) : tabInfo.note
            ) : (
              <>The merge queue is not shown while it loads or after an error. Open the{" "}
                <Link to={to("queue")} replace className={LINK} data-testid="pr-queue-link">Merge queue tab</Link>.</>
            )}
          </p>
          {!data ? (
            <div className="grid min-w-0 gap-5" data-testid="github-page-wait">
              {error ? (
                <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
                  <b>We could not load the pull requests.</b> <span className="text-foreground">Check your connection, then try again.</span>
                </Banner>
              ) : <Skeleton className="h-40" />}
            </div>
          ) : (
            <>
              <Collapse>{data.github_error && (
                <Banner tone="warn" data-testid="github-error">
                  <b>Some pull requests could not be read from GitHub.</b> <span className="text-foreground">{data.github_error}</span>
                </Banner>
              )}</Collapse>
              {data.repos.length === 0 ? (
                <EmptyState className="border-t border-border py-4" data-testid="github-empty">
                  No pull requests yet. Each one the crew opens for a ticket shows here.
                </EmptyState>
              ) : data.repos.map((r) => <Repo key={r.name ?? ""} r={r} />)}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function FactoryProjectGitHub() {
  return <ProjectShell page="prs">{({ id }) => <PullRequests id={id} />}</ProjectShell>;
}
