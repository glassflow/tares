import * as React from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Collapse, Dialog, EmptyState, Panel, SearchSelect, Skeleton, Toaster, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { useWho } from "../who";
import { brainDecisions, deleteDecision, type DecisionFilters, type DecisionRow, type DecisionsPage, type DecisionTab } from "./BrainDecisions.api";
import {
  ALL_PROJECTS, ALL_REPOS, ANY, DELETE_LINE, DELETE_TITLE, deleteErrorWords, emptyWords, HEADING,
  EMPTY_PAGE_END, EMPTY_PAGE_MORE, isOnCell, LOAD_FAILED_WORDS, LOADING_WORDS, NOT_SET_UP_WORDS, originLabel, rangeWords, RECORD_WORDS, safeLink, SUBTITLE, tabOf, TABS, titleOf,
  whenWords, originOf, whoParts,
} from "./brainDecisionsText";

// Decisions (TR-1444): every decision the brain holds, from repo folders, shared factory projects and
// ones added by hand, 25 to a page, narrowed by three dropdowns: Origin (with the cell's counts), Repo and
// Project, each kept in the address (`?where=`, `?repo=owner/name`, `?project=<id>`). Record a decision
// opens the form at /brain/decisions/new (BrainDecisionNew.tsx). The list is read once when the page
// opens and again after a delete, a change of filter or page, or coming back from the form with a new
// decision: building the counts walks every decision in the brain, so the page never reads by itself. Titles, summaries, names and paths are a person's own text and marked data-user-text.

const MUTED = "text-[13px] text-muted-foreground";
const ROW = "grid min-w-0 gap-x-4 gap-y-1 px-4 py-3 md:grid-cols-[minmax(0,1fr)_220px_170px] md:items-baseline";

interface ListState {
  /** What the rows are for; a read for another tab or repo is dropped when it lands late. */
  key: string;
  rows: DecisionRow[];
  next: string | null;
  counts: DecisionsPage["counts"] | null;
  filters: DecisionFilters | null;
  /** How many of `rows` the cell sent: a row recorded here and pinned on top is not one of them. */
  served: number;
}

type Load = { state: "loading" } | { state: "ready"; list: ListState } | { state: "error"; message: string; list?: ListState } | { state: "no-brain" };

function OriginCell({ r }: { r: DecisionRow }) {
  const o = originOf(r);
  if (!o) return <span />;
  const name = o.name && (
    !o.href ? <span data-user-text>{o.name}</span>
      : isOnCell(o.href) ? <Link to={o.href} className="text-foreground" data-user-text data-testid="brain-decision-origin-link">{o.name}</Link>
      : <a href={o.href} target="_blank" rel="noreferrer" className="text-foreground" data-user-text data-testid="brain-decision-origin-link">{o.name}</a>
  );
  return (
    <span className="min-w-0 text-[13px] [overflow-wrap:anywhere]" data-testid="brain-decision-origin">
      {o.label}
      {name && <span className="block">{name}</span>}
    </span>
  );
}

function Title({ r }: { r: DecisionRow }) {
  const href = safeLink(r.link);
  const cls = "font-semibold text-foreground no-underline hover:underline";
  if (!href) return <span className="font-semibold" data-user-text data-testid="brain-decision-title">{titleOf(r)}</span>;
  if (isOnCell(href)) return <Link to={href} className={cls} data-user-text data-testid="brain-decision-title">{titleOf(r)}</Link>;
  return <a href={href} target="_blank" rel="noreferrer" className={cls} data-user-text data-testid="brain-decision-title">{titleOf(r)}</a>;
}

function Row({ r, now, onDelete }: { r: DecisionRow; now: number; onDelete: (r: DecisionRow) => void }) {
  const who = whoParts(r);
  const when = whenWords(r, now);
  return (
    <div className={cn(ROW, "border-t border-border")} data-testid="brain-decision-row" data-id={r.id}>
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <Title r={r} />
        {r.summary && <div className={MUTED} data-user-text data-testid="brain-decision-summary">{r.summary}</div>}
      </div>
      <OriginCell r={r} />
      <div className="min-w-0 text-[13px] [overflow-wrap:anywhere]">
        {who.decided && <span className="block" data-user-text data-testid="brain-decision-who">{who.decided}</span>}
        {who.updated && <span className="block" data-user-text data-testid="brain-decision-updated">{who.updated}</span>}
        {when && <span className="block text-muted-foreground" data-testid="brain-decision-when">{when}</span>}
        {r.can_delete && (
          <Button variant="quiet" className="-ml-2 h-7 px-2 text-[13px]" onClick={() => onDelete(r)} data-testid="brain-decision-delete">Delete</Button>
        )}
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" className="grid gap-3 px-4 py-4" data-testid="brain-decisions-loading">
      <span className={MUTED}>{LOADING_WORDS}</span>
      {[0, 1, 2].map((i) => <Skeleton key={i} width={`${70 - i * 12}%`} height={12} />)}
    </div>
  );
}

