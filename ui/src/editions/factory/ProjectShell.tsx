import * as React from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "../../api";
import { Banner, Button, Dialog, Skeleton, Toaster } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { placeBeforeProject } from "../settingsBack";
import { useOwnMenu } from "../Shell";
import { useSample } from "../sample";
import {
  acceptMilestone, addProjectMember, answerCheck, decideCall, factoryProject, setLater, setProjectIcon, setProjectLead,
  type FactoryProjectData, type ProjectCall, type ProjectMilestone, type ProjectPageState,
} from "./FactoryProject.api";
import type { HomeItem } from "./home.types";
import { InlineIcon } from "./ProjectIcon";
import { isSample, sampleClose, sampleClosed, sampleLoad } from "./projectSamples";

// The frame of every page of one factory project (TR-1245): the project sidebar beside the page.
// On top the project's name, icon and state, then one item per page (Overview, Tickets, Pull
// requests, Milestones, Decisions and calls, Docs and memory, People and access, Settings), each
// with its count, then where the tickets live and the builder's session. At phone width the items
// become a row of buttons under the name that scrolls on its own.
//
// The frame reads the project once (GET /api/factory/projects/{id}) and gives the page it holds the
// answer and the project's actions. The answer is kept between pages, so moving from one to the
// next shows at once and reads again in the background. A page with more to show (pull requests,
// docs, tickets) makes its own read beside this one.

const POLL_MS = 15_000;
const CLOSE_WORDS = "Closed from the project page";
const MUTED = "text-xs text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";

export type ProjectPage = "overview" | "tickets" | "prs" | "milestones" | "decisions" | "docs" | "people" | "settings";

const PAGES: { key: ProjectPage; label: string; to: string }[] = [
  { key: "overview", label: "Overview", to: "" },
  { key: "tickets", label: "Tickets", to: "/tickets" },
  { key: "prs", label: "Pull requests", to: "/github" },
  { key: "milestones", label: "Milestones", to: "/milestones" },
  // Calls is the first of its two tabs: what the crew decided without you, to confirm
  { key: "decisions", label: "Decisions and calls", to: "/calls" },
  { key: "docs", label: "Docs and memory", to: "/docs" },
  { key: "people", label: "People and access", to: "/people" },
  { key: "settings", label: "Settings", to: "/settings" },
];

const CHIP: Record<ProjectPageState, string> = {
  waiting_on_you: "text-[color:var(--accent-deep)]",
  building: "text-[color:var(--accent-deep)]",
  not_started: "text-muted-foreground",
  ready_to_close: "text-[color:var(--ok)]",
  later: "text-muted-foreground",
  finished: "text-foreground",
};

/** The address of one page of a project. */
export const pageHref = (id: string, page: ProjectPage) =>
  `/projects/${encodeURIComponent(id)}${PAGES.find((p) => p.key === page)?.to ?? ""}`;

/** The row the cell does not send: every ticket merged and the project still open. */
function readyRow(d: FactoryProjectData): HomeItem {
  const merged = d.counts.merged;
  return {
    id: "ready_to_close", kind: "signoff", label: "Ready to close",
    title: `All ${merged} ${merged === 1 ? "ticket is" : "tickets are"} merged`,
    detail: "Close the project when you have checked the result.", meta: "",
    project: { id: d.id, name: d.name }, repo: null, from: null, since: null, waited: "", actions: [
      { label: "Close project", primary: true, do: "close" },
    ],
  };
}

/** What waits for the person on this project, as Overview lists it, leaving out the ids in `kept`. */
export function waitingItems(d: FactoryProjectData, kept: string[] = []): HomeItem[] {
  if (d.state === "finished") return [];
  // a button that opens this very page has no place on it
  const here = `/projects/${d.id}`;
  const own = d.waiting.map((w) => ({ ...w, actions: w.actions.filter((a) => a.href !== here) }));
  const listed = own.filter((w) => !kept.includes(w.id));
  return d.can_close && !listed.some((w) => w.actions.some((a) => a.do === "close")) ? [...listed, readyRow(d)] : listed;
}

