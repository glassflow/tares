import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, ApiError } from "../../api";
import { Banner, Button, Dialog, EmptyState, LoadingBlocks, ProgressBar, STATUS_TONES, StatusLabel } from "../../components/edition";
import { Input } from "../../components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import { Slot, useEeLoaded, useEePending, useEeSamples } from "../../eeSlots";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  factoryProjects, type FactoryProjectRow, type FactoryProjectsData, type ProjectState, type ProjectsView,
} from "./FactoryProjects.api";
import { InlineIcon } from "./ProjectIcon";
import { isSample, sampleClose, sampleClosed, sampleLoad } from "./projectsSamples";

// Projects: every software factory project as one row, from one read (GET /api/factory/projects).
// The filter lives in the address (?state=) and the search box filters the rows already read, so
// neither makes another call. A project whose tickets are all merged has a Close button. Every
// string from the cell is a React text node; a name, goal, repo and milestone are the person's own
// text and marked data-user-text.
//
// On a cell with more than one person the body carries `team` (TR-1277), and ee fills four slots:
// the Yours/Team switch (?whose=team, cut from the rows already read, so it makes no call either), the
// lock on a private project, who leads each row, and the private projects the person is not on, as
// quiet rows (Team) or one line (Mine). Without the ee chunk loaded the page ignores `team` and lists
// every row, so a switch that is not on screen never hides a project.

const POLL_MS = 15_000;
const NOT_CLOSED = "The project was not closed. Try again in a moment.";
const CLOSE_WORDS = "Closed from the Projects list";

type Filter = "open" | "all" | ProjectState;
const TABS: { key: Filter; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "waiting_on_you", label: "Waiting for you" },
  { key: "building", label: "Building" },
  { key: "not_started", label: "Not started" },
  { key: "ready_to_close", label: "Ready to close" },
  { key: "later", label: "Later" },
  { key: "done", label: "Done" },
  { key: "all", label: "All" },
];
const isFilter = (v: string | null): v is Filter => TABS.some((t) => t.key === v);

const pct = (n: number, of: number) => (of > 0 ? Math.round((n / of) * 100) : 0);

function Row({ p, error, onClose, team, me }: {
  p: FactoryProjectRow; error?: string; onClose: (p: FactoryProjectRow) => void; team: boolean; me?: string;
}) {
  const { merged, closed, queued, stuck, total } = p.tickets;
  const repos = p.repo_names.join(", ");
  return (
    <div className="border-b border-border px-4 py-3.5 last:border-b-0 hover:bg-row-hover" data-testid="project-row" data-state={p.state} data-project={p.id}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <InlineIcon icon={p.icon} />
          {team && p.scope === "private" && <Slot name="projects.lock" />}
          <Link to={`/projects/${encodeURIComponent(p.id)}`} data-user-text data-testid="project-name"
                className="font-bold [overflow-wrap:anywhere] text-foreground underline decoration-border underline-offset-[3px]">
            {p.name}
          </Link>
          <StatusLabel data-testid="project-state" tone={STATUS_TONES.project[p.state]} className="ml-2 align-[1px]">
            {p.state_label}
          </StatusLabel>
        </div>
        {p.can_close && (
          <Button variant="secondary" data-testid="project-close" onClick={() => onClose(p)}
                  className="h-[30px] shrink-0 border-foreground bg-field">
            Close
          </Button>
        )}
      </div>
      <div data-user-text data-testid="project-goal" title={p.goal} className="mt-1 line-clamp-2 [overflow-wrap:anywhere]">{p.goal}</div>
      {(repos || p.meta) && (
        <div className="mt-0.5 text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="project-meta">
          {repos && <span data-user-text>{repos}</span>}
          {repos && p.meta && " · "}
          {p.meta}
        </div>
      )}
      {team && <Slot name="projects.lead" p={p} me={me} />}
      <ProgressBar value={pct(merged + closed, total)} blocked={pct(stuck, total)} aria-label={p.tickets.label}
                   className={cn("my-2 [&>div:first-child]:bg-foreground", p.state === "done" && "[&>div:first-child]:bg-faint")} />
      {/* the server's count line, with stuck in the error tone: it is the group a glance is for */}
      <div className="text-xs text-muted-foreground" data-testid="project-tickets" aria-label={p.tickets.label}>
        {total === 0 ? p.tickets.label : (
          <span aria-hidden="true">
            {merged} merged{closed > 0 && `, ${closed} closed`}{queued > 0 && `, ${queued} queued`}
            {stuck > 0 && <>, <span className="text-err" data-testid="project-stuck">{stuck} stuck</span></>} of {total}
          </span>
        )}
      </div>
      {p.milestone && <div data-user-text data-testid="project-milestone" className="mt-0.5 text-xs text-muted-foreground [overflow-wrap:anywhere]">{p.milestone}</div>}
      {error && <div role="alert" data-user-text data-testid="project-error" className="mt-2 text-[13px] text-err [overflow-wrap:anywhere]">{error}</div>}
    </div>
  );
}


