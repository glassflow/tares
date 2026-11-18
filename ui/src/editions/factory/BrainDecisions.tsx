import * as React from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Collapse, Dialog, EmptyState, Panel, Skeleton, Tabs, Toaster, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { brainDecisions, deleteDecision, type DecisionRow, type DecisionsPage, type DecisionTab } from "./BrainDecisions.api";
import {
  DELETE_LINE, DELETE_TITLE, deleteErrorWords, emptyWords, HEADING,
  isOnCell, LOAD_FAILED_WORDS, LOADING_WORDS, NOT_SET_UP_WORDS, RECORD_WORDS, safeLink, SUBTITLE, tabLabel, tabOf, TABS, titleOf,
  whenWords, originOf, whoParts,
} from "./brainDecisionsText";

// Decisions (TR-1444): every decision the brain holds, from repo folders, shared factory projects and
// ones added by hand, in tabs with the cell's counts. Record a decision opens the form at
// /brain/decisions/new (BrainDecisionNew.tsx). The list is read once when the page opens and again after a
// delete, a change of tab, or coming back from the form with a new decision: building the counts
// walks every decision in the brain, so the page never reads by itself. `?repo=owner/name` narrows the
// list to one repo. Titles, summaries, names and paths are a person's own text and marked data-user-text.

const MUTED = "text-[13px] text-muted-foreground";
const ROW = "grid min-w-0 gap-x-4 gap-y-1 px-4 py-3 md:grid-cols-[minmax(0,1fr)_220px_170px] md:items-baseline";