function countOf(d: FactoryProjectData, page: ProjectPage): number {
  switch (page) {
    case "overview": return waitingItems(d).length;
    case "tickets": return d.counts.total;
    case "prs": return d.prs_total ?? 0;
    case "milestones": return d.milestones.length;
    case "decisions": return d.decisions.length + d.calls.length;
    case "docs": return d.docs_total ?? d.docs.length;
    case "people": return d.people ? (d.people.lead ? 1 : 0) + d.people.members.length : 0;
    default: return 0;
  }
}

// ── the project's read and its actions ───────────────────────────────────────

const kept = new Map<string, FactoryProjectData>();

export interface ProjectActions {
  close: () => Promise<void>;
  decide: (c: ProjectCall, state: "kept" | "overturned", words?: string) => Promise<void>;
  answer: (m: ProjectMilestone, n: number, result: "pass" | "fail", note?: string) => Promise<void>;
  accept: (m: ProjectMilestone) => Promise<void>;
  lead: (person: string) => Promise<void>;
  add: (person: string) => Promise<void>;
  later: (on: boolean) => Promise<void>;
  /** Left out on a sample: there is no cell to keep the icon. */
  icon?: (next: string | null) => Promise<void>;
  remove: () => Promise<void>;
}

export interface ProjectCtx {
  d: FactoryProjectData;
  id: string;
  /** The sample this page shows, or null on a live cell. */
  sample: string | null;
  reload: () => void;
  act: ProjectActions;
}

function useProject(id: string, onOther?: () => void) {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const key = sample ? `sample:${sample}` : id;
  const [closedHere, setClosedHere] = React.useState(false);
  const navigate = useNavigate();
  const { data, error, reload } = usePagePolling<FactoryProjectData>(async () => {
    const d = await (sample ? sampleLoad(sample) : factoryProject(id).catch((e) => {
      // the cell answers 404 for a project this page is not for: today's page takes it
      if (e instanceof ApiError && e.status === 404 && onOther) onOther();
      throw e;
    }));
    kept.set(key, d);
    return d;
  }, POLL_MS);
  const live = (fn: () => Promise<unknown>) => async () => { if (!sample) { await fn(); reload(); } };
  const act: ProjectActions = {
    // a sample has no cell to close on: `fail` answers as the cell does, any other moves the page on this copy
    close: async () => {
      if (sample) {
        if (sampleClose(sample).error) throw new ApiError(sampleClose(sample).error, 409);
        setClosedHere(true);
        return;
      }
      await api.finishProject(id, CLOSE_WORDS);
      reload();
    },
    decide: (c, state, words) => live(() => decideCall(id, c.ref, state, words))(),
    answer: (m, n, result, note) => live(() => answerCheck(id, m.id, n, result, note))(),
    accept: (m) => live(() => acceptMilestone(id, m.id))(),
    lead: (person) => live(() => setProjectLead(id, person))(),
    add: (person) => live(() => addProjectMember(id, person))(),
    later: (on) => live(() => setLater(id, on))(),
    icon: sample ? undefined : async (next) => { await setProjectIcon(id, next); reload(); },
    remove: async () => {
      if (!sample) await api.deleteProject(id);
      kept.delete(key);
      navigate("/projects", { replace: true });
    },
  };
  const shown = data ?? kept.get(key);
  return { data: shown && closedHere ? sampleClosed(shown) : shown, error, reload, act, sample };
}

// ── the builder's session ────────────────────────────────────────────────────

function Command({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = React.useState<"" | "yes" | "failed">("");
  React.useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(""), 2000);
    return () => clearTimeout(t);
  }, [copied]);
  const copy = () => {
    // a cell served over plain http has no clipboard: the command stays on the page to select
    if (!navigator.clipboard) { setCopied("failed"); return; }
    navigator.clipboard.writeText(command).then(() => setCopied("yes"), () => setCopied("failed"));
  };
  return (
    <div data-testid="command">
      <div className="mb-1.5 text-[13px]">{label}</div>
      <div className="flex items-stretch gap-2">
        <code data-words-exempt data-testid="command-text"
              className="flex min-w-0 flex-1 items-center border border-border bg-[var(--input-bg)] px-2 py-1.5 font-mono text-[12.5px] [overflow-wrap:anywhere]">{command}</code>
        <Button variant="secondary" className="h-auto min-h-[30px] text-[13px]!" onClick={copy} data-testid="command-copy">{copied === "yes" ? "Copied" : copied === "failed" ? "Select it to copy" : "Copy"}</Button>
      </div>
    </div>
  );
}