const NewProject = ({ amber = true }: { amber?: boolean }) => (
  <Button asChild variant={amber ? "primary" : "secondary"}>
    <Link to="/projects/new" data-testid="new-project">New project</Link>
  </Button>
);

/** `onClose` closes one project, and answers when it is done or throws the cell's answer. Without
 *  it the Close dialog only opens and shuts. `start` is a Close part way, for a sample. */
export function FactoryProjectsView({ data, onClose, start }: {
  data: FactoryProjectsData;
  onClose?: (p: FactoryProjectRow) => Promise<void>;
  start?: { open: string; row: string; error: string };
}) {
  const [params, setParams] = useSearchParams();
  const asked = params.get("state");
  const tab: Filter = isFilter(asked) ? asked : "open";
  const [query, setQuery] = React.useState("");
  const [asking, setAsking] = React.useState<string | null>(start?.open || null);
  const [busy, setBusy] = React.useState(false);
  const [errors, setErrors] = React.useState<Record<string, string>>(start?.row ? { [start.row]: start.error } : {});
  const me = useMe();
  // the team's keys count only once ee can show the switch that undoes Mine
  const team = useEeLoaded() ? data.team : undefined;
  const view: ProjectsView = team && params.get("whose") === "team" ? "team" : "mine";
  const rows = team && view === "mine" ? data.projects.filter((p) => p.mine) : data.projects;
  const counts = team ? countsOf(rows) : data.counts;

  const setTab = (k: string) => setParams((q) => {
    const n = new URLSearchParams(q);
    if (k === "open") n.delete("state"); else n.set("state", k);
    return n;
  });
  const setView = (v: ProjectsView) => setParams((q) => {
    const n = new URLSearchParams(q);
    if (v === "mine") n.delete("whose"); else n.set("whose", v);
    return n;
  });
  // Open is what is being worked on: not finished, and not kept for later
  const count = (k: Filter) => k === "all" ? counts.all
    : k === "open" ? counts.all - counts.done - (counts.later ?? 0) : (counts[k] ?? 0);
  const finished = counts.done;

  const needle = query.trim().toLowerCase();
  const shown = rows.filter((p) =>
    (tab === "all" || (tab === "open" ? p.state !== "done" && p.state !== "later" : p.state === tab)) &&
    (!needle || `${p.name} ${p.goal}`.toLowerCase().includes(needle)));

  const target = data.projects.find((p) => p.id === asking) ?? null;
  const confirm = async () => {
    if (!target) return;
    const id = target.id;
    setBusy(true);
    try {
      await onClose?.(target);
      setErrors((e) => { const n = { ...e }; delete n[id]; return n; });
    } catch (err) {
      setErrors((e) => ({ ...e, [id]: err instanceof ApiError ? err.message : NOT_CLOSED }));
    } finally {
      setBusy(false);
      setAsking(null);
    }
  };

  // a private project the person is not on is open work, so it shows where the open rows do
  const hiddenHere = !!team && !needle && (tab === "open" || tab === "all")
    && (view === "team" ? team.hidden.length > 0 : !!team.hidden_summary);
  const empty = data.projects.length === 0 && !team?.hidden.length;
  const none = needle ? "No project matches your search."
    : team && view === "mine" && (tab === "open" || tab === "all") ? "No projects of yours yet."
    : "No project is in this state.";
  return (
    <ProjectsHead>
      {empty ? (
        <EmptyState title="No projects yet" action={<NewProject />} className="border border-border bg-card" data-testid="projects-empty">
          Start one and hand it to the crew.
        </EmptyState>
      ) : (
        <div className="grid gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Input aria-label="Search projects" placeholder="Search by name or goal" value={query} data-testid="projects-search"
                   onChange={(e) => setQuery(e.target.value)} className="h-9 w-full max-w-[320px] bg-field" />
            {team && <Slot name="projects.switch" view={view} counts={team.counts} onView={setView} />}
          </div>
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList aria-label="Projects by state" className="h-auto flex-wrap">
              {TABS.map((t) => (
                <TabsTrigger key={t.key} value={t.key} data-testid={`tab-${t.key}`}
                             className="data-[state=active]:border-b-brand data-[state=active]:hover:border-b-brand">
                  {t.label}
                  <b className="ml-1 font-mono text-xs font-medium" data-testid="tab-count">{count(t.key)}</b>
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value={tab} className="mt-4">
              <div className="border border-border bg-card" data-testid="projects-list">
                {shown.length === 0 && !(hiddenHere && view === "team") && (
                  <div className="border-b border-border px-4 py-7 text-muted-foreground last:border-b-0" data-testid="projects-none">
                    {none}
                  </div>
                )}
                {shown.map((p) => <Row key={p.id} p={p} error={errors[p.id]} onClose={(r) => setAsking(r.id)} team={!!team} me={me} />)}
                {hiddenHere && <Slot name="projects.hidden" stubs={team.hidden} summary={team.hidden_summary} view={view} />}
                {tab === "open" && finished > 0 && (
                  <Link to="?state=done" data-testid="show-finished"
                        className="block border-t border-border px-4 py-3 text-[13px] text-foreground underline">
                    Show {finished} finished
                  </Link>
                )}
              </div>
            </TabsContent>
          </Tabs>
        </div>
      )}

      <Dialog open={target !== null} onOpenChange={(o) => { if (!o && !busy) setAsking(null); }} data-testid="close-dialog"
              title={`Close ${target?.name ?? ""}?`}
              description="It moves to Done. Handing it to the crew again opens it."
              footer={
                <>
                  <Button variant="secondary" disabled={busy} onClick={() => setAsking(null)}>Cancel</Button>
                  <Button busy={busy} onClick={confirm} data-testid="close-confirm">Close</Button>
                </>
              } />
    </ProjectsHead>
  );
}

/** The person's name as the cell has it, for "Led by you". */
function useMe(): string | undefined {
  const { personName } = useEdition();
  return personName && personName !== "You" ? personName : undefined;
}

const countsOf = (rows: FactoryProjectRow[]): FactoryProjectsData["counts"] => {
  const n = (s: ProjectState) => rows.filter((p) => p.state === s).length;
  return { all: rows.length, building: n("building"), waiting_on_you: n("waiting_on_you"), not_started: n("not_started"),
           ready_to_close: n("ready_to_close"), later: n("later"), done: n("done") };
};

export default function FactoryProjects() {
  const named = useSample();
  const ee = useEeSamples("projects");
  const eePending = useEePending();
  const eeSample = named && !isSample(named) && Object.prototype.hasOwnProperty.call(ee, named) ? ee[named] : null;
  // a team sample is ee's: while the chunk loads, wait for it instead of showing the live list and swapping
  if (named && !isSample(named) && !eeSample && eePending) {
    return <ProjectsHead><LoadingBlocks data-testid="projects-loading" /></ProjectsHead>;
  }
  return <ProjectsPage key={named ?? ""} named={named} eeSample={eeSample} />;
}

function ProjectsPage({ named, eeSample }: { named: string | null; eeSample: FactoryProjectsData | null }) {
  const sample = isSample(named) ? named : eeSample ? named : null;
  const [closedHere, setClosedHere] = React.useState<string[]>([]);
  const { data, error, reload } = usePagePolling<FactoryProjectsData>(
    () => (eeSample ? Promise.resolve(eeSample) : sample ? sampleLoad(sample) : factoryProjects()), POLL_MS);

  const shown = data && closedHere.reduce(sampleClosed, data);
  // a sample has no cell to close on: `fail` answers as the cell does, any other moves the row on this copy
  const close = async (p: FactoryProjectRow) => {
    if (sample) {
      if (sampleClose(sample).error) throw new ApiError(sampleClose(sample).error, 409);
      setClosedHere((c) => [...c, p.id]);
      return;
    }
    await api.finishProject(p.id, CLOSE_WORDS);
    reload();
  };

  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
      <b>Projects did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  if (!shown) return <ProjectsHead>{banner || <LoadingBlocks data-testid="projects-loading" />}</ProjectsHead>;
  return (
    <>
      {banner && <div className="mb-6">{banner}</div>}
      <FactoryProjectsView data={shown} onClose={close} start={sample ? sampleClose(sample) : undefined} />
    </>
  );
}

/** The title and New project, over what the page has to show. */
function ProjectsHead({ children }: { children?: React.ReactNode }) {
  return (
    <div className="grid gap-6 md:gap-8" data-testid="factory-projects">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Projects</h1>
          <p className="m-0 max-w-[640px] text-[15px] text-muted-foreground">Every project you handed to the crew.</p>
        </div>
        <NewProject />
      </div>
      {children}
    </div>
  );
}