function List({ load, tab, repo, project, canAdd, now, page, before, reading, onPage, onDelete, onRetry }: {
  load: Load; tab: DecisionTab; repo: string | null; project: string | null; canAdd: boolean; now: number;
  /** The page shown, from 0. */
  page: number;
  /** How many rows the earlier pages held. */
  before: number;
  /** A read is on its way: the pages wait for it, so a page is never counted from rows about to change. */
  reading: boolean;
  onPage: (to: number) => void; onDelete: (r: DecisionRow) => void; onRetry: () => void;
}) {
  const list = load.state === "ready" ? load.list : load.state === "error" ? load.list : undefined;
  return (
    <section aria-label="Decision list" className="min-w-0 border border-border bg-card" data-testid="brain-decisions-list">
      {load.state === "error" && (
        <div className="p-4">
          <Banner tone="error" action={<Button variant="secondary" onClick={onRetry} data-testid="brain-decisions-retry">Try again</Button>} data-testid="brain-decisions-error">
            <b>{LOAD_FAILED_WORDS}</b> <span className="text-foreground" data-user-text>{load.message}</span>
          </Banner>
        </div>
      )}
      {load.state === "loading" && <Loading />}
      {/* nothing at all only on a first page with no more after it; an empty page further on says so and keeps the pages */}
      {list && list.rows.length === 0 && page === 0 && !list.next && (
        <EmptyState className="px-4 py-4" data-testid="brain-decisions-empty">{emptyWords(tab, repo, canAdd, project)}</EmptyState>
      )}
      {list && list.rows.length === 0 && (page > 0 || list.next) && (
        <EmptyState className="px-4 py-4" data-testid="brain-decisions-empty-page">{list.next ? EMPTY_PAGE_MORE : EMPTY_PAGE_END}</EmptyState>
      )}
      {list && (list.rows.length > 0 || page > 0 || list.next) && (
        <>
          {list.rows.length > 0 && (
            <div className={cn(ROW, "hidden py-2 text-xs text-muted-foreground md:grid")} aria-hidden="true">
              <span>Decision</span><span>Origin</span><span>Decided by</span>
            </div>
          )}
          {list.rows.map((r) => <Row key={r.id} r={r} now={now} onDelete={onDelete} />)}
          {(page > 0 || list.next) && (
            <nav aria-label="Pages" className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3" data-testid="brain-decisions-pages">
              <span className={MUTED} data-testid="brain-decisions-range">
                {rangeWords(before + 1, before + list.rows.length, repo || project || !list.counts ? null : list.counts[tab])}
              </span>
              <div className="flex gap-2">
                <Button variant="secondary" disabled={page === 0 || load.state !== "ready" || reading} onClick={() => onPage(page - 1)} data-testid="brain-decisions-prev">Previous</Button>
                <Button variant="secondary" disabled={!list.next || load.state !== "ready" || reading} onClick={() => onPage(page + 1)} data-testid="brain-decisions-next">Next</Button>
              </div>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

export default function BrainDecisions() {
  const [params, setParams] = useSearchParams();
  const tab = tabOf(params.get("where"));
  const repo = (params.get("repo") ?? "").trim() || null;
  const project = (params.get("project") ?? "").trim() || null;
  const key = `${tab}|${repo ?? ""}|${project ?? ""}`;
  const { canWrite } = useWho();
  const location = useLocation();
  const navigate = useNavigate();
  const canAdd = canWrite !== false;
  const { toast } = useToast();

  const [load, setLoad] = React.useState<Load>({ state: "loading" });
  // the cursor of each page seen so far (the first is none), and the page shown; a new tab or repo starts at the first
  const [pages, setPages] = React.useState<{ key: string; cursors: (string | null)[]; firsts: number[]; at: number }>({ key, cursors: [null], firsts: [0], at: 0 });
  const at = pages.key === key ? pages.at : 0;
  const cursor = pages.key === key ? pages.cursors[at] ?? null : null;
  // how many rows come before the page shown, for "51 to 100 of 238"
  const before = pages.key === key ? pages.firsts[at] ?? 0 : 0;
  const listTop = React.useRef<HTMLDivElement>(null);
  // the dropdowns keep the last lists the cell gave while a new read is on its way
  const [filters, setFilters] = React.useState<DecisionFilters | null>(null);
  const [lastCounts, setLastCounts] = React.useState<DecisionsPage["counts"] | null>(null);
  const [reading, setReading] = React.useState(true);
  const [tick, setTick] = React.useState(0);
  const [asking, setAsking] = React.useState<DecisionRow | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState("");
  // rows added on this page: kept at the top until a read has them, so a new one never flickers out
  const added = React.useRef<DecisionRow[]>([]);
  const latest = React.useRef(0);

  const reload = React.useCallback(() => setTick((t) => t + 1), []);

  // one read per open, tab, repo or reload; an answer for an older one is dropped
  React.useEffect(() => {
    const mine = ++latest.current;
    const listKey = `${key}|${at}`;
    setLoad((l) => (l.state === "ready" && l.list.key === listKey ? l : { state: "loading" }));
    setReading(true);
    brainDecisions({ where: tab, repo, project, cursor }).then(
      (page) => {
        if (mine !== latest.current) return;
        const ids = new Set(page.decisions.map((r) => r.id));
        added.current = added.current.filter((r) => !ids.has(r.id));
        // a decision recorded here shows at the top of the first page until a read has it
        const fits = (r: DecisionRow) => at === 0 && !repo && !project && (tab === "all" || tab === r.where);
        setLoad({ state: "ready", list: { key: listKey, rows: [...added.current.filter(fits), ...page.decisions], next: page.next_cursor, counts: page.counts, filters: page.filters ?? null, served: page.decisions.length } });
        if (page.filters) setFilters(page.filters);
        setLastCounts(page.counts);
        setReading(false);
      },
      (e: unknown) => {
        if (mine !== latest.current) return;
        setReading(false);
        if (e instanceof ApiError && e.status === 404) { setLoad({ state: "no-brain" }); return; }
        const message = e instanceof ApiError && e.status !== 502 ? (e.message || "") : "The cell did not answer. Try again in a moment.";
        setLoad((l) => ({ state: "error", message, list: l.state === "ready" && l.list.key === listKey ? l.list : undefined }));
      },
    );
  }, [key, tab, repo, project, at, cursor, tick]);

  // A change of filter starts at the first page, also when it comes back to a filter seen before.
  React.useEffect(() => {
    setPages((p) => (p.key === key && p.at === 0 && p.cursors.length === 1 ? p : { key, cursors: [null], firsts: [0], at: 0 }));
  }, [key]);

  // Next keeps the cursor the read gave, so Previous goes back without asking for it again
  const goTo = (to: number) => {
    const next = load.state === "ready" ? load.list.next : null;
    const shown = load.state === "ready" ? load.list.served : 0;
    setPages((p) => {
      const cursors = p.key === key ? [...p.cursors] : [null];
      const firsts = p.key === key ? [...p.firsts] : [0];
      if (to === at + 1 && next) { cursors[to] = next; firsts[to] = (firsts[at] ?? 0) + shown; }
      return { key, cursors, firsts, at: Math.max(0, Math.min(to, cursors.length - 1)) };
    });
    listTop.current?.scrollIntoView({ block: "start" });
  };

  // back from the form with a new decision: it stays at the top until a read has it, and the address
  // drops the state so a reload does not say it again
  const justAdded = (location.state as { added?: DecisionRow } | null)?.added;
  React.useEffect(() => {
    if (!justAdded) return;
    added.current = [justAdded, ...added.current.filter((x) => x.id !== justAdded.id)];
    toast("Recorded.");
    navigate({ pathname: location.pathname, search: location.search }, { replace: true, state: null });
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [justAdded]);

  const confirmDelete = async () => {
    if (!asking) return;
    setDeleting(true); setDeleteError("");
    try {
      await deleteDecision(asking.id);
      added.current = added.current.filter((x) => x.id !== asking.id);
      setLoad((l) => (l.state === "ready" ? { state: "ready", list: { ...l.list, rows: l.list.rows.filter((x) => x.id !== asking.id) } } : l));
      setAsking(null);
      toast("Deleted.");
      reload();
    } catch (e) {
      const status = (e as { status?: number }).status;
      if (status === 404) { setAsking(null); toast(deleteErrorWords({ status })); reload(); }
      else setDeleteError(deleteErrorWords({ status }));
    } finally { setDeleting(false); }
  };

  // A repo belongs to GitHub repos and a project to Factory projects: picking one sets the origin, and
  // an origin that rules a repo or project out clears it.
  const setFilter = (change: { where?: string; repo?: string; project?: string }) => {
    const next = new URLSearchParams(params);
    const set = (k: string, v: string | undefined) => { if (!v || v === ANY || v === "all") next.delete(k); else next.set(k, v); };
    if (change.where !== undefined) {
      set("where", change.where);
      if (change.where !== "all" && change.where !== "repo_folder") next.delete("repo");
      if (change.where !== "all" && change.where !== "factory_project") next.delete("project");
    }
    if (change.repo !== undefined) {
      set("repo", change.repo);
      if (change.repo !== ANY) { next.set("where", "repo_folder"); next.delete("project"); }
    }
    if (change.project !== undefined) {
      set("project", change.project);
      if (change.project !== ANY) { next.set("where", "factory_project"); next.delete("repo"); }
    }
    setParams(next, { replace: true });
  };
  const filtered = tab !== "all" || repo !== null || project !== null;
  const clearFilters = () => setParams(new URLSearchParams(), { replace: true });

  // Rows are shown only for the filter and page they were read for: right after Next, the old page's rows
  // would be counted from the new page's position until the read lands, so the list shows loading instead.
  const here = `${key}|${at}`;
  const shown: Load = (load.state === "ready" && load.list.key !== here) ? { state: "loading" }
    : (load.state === "error" && load.list && load.list.key !== here) ? { state: "error", message: load.message } : load;
  const list = shown.state === "ready" ? shown.list : shown.state === "error" ? shown.list : undefined;
  // the last counts the cell gave, so Origin keeps its numbers while the next read is on its way
  const counts = list?.counts ?? lastCounts;
  const repoOptions = [{ value: ANY, label: ALL_REPOS },
    ...[...new Set([...(filters?.repos ?? []), ...(repo ? [repo.toLowerCase()] : [])])].map((r) => ({ value: r, label: r }))];
  const projectOptions = [{ value: ANY, label: ALL_PROJECTS },
    ...(filters?.projects ?? []).map((p) => ({ value: p.id, label: p.name })),
    ...(project && !(filters?.projects ?? []).some((p) => p.id === project) ? [{ value: project, label: project }] : [])];
  const now = Date.now();

  return (
    <div className="grid min-w-0 gap-5 md:gap-6" data-testid="brain-decisions">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">{HEADING}</h1>
          <p className="m-0 text-[15px] text-muted-foreground">{SUBTITLE}</p>
        </div>
        {canAdd && load.state !== "no-brain" && (
          <Button asChild><Link to="/brain/decisions/new" data-testid="brain-decision-record">{RECORD_WORDS}</Link></Button>
        )}
      </div>
      {load.state === "no-brain" ? (
        <Panel><EmptyState title="Not set up" data-testid="brain-decisions-not-set-up">{NOT_SET_UP_WORDS}</EmptyState></Panel>
      ) : (
        <>
          <div className="grid min-w-0 gap-3 scroll-mt-4" ref={listTop}>
            <div className="grid min-w-0 gap-3 sm:grid-cols-3 sm:items-end" role="group" aria-label="Filters" data-testid="brain-decisions-filters">
              <SearchSelect label="Origin" value={tab} onValueChange={(v) => setFilter({ where: v })} data-testid="brain-decisions-origin"
                            searchLabel="Search origins" noMatch="No origin matches."
                            options={TABS.map((t) => ({ value: t.value, label: originLabel(t.value, counts) }))} />
              <SearchSelect label="Repo" value={repo ? repo.toLowerCase() : ANY} onValueChange={(v) => setFilter({ repo: v })} options={repoOptions}
                            searchLabel="Search repos" noMatch="No repo matches." data-testid="brain-decisions-repo" />
              <SearchSelect label="Project" value={project ?? ANY} onValueChange={(v) => setFilter({ project: v })} options={projectOptions}
                            searchLabel="Search projects" noMatch="No project matches." data-testid="brain-decisions-project" />
            </div>
            {filtered && (
              <div>
                <Button variant="quiet" className="-ml-2 h-7 px-2 text-[13px]" onClick={clearFilters} data-testid="brain-decisions-clear">Clear filters</Button>
              </div>
            )}
            <List load={shown} tab={tab} repo={repo} project={project} canAdd={canAdd} now={now} page={at} before={before} reading={reading}
                  onPage={goTo} onDelete={(r) => { setDeleteError(""); setAsking(r); }} onRetry={reload} />
          </div>
        </>
      )}
      <Dialog open={asking !== null} onOpenChange={(o) => { if (!o && !deleting) setAsking(null); }} data-testid="brain-decision-delete-dialog"
              title={DELETE_TITLE} description={DELETE_LINE}
              footer={
                <>
                  <Button variant="secondary" disabled={deleting} onClick={() => setAsking(null)} data-testid="brain-decision-delete-cancel">Cancel</Button>
                  <Button busy={deleting} onClick={confirmDelete} data-testid="brain-decision-delete-confirm">Delete</Button>
                </>
              }>
        <p className="m-0 text-[14px] font-semibold [overflow-wrap:anywhere]" data-user-text>{asking ? titleOf(asking) : ""}</p>
        <Collapse>{deleteError && <Banner tone="error" data-testid="brain-decision-delete-failed">{deleteError}</Banner>}</Collapse>
      </Dialog>
      <Toaster />
    </div>
  );
}