interface ListState {
  /** What the rows are for; a read for another tab or repo is dropped when it lands late. */
  key: string;
  rows: DecisionRow[];
  next: string | null;
  counts: DecisionsPage["counts"] | null;
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

function List({ load, tab, repo, canAdd, now, more, loadingMore, onMore, onDelete, onRetry }: {
  load: Load; tab: DecisionTab; repo: string | null; canAdd: boolean; now: number; more: boolean; loadingMore: boolean;
  onMore: () => void; onDelete: (r: DecisionRow) => void; onRetry: () => void;
}) {
  const list = load.state === "ready" ? load.list : load.state === "error" ? load.list : undefined;
  return (
    <section aria-label="Decision list" className="min-w-0 border border-t-0 border-border bg-card" data-testid="brain-decisions-list">
      {load.state === "error" && (
        <div className="p-4">
          <Banner tone="error" action={<Button variant="secondary" onClick={onRetry} data-testid="brain-decisions-retry">Try again</Button>} data-testid="brain-decisions-error">
            <b>{LOAD_FAILED_WORDS}</b> <span className="text-foreground" data-user-text>{load.message}</span>
          </Banner>
        </div>
      )}
      {load.state === "loading" && <Loading />}
      {list && list.rows.length === 0 && (
        <EmptyState className="px-4 py-4" data-testid="brain-decisions-empty">{emptyWords(tab, repo, canAdd)}</EmptyState>
      )}
      {list && list.rows.length > 0 && (
        <>
          <div className={cn(ROW, "hidden py-2 text-xs text-muted-foreground md:grid")} aria-hidden="true">
            <span>Decision</span><span>Origin</span><span>Decided by</span>
          </div>
          {list.rows.map((r) => <Row key={r.id} r={r} now={now} onDelete={onDelete} />)}
          {more && (
            <div className="border-t border-border px-4 py-3">
              <Button variant="secondary" busy={loadingMore} onClick={onMore} data-testid="brain-decisions-more">Show more</Button>
            </div>
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
  const key = `${tab}|${repo ?? ""}`;
  const { canWrite } = useEdition();
  const location = useLocation();
  const navigate = useNavigate();
  const canAdd = canWrite !== false;
  const { toast } = useToast();

  const [load, setLoad] = React.useState<Load>({ state: "loading" });
  const [loadingMore, setLoadingMore] = React.useState(false);
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
    setLoad((l) => (l.state === "ready" && l.list.key === key ? l : { state: "loading" }));
    brainDecisions({ where: tab, repo }).then(
      (page) => {
        if (mine !== latest.current) return;
        const ids = new Set(page.decisions.map((r) => r.id));
        added.current = added.current.filter((r) => !ids.has(r.id));
        const fits = (r: DecisionRow) => !repo && (tab === "all" || tab === r.where);
        setLoad({ state: "ready", list: { key, rows: [...added.current.filter(fits), ...page.decisions], next: page.next_cursor, counts: page.counts } });
      },
      (e: unknown) => {
        if (mine !== latest.current) return;
        if (e instanceof ApiError && e.status === 404) { setLoad({ state: "no-brain" }); return; }
        const message = e instanceof ApiError && e.status !== 502 ? (e.message || "") : "The cell did not answer. Try again in a moment.";
        setLoad((l) => ({ state: "error", message, list: l.state === "ready" && l.list.key === key ? l.list : undefined }));
      },
    );
  }, [key, tab, repo, tick]);

  const more = async () => {
    if (load.state !== "ready" || !load.list.next) return;
    const list = load.list;
    setLoadingMore(true);
    try {
      const page = await brainDecisions({ where: tab, repo, cursor: list.next });
      const have = new Set(list.rows.map((r) => r.id));
      setLoad((l) => (l.state === "ready" && l.list.key === list.key
        ? { state: "ready", list: { ...l.list, rows: [...l.list.rows, ...page.decisions.filter((r) => !have.has(r.id))], next: page.next_cursor, counts: page.counts } }
        : l));
    } catch {
      toast("More decisions did not load. Try again.");
    } finally { setLoadingMore(false); }
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

  const setTab = (v: string) => {
    const next = new URLSearchParams(params);
    if (v === "all") next.delete("where"); else next.set("where", v);
    setParams(next, { replace: true });
  };
  const clearRepo = () => {
    const next = new URLSearchParams(params);
    next.delete("repo");
    setParams(next, { replace: true });
  };

  const list = load.state === "ready" ? load.list : load.state === "error" ? load.list : undefined;
  const counts = list?.counts ?? null;
  const now = Date.now();

  return (
    <div className="grid min-w-0 gap-5 md:gap-6" data-testid="brain-decisions">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">{HEADING}</h1>
          <p className="m-0 max-w-[640px] text-[15px] text-muted-foreground">{SUBTITLE}</p>
        </div>
        {canAdd && load.state !== "no-brain" && (
          <Button asChild><Link to="/brain/decisions/new" data-testid="brain-decision-record">{RECORD_WORDS}</Link></Button>
        )}
      </div>
      {load.state === "no-brain" ? (
        <Panel><EmptyState title="Not set up" data-testid="brain-decisions-not-set-up">{NOT_SET_UP_WORDS}</EmptyState></Panel>
      ) : (
        <>
          <div className="min-w-0">
            {repo && (
              <p className={cn("m-0 mb-3 flex flex-wrap items-center gap-x-2", MUTED)} data-testid="brain-decisions-repo-filter">
                <span>Decisions from <span className="font-mono text-foreground" data-user-text>{repo}</span>. The counts are for the whole cell.</span>
                <Button variant="quiet" className="h-7 px-2 text-[13px]" onClick={clearRepo} data-testid="brain-decisions-repo-clear">Show every repo</Button>
              </p>
            )}
            <Tabs aria-label="Where from" value={tab} onValueChange={setTab} data-testid="brain-decisions-tabs"
                  items={TABS.map((t) => ({
                    value: t.value,
                    label: tabLabel(t.value, counts, true),
                    content: (
                      <List load={load} tab={tab} repo={repo} canAdd={canAdd} now={now} more={Boolean(list?.next)} loadingMore={loadingMore}
                            onMore={more} onDelete={(r) => { setDeleteError(""); setAsking(r); }} onRetry={reload} />
                    ),
                  }))} />
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