/** "Work on it yourself": the commands that open a builder's session, in a dialog. Only when a builder works on the project. */
function Terminal({ items }: { items: FactoryProjectData["terminal"] }) {
  const [open, setOpen] = React.useState(false);
  if (items.length === 0) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} data-testid="terminal-open"
              className={cn("border-0! bg-transparent! p-0! text-left text-[13px]! font-normal! shadow-none!", LINK)}>
        Work on it yourself
      </button>
      <Dialog open={open} onOpenChange={setOpen} data-testid="terminal" title="Work on it yourself"
              description="Open a builder's session in your terminal, to watch it or take over."
              footer={<Button variant="secondary" onClick={() => setOpen(false)} data-testid="terminal-done">Done</Button>}>
        <div className="grid gap-4">{items.map((c) => <Command key={c.command} label={c.label} command={c.command} />)}</div>
      </Dialog>
    </>
  );
}

// ── the frame ────────────────────────────────────────────────────────────────

const BACK_ARROW = (
  <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M8 2L4 6l4 4" /></svg>
);

/** The way out: the project's pages hide the main menu, as Settings does. Back goes to the page before the project. */
function Back() {
  return (
    <Link to={placeBeforeProject()} data-testid="project-back"
          className="inline-flex! h-8 w-fit items-center gap-1.5 px-2.5 text-[13px] text-muted-foreground! no-underline! hover:text-foreground!">{BACK_ARROW}Back</Link>
  );
}

function ProjectNav({ d, page, search }: { d: FactoryProjectData; page: ProjectPage; search: string }) {
  // at phone width the row scrolls on its own: bring the page in view into it, without moving the page
  const row = React.useRef<HTMLUListElement>(null);
  React.useEffect(() => {
    const ul = row.current;
    const on = ul?.querySelector<HTMLElement>("[aria-current=page]");
    if (!ul || !on || ul.scrollWidth <= ul.clientWidth) return;
    ul.scrollLeft = Math.max(0, on.offsetLeft - 16);   // the row is the links' offset parent
  }, [page]);
  return (
    <nav aria-label="This project" data-testid="project-nav"
         className="min-w-0 lg:sticky lg:top-[89px] lg:w-[216px] lg:flex-none">
      <Back />
      <div className="mt-2 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-start gap-x-2 px-2.5 lg:px-2.5" data-testid="project-title">
        {d.icon ? <InlineIcon icon={d.icon} className="mr-0 text-[20px] leading-[26px]" /> : <span aria-hidden />}
        <span className={cn("min-w-0 text-[18px] font-bold leading-[26px] [overflow-wrap:anywhere]", !d.icon && "-col-start-1 col-span-2")} data-user-text data-testid="project-name">{d.name}</span>
        <span className={cn("mt-1 font-mono text-[11px] font-medium uppercase tracking-[0.06em]", d.icon ? "col-start-2" : "col-span-2", CHIP[d.state])} data-testid="project-chip">
          {d.state_label}
        </span>
      </div>
      <ul ref={row} className="relative m-0! mt-3 flex list-none! gap-1.5 overflow-x-auto p-0! pb-1 lg:mt-4 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:pb-0" data-testid="project-pages">
        {PAGES.map((p) => {
          const n = countOf(d, p.key);
          const on = p.key === page;
          return (
            <li key={p.key} className="m-0! flex-none">
              <Link to={`${pageHref(d.id, p.key)}${search}`} aria-current={on ? "page" : undefined} data-testid={`project-page-${p.key}`}
                    className={cn("flex items-center justify-between gap-3 whitespace-nowrap border border-border px-3 py-1.5 text-[14px] no-underline! hover:bg-muted",
                                  "lg:border-0 lg:px-2.5 lg:py-[7px]",
                                  on ? "border-foreground bg-card font-semibold text-foreground! lg:border lg:border-border" : "text-muted-foreground!")}>
                <span>{p.label}</span>
                {n > 0 && (
                  <span className={cn("font-mono text-xs font-normal", p.key === "overview" ? "text-[color:var(--accent-deep)]" : "text-muted-foreground")}
                        data-testid="project-page-count">{n}</span>
                )}
              </Link>
            </li>
          );
        })}
      </ul>
      {(d.terminal.length > 0 || (d.linked.tickets_in && d.linked.url)) && (
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-[13px] lg:mt-4 lg:grid lg:border-t lg:border-border lg:px-2.5 lg:pt-3" data-testid="project-nav-links">
          {d.linked.tickets_in && d.linked.url && (
            <a href={d.linked.url} target="_blank" rel="noopener noreferrer" className={LINK} data-testid="tracker-link">
              Tickets in {d.linked.tickets_in} {"↗"}
            </a>
          )}
          <Terminal items={d.terminal} />
        </div>
      )}
    </nav>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" data-testid="project-loading" className="grid min-w-0 gap-5">
      <Skeleton width={160} height={26} />
      <Skeleton width="70%" />
      <div className="grid gap-2.5 border border-border bg-card p-4">
        {[90, 70, 80, 60].map((w, i) => <Skeleton key={i} width={`${w}%`} />)}
      </div>
      <div className="grid gap-2.5 border border-border bg-card p-4">
        {[80, 60, 90].map((w, i) => <Skeleton key={i} width={`${w}%`} />)}
      </div>
    </div>
  );
}

