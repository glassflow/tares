import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, ApiError } from "../../api";
import { Banner, Button, Dialog, EmptyState, LoadingBlocks, ProgressBar, STATUS_TONES, StatusLabel } from "../../components/edition";
import { Input } from "../../components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import { factoryProjects, type FactoryProjectRow, type FactoryProjectsData, type ProjectState } from "./FactoryProjects.api";
import { InlineIcon } from "./ProjectIcon";
import { isSample, sampleClose, sampleClosed, sampleLoad } from "./projectsSamples";

// Projects: every software factory project as one row, from one read (GET /api/factory/projects).
// The filter lives in the address (?state=) and the search box filters the rows already read, so
// neither makes another call. A project whose tickets are all merged has a Close button. Every
// string from the cell is a React text node; a name, goal, repo and milestone are the person's own
// text and marked data-user-text.

const POLL_MS = 15_000;
const NOT_CLOSED = "The project was not closed. Try again in a moment.";
const CLOSE_WORDS = "Closed from the Projects list";

type Filter = "open" | "all" | ProjectState;
const TABS: { key: Filter; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "waiting_on_you", label: "Waiting on you" },
  { key: "building", label: "Building" },
  { key: "not_started", label: "Not started" },
  { key: "ready_to_close", label: "Ready to close" },
  { key: "later", label: "Later" },
  { key: "done", label: "Done" },
  { key: "all", label: "All" },
];
const isFilter = (v: string | null): v is Filter => TABS.some((t) => t.key === v);

const pct = (n: number, of: number) => (of > 0 ? Math.round((n / of) * 100) : 0);

function Row({ p, error, onClose }: { p: FactoryProjectRow; error?: string; onClose: (p: FactoryProjectRow) => void }) {
  const { merged, blocked, total } = p.tickets;
  const repos = p.repo_names.join(", ");
  return (
    <div className="border-b border-border px-4 py-3.5 last:border-b-0 hover:bg-[var(--row-hover)]" data-testid="project-row" data-state={p.state} data-project={p.id}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <InlineIcon icon={p.icon} />
          <Link to={`/projects/${encodeURIComponent(p.id)}`} data-user-text data-testid="project-name"
                className="font-bold [overflow-wrap:anywhere] text-foreground! underline! decoration-border! underline-offset-[3px]">
            {p.name}
          </Link>
          <StatusLabel data-testid="project-state" tone={STATUS_TONES.project[p.state]} className="ml-2 align-[1px]">
            {p.state_label}
          </StatusLabel>
        </div>
        {p.can_close && (
          <Button variant="secondary" data-testid="project-close" onClick={() => onClose(p)}
                  className="h-[30px] shrink-0 border-foreground! bg-[var(--input-bg)]!">
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
      <ProgressBar value={pct(merged, total)} blocked={pct(blocked, total)} aria-label={`${merged} of ${total} tickets merged`}
                   className={cn("my-2 [&>div:first-child]:bg-foreground", p.state === "done" && "[&>div:first-child]:bg-[color:var(--ink-faint)]!")} />
      <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground" data-testid="project-tickets">
        <span>{merged} of {total} tickets merged</span>
        {blocked > 0 && <span className="text-[color:var(--err)]">{blocked} blocked</span>}
      </div>
      {p.milestone && <div data-user-text data-testid="project-milestone" className="mt-0.5 text-xs text-muted-foreground [overflow-wrap:anywhere]">{p.milestone}</div>}
      {error && <div role="alert" data-user-text data-testid="project-error" className="mt-2 text-[13px] text-[color:var(--err)] [overflow-wrap:anywhere]">{error}</div>}
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

  const setTab = (k: string) => setParams((q) => {
    const n = new URLSearchParams(q);
    if (k === "open") n.delete("state"); else n.set("state", k);
    return n;
  });
  // Open is what is being worked on: not finished, and not kept for later
  const count = (k: Filter) => k === "all" ? data.counts.all
    : k === "open" ? data.counts.all - data.counts.done - (data.counts.later ?? 0) : (data.counts[k] ?? 0);
  const finished = data.counts.done;

  const needle = query.trim().toLowerCase();
  const shown = data.projects.filter((p) =>
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

  const empty = data.projects.length === 0;
  return (
    <ProjectsHead>
      {empty ? (
        <EmptyState title="No projects yet" action={<NewProject />} className="border border-border bg-card" data-testid="projects-empty">
          Start one and hand it to the crew.
        </EmptyState>
      ) : (
        <div className="grid gap-4">
          <Input aria-label="Search projects" placeholder="Search by name or goal" value={query} data-testid="projects-search"
                 onChange={(e) => setQuery(e.target.value)} className="h-9 w-full max-w-[320px] bg-[var(--input-bg)]!" />
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList aria-label="Projects by state" className="h-auto flex-wrap">
              {TABS.map((t) => (
                <TabsTrigger key={t.key} value={t.key} data-testid={`tab-${t.key}`}
                             className="data-[state=active]:border-b-[color:var(--accent)]! data-[state=active]:hover:border-b-[color:var(--accent)]!">
                  {t.label}
                  <b className="ml-1 font-mono text-xs font-medium" data-testid="tab-count">{count(t.key)}</b>
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value={tab} className="mt-4">
              <div className="border border-border bg-card" data-testid="projects-list">
                {shown.length === 0 && (
                  <div className="px-4 py-7 text-muted-foreground" data-testid="projects-none">
                    {needle ? "No project matches your search." : "No project is in this state."}
                  </div>
                )}
                {shown.map((p) => <Row key={p.id} p={p} error={errors[p.id]} onClose={(r) => setAsking(r.id)} />)}
                {tab === "open" && finished > 0 && (
                  <Link to="?state=done" data-testid="show-finished"
                        className="block border-t border-border px-4 py-3 text-[13px] text-foreground! underline!">
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

export default function FactoryProjects() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [closedHere, setClosedHere] = React.useState<string[]>([]);
  const { data, error, reload } = usePagePolling<FactoryProjectsData>(
    () => (sample ? sampleLoad(sample) : factoryProjects()), POLL_MS);

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
          <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Projects</h1>
          <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Every project you handed to the crew.</p>
        </div>
        <NewProject />
      </div>
      {children}
    </div>
  );
}