/** The heading of a project's page: its name for the page, a line under it, and buttons beside it. */
export function PageHead({ title, sub, actions, children }: { title: string; sub?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="grid min-w-0 gap-2">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h1 className="m-0! text-[22px]! md:text-[26px]!" data-testid="page-title">{title}</h1>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
      {sub && <div className="m-0! max-w-[760px] text-[15px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="page-sub">{sub}</div>}
      {children}
    </div>
  );
}

/** One page of a project, in the project's frame. `onOther` hands the address to today's page when
 *  the cell does not answer for it as a factory project (only Overview, the project's own address). */
export function ProjectShell({ page, onOther, children }: {
  page: ProjectPage;
  onOther?: () => void;
  children: (ctx: ProjectCtx) => React.ReactNode;
}) {
  const { id = "" } = useParams();
  const { hash } = useLocation();
  const { data, error, reload, act, sample } = useProject(id, onOther);
  useOwnMenu();
  const keep = sample ? `?sample=${encodeURIComponent(sample)}` : "";

  // an address with a #section scrolls to it once the page is there
  const ready = Boolean(data);
  React.useEffect(() => {
    if (!hash || !ready) return;
    let at = hash.slice(1);
    try { at = decodeURIComponent(at); } catch { /* a malformed address keeps its raw text */ }
    // a #desk-* one (a permission request named for its desk) waits in Waiting for you
    const el = document.getElementById(at) ?? (at.startsWith("desk-") ? document.getElementById("waiting") : null);
    el?.scrollIntoView({ block: "start" });
  }, [hash, ready]);

  if (!data) {
    return (
      <div className="min-w-0" data-testid="factory-project-wait">
        {error ? (
          <div className="grid gap-3">
            <Back />
            <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
              <b>We could not load this project.</b> <span className="text-foreground">Check your connection, then try again.</span>
            </Banner>
          </div>
        ) : <Loading />}
      </div>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-6 lg:flex-row lg:items-start lg:gap-8" data-testid="project-shell" data-page={page}>
      <ProjectNav d={data} page={page} search={keep} />
      <div className="grid min-w-0 flex-1 content-start gap-6" data-testid="project-page">
        {error && (
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
            <b>This page did not refresh.</b> <span className="text-foreground">Check your connection, then try again.</span>
          </Banner>
        )}
        {children({ d: data, id, sample, reload, act })}
      </div>
      <Toaster />
    </div>
  );
}
