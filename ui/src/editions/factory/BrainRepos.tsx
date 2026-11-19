import * as React from "react";
import { Link } from "react-router-dom";

import { Banner, Button, Collapse, Dialog, EmptyState, Panel, ProgressBar, Skeleton, StatusLabel, Toaster, type Tone, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { brainRepos, removeBrainRepo, retryBrainRepo, type BrainReposData, type BrainRepoRow } from "./BrainRepos.api";
import { BRAIN_SAMPLES, isBrainSample, SAMPLE_FOLDERS, SAMPLE_NOW, SAMPLE_UPDATES } from "./brainSamples";
import { repoFolders, repoUpdates, saveRepoFolders, type FolderScan, type RepoUpdate } from "./BrainFolders.api";
import { FolderList } from "./BrainFolders";
import {
  commitHref, decisionsHref, decisionsWhere, filesWords, HISTORY_FAILED, HISTORY_EMPTY, HISTORY_LINE, HISTORY_TITLE, NO_DECISIONS_WORDS,
  NOT_ADMIN_403, PICK_FOLDER_WORDS, REMOVING_409, SAVE_FAILED, updatedWords, updateWords,
} from "./brainFoldersText";
import { formatStampUTC } from "../../lib/time";
import {
  actionError, addedParts, ago, cardKind, count, EMPTY_WORDS, NOT_SET_UP_WORDS, plainError, percent, pollMs, POLL_SLOW_MS, queuedWords,
  REMOVE_WORDS, REMOVING_WORDS, shortCommit, STATE_WORDS, timeOf, type CardKind,
} from "./brainText";

// Repos (TR-1204), the first Second Brain page: every repo the brain knows, as a card, from one read
// (GET /api/brain), repeated every 10 seconds while a repo is queued, cloning, indexing or being
// removed and every minute otherwise. Connect, Retry and Remove are for an admin key; anyone else
// sees the cards. Repo names, who added a repo and the cell's error words are a person's or the
// cell's own text and marked data-user-text.

const NOT_DONE = "That did not go through. Try again in a moment.";
const SAMPLE_NOTE = "This is a sample, so nothing was sent.";

const TONE: Record<CardKind, Tone> = {
  queued: "quiet", cloning: "quiet", indexing: "quiet", ready: "ready", failed: "bad", removing: "quiet", remove_failed: "bad",
};
// Cloning and Indexing are drawn in the page's own ink, as in the approved mock; only Ready is green, a failure red.
const INK = new Set<CardKind>(["cloning", "indexing"]);
const MUTED = "m-0 text-[13px] text-muted-foreground";
const SMALL_BUTTON = "h-8 px-3 text-[13px]";
const MONO = "font-mono text-[12.5px]";

/** "Updated 22 minutes ago, 3 files changed. 1,507 files." on a Ready card. */
const readyLine = (updated: string, changed: string, files: number): string =>
  [updated && updated + changed, `${count(files)} ${files === 1 ? "file" : "files"}`].filter(Boolean).join(". ") + ".";

function Lines({ r, all, now, admin, onHistory }: { r: BrainRepoRow; all: BrainRepoRow[]; now: number; admin: boolean; onHistory: (repo: string) => void }) {
  const added = addedParts(r, now);
  const started = ago(r.added_at, now);
  switch (cardKind(r)) {
    case "queued": {
      const q = queuedWords(r, all);
      return (
        <p className={MUTED} data-testid="brain-line">
          {q.waitsFor ? <>Starts when <span className="font-mono" data-user-text>{q.waitsFor}</span> {q.line}</> : q.line}
          {added.when && <> Added{added.who && <> by <span data-user-text>{added.who}</span></>} {added.when}.</>}
        </p>
      );
    }
    case "cloning":
    case "indexing": {
      const known = r.files_total > 0;
      return (
        <>
          <div>
            <ProgressBar value={percent(r)} aria-label={`Progress of ${r.repo}`} data-testid="brain-progress" />
            <div className="mt-1.5 text-[13px]" data-testid="brain-files">
              {known ? `${count(r.files_done)} of ${count(r.files_total)} files` : cardKind(r) === "cloning" ? "Fetching the code." : "Counting files."}
            </div>
          </div>
          {started && <p className={MUTED} data-testid="brain-line">Started {started}.</p>}
        </>
      );
    }
    case "ready": {
      const href = commitHref(r.repo, r.indexed_commit);
      const at = timeOf(r.last_update?.at ?? r.last_synced);
      const last = r.last_update;
      const changed = last && last.ok && last.kind === "update" ? `, ${filesWords(last.files_changed)} changed` : "";
      const folders = r.folders ?? [];
      return (
        <div className="grid gap-1 text-[13px]">
          <p className="m-0" data-testid="brain-synced">
            Up to date with <span className={MONO}>main</span>
            {r.indexed_commit && <> at {href
              ? <a href={href} target="_blank" rel="noreferrer" className={cn(MONO, "text-foreground underline")} data-testid="brain-commit">{shortCommit(r.indexed_commit)}</a>
              : <span className={MONO} data-testid="brain-commit">{shortCommit(r.indexed_commit)}</span>}</>}
          </p>
          <p className="m-0 text-muted-foreground" data-testid="brain-updated">
            {readyLine(updatedWords(at, now), changed, r.files_total)}{" "}
            <Button variant="quiet" className="inline h-auto p-0 align-baseline text-[13px] text-foreground underline hover:bg-transparent" onClick={() => onHistory(r.repo)} data-testid="brain-history">History</Button>
          </p>
          <p className="m-0" data-testid="brain-decisions">
            Decisions: {folders.length > 0 ? (
              <><Link to={decisionsHref(r.repo)} className="text-foreground underline" data-testid="brain-decisions-link">{count(r.decisions ?? 0)}</Link>{" "}
                from {folders.length === 1 ? <span className={MONO} data-user-text>{decisionsWhere(folders)}</span> : decisionsWhere(folders)}</>
            ) : <>{NO_DECISIONS_WORDS}{admin && <> {PICK_FOLDER_WORDS}</>}</>}
          </p>
          {r.sync_failures > 0 && (
            <p className={MUTED} data-testid="brain-sync-note">
              {r.sync_failures === 1 ? "The last update from main did not go through." : `The last ${r.sync_failures} updates from main did not go through.`}
              {r.sync_error && <> <span data-user-text>{plainError(r.sync_error, "")}</span></>}
            </p>
          )}
        </div>
      );
    }
    case "failed":
      return (
        <p className="m-0 text-[13px]" data-user-text data-testid="brain-error">
          {r.error || "The index did not finish. Try again."}
        </p>
      );
    case "removing":
      return <p className={MUTED} data-testid="brain-line">{REMOVING_WORDS}</p>;
    case "remove_failed":
      return <p className="m-0 text-[13px]" data-user-text data-testid="brain-remove-error">{r.remove_error}</p>;
  }
}

function Card({ r, all, now, admin, busy, onRetry, onRemove, onRetryRemoval, onFolders, onHistory }: {
  r: BrainRepoRow; all: BrainRepoRow[]; now: number; admin: boolean; busy: boolean;
  onRetry: (repo: string) => void; onRemove: (repo: string) => void; onRetryRemoval: (repo: string) => void;
  onFolders: (repo: string) => void; onHistory: (repo: string) => void;
}) {
  const kind = cardKind(r);
  return (
    <article className={cn("flex min-w-0 flex-col gap-2.5 border bg-card p-4 [overflow-wrap:anywhere]",
                           TONE[kind] === "bad" ? "border-err-border" : "border-border")}
             data-testid="brain-card" data-repo={r.repo} data-state={kind}>
      <div className="flex items-start justify-between gap-2">
        <h2 className="m-0 text-[15px] font-medium leading-snug" data-user-text data-testid="brain-name">{r.repo}</h2>
        <StatusLabel tone={TONE[kind]} className={cn("shrink-0 font-sans text-[13px] font-semibold normal-case tracking-normal", INK.has(kind) && "text-foreground")} data-testid="brain-state">
          {STATE_WORDS[kind]}
        </StatusLabel>
      </div>
      <Lines r={r} all={all} now={now} admin={admin} onHistory={onHistory} />
      {admin && (kind === "ready" || kind === "failed" || kind === "remove_failed") && (
        <div className="flex flex-wrap gap-2">
          {kind === "ready" && <Button variant="secondary" disabled={busy} className={SMALL_BUTTON} onClick={() => onFolders(r.repo)} data-testid="brain-folders">Decision folders</Button>}
          {kind === "failed" && <Button variant="secondary" busy={busy} className={cn(SMALL_BUTTON, "border-foreground")} onClick={() => onRetry(r.repo)} data-testid="brain-retry">Retry</Button>}
          {kind === "remove_failed" && <Button variant="secondary" busy={busy} className={cn(SMALL_BUTTON, "border-foreground")} onClick={() => onRetryRemoval(r.repo)} data-testid="brain-retry-removal">Retry removal</Button>}
          {kind !== "remove_failed" && <Button variant="secondary" disabled={busy} className={SMALL_BUTTON} onClick={() => onRemove(r.repo)} data-testid="brain-remove">Remove</Button>}
        </div>
      )}
    </article>
  );
}

/** The Decision folders dialog of a Ready card (admins): the chosen folders, then the ones Tares found, then
 *  one to type; Save sends the full list. The cell answers 409 while the repo is being removed. */
function FoldersDialog({ repo, sample, now, onClose, onSaved }: {
  repo: string | null; sample: boolean; now: number; onClose: () => void; onSaved: () => void;
}) {
  const [scan, setScan] = React.useState<FolderScan | null>(null);
  const [readError, setReadError] = React.useState("");
  const [selected, setSelected] = React.useState<string[]>([]);
  const [saving, setSaving] = React.useState(false);
  const [failure, setFailure] = React.useState("");
  React.useEffect(() => {
    if (!repo) return;
    let live = true;
    setScan(null); setReadError(""); setFailure(""); setSaving(false);
    const read = sample ? Promise.resolve(SAMPLE_FOLDERS(repo)) : repoFolders(repo);
    read.then((s) => { if (live) { setScan(s); setSelected(s.folders.map((f) => f.path)); } },
              (e) => { if (live) setReadError(plainError((e as { message?: string }).message, NOT_DONE)); });
    return () => { live = false; };
  }, [repo, sample]);
  const save = async () => {
    if (!repo) return;
    if (sample) { onSaved(); return; }
    setSaving(true); setFailure("");
    try { await saveRepoFolders(repo, selected); onSaved(); }
    catch (e) {
      const st = (e as { status?: number }).status;
      setFailure(st === 409 ? REMOVING_409 : st === 403 ? NOT_ADMIN_403 : plainError((e as { message?: string }).message, SAVE_FAILED));
    } finally { setSaving(false); }
  };
  return (
    <Dialog open={repo !== null} onOpenChange={(o) => { if (!o && !saving) onClose(); }} data-testid="brain-folders-dialog"
            title="Decision folders" description="The folders whose Markdown files the brain keeps as this repo's decisions."
            footer={
              <>
                <Button variant="secondary" disabled={saving} onClick={onClose} data-testid="brain-folders-cancel">Cancel</Button>
                <Button busy={saving} disabled={scan === null && !readError} onClick={save} data-testid="brain-folders-save">Save</Button>
              </>
            }>
      <p className="m-0 font-mono text-[14px] [overflow-wrap:anywhere]" data-user-text>{repo}</p>
      <FolderList scan={scan} readError={readError || undefined} selected={selected} onChange={setSelected} disabled={saving} now={now} data-testid="brain-folders-list" />
      <Collapse>{failure && <Banner tone="error" data-testid="brain-folders-failed"><span data-user-text>{failure}</span></Banner>}</Collapse>
    </Dialog>
  );
}

/** A repo's first index and its updates since, newest first (TR-1463). Anyone can open it. */
function HistoryDialog({ repo, sample, now, onClose }: { repo: string | null; sample: boolean; now: number; onClose: () => void }) {
  const [updates, setUpdates] = React.useState<RepoUpdate[] | null>(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
    if (!repo) return;
    let live = true;
    setUpdates(null); setFailed(false);
    const read = sample ? Promise.resolve({ repo, updates: SAMPLE_UPDATES }) : repoUpdates(repo);
    read.then((r) => { if (live) setUpdates(r.updates); }, () => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [repo, sample]);
  return (
    <Dialog open={repo !== null} onOpenChange={(o) => { if (!o) onClose(); }} data-testid="brain-history-dialog"
            title={HISTORY_TITLE} description={HISTORY_LINE}
            footer={<Button variant="secondary" onClick={onClose} data-testid="brain-history-close">Close</Button>}>
      <p className="m-0 font-mono text-[14px] [overflow-wrap:anywhere]" data-user-text>{repo}</p>
      {failed ? <Banner tone="error" data-testid="brain-history-failed">{HISTORY_FAILED}</Banner>
        : updates === null ? <p className={MUTED} role="status">Loading the history…</p>
        : updates.length === 0 ? <p className={MUTED} data-testid="brain-history-empty">{HISTORY_EMPTY}</p>
        : (
          <ol className="m-0 max-h-[50vh] list-none overflow-y-auto border-t border-border p-0" data-testid="brain-history-list">
            {updates.map((u, i) => {
              const href = repo ? commitHref(repo, u.indexed_commit) : "";
              const at = timeOf(u.at);
              return (
                <li key={i} className="grid gap-0.5 border-b border-border py-2 text-[13px]" data-testid="brain-history-row" data-ok={u.ok ? "1" : "0"}>
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <span className={cn("font-medium", !u.ok && "text-err")}>{updateWords(u)}</span>
                    {Number.isFinite(at) && <time dateTime={u.at} title={formatStampUTC(at)} className="text-muted-foreground">{ago(u.at, now)}</time>}
                  </div>
                  {u.indexed_commit && (
                    <span className="text-muted-foreground">at {href
                      ? <a href={href} target="_blank" rel="noreferrer" className={cn(MONO, "text-foreground underline")}>{shortCommit(u.indexed_commit)}</a>
                      : <span className={MONO}>{shortCommit(u.indexed_commit)}</span>}</span>
                  )}
                  {!u.ok && u.error && <span data-user-text>{plainError(u.error, "")}</span>}
                </li>
              );
            })}
          </ol>
        )}
    </Dialog>
  );
}

function ConnectButton() {
  return <Button asChild><Link to="/brain/connect" data-testid="brain-connect">Connect a repo</Link></Button>;
}

const NONE: ReadonlySet<string> = new Set();

export function BrainReposView({ data, admin, now, busy = NONE, onRetry = () => {}, onRemove = () => {}, onRetryRemoval = () => {}, onFolders = () => {}, onHistory = () => {} }: {
  data: BrainReposData; admin: boolean;
  /** The clock the "9 minutes ago" words count from (ms); a sample passes a fixed one. */
  now: number;
  /** The repos whose button was pressed and has not been answered yet, each until its own call ends. */
  busy?: ReadonlySet<string>;
  onRetry?: (repo: string) => void; onRemove?: (repo: string) => void; onRetryRemoval?: (repo: string) => void;
  onFolders?: (repo: string) => void; onHistory?: (repo: string) => void;
}) {
  if (!data.configured) {
    return (
      <BrainHead>
        <Panel><EmptyState title="Not set up" data-testid="brain-not-set-up">{NOT_SET_UP_WORDS}</EmptyState></Panel>
      </BrainHead>
    );
  }
  if (data.repos.length === 0) {
    return (
      <BrainHead>
        <Panel>
          <EmptyState title="No repos yet" action={admin ? <ConnectButton /> : undefined} data-testid="brain-empty">{EMPTY_WORDS}</EmptyState>
        </Panel>
      </BrainHead>
    );
  }
  return (
    <BrainHead action={admin ? <ConnectButton /> : undefined}>
      <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))]" data-testid="brain-cards">
        {data.repos.map((r) => (
          <Card key={r.repo} r={r} all={data.repos} now={now} admin={admin} busy={busy.has(r.repo)}
                onRetry={onRetry} onRemove={onRemove} onRetryRemoval={onRetryRemoval} onFolders={onFolders} onHistory={onHistory} />
        ))}
      </div>
    </BrainHead>
  );
}

function BrainHead({ children, action }: { children?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="grid gap-5 md:gap-6" data-testid="brain-repos">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Repos</h1>
          <p className="m-0 text-[15px] text-muted-foreground">
            The code the brain knows. Each repo is indexed once, then kept up to date with every push to main.
          </p>
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))]" data-testid="brain-loading">
      {[0, 1, 2].map((i) => (
        <div key={i} className="grid gap-3 border border-border bg-card p-4"><Skeleton width="55%" height={14} /><Skeleton width="80%" height={8} /><Skeleton width="65%" height={10} /></div>
      ))}
    </div>
  );
}

const sampleLoad = (name: string): Promise<BrainReposData> =>
  name === "loading" ? new Promise(() => {})
    : name === "error" ? Promise.reject(new Error("sample"))
    : Promise.resolve(structuredClone(BRAIN_SAMPLES[name]));

export default function BrainRepos() {
  const named = useSample();
  const sample = isBrainSample(named) ? named : null;
  const { admin } = useEdition();
  const { toast } = useToast();
  const [ms, setMs] = React.useState(POLL_SLOW_MS);
  const { data, error, reload } = usePagePolling<BrainReposData>(() => (sample ? sampleLoad(sample) : brainRepos()), ms);
  // The read picks its own pace from what it last saw. A change of pace reads once straight away.
  React.useEffect(() => { if (data) setMs(pollMs(data)); }, [data]);

  const [asking, setAsking] = React.useState<string | null>(null);
  const [foldersFor, setFoldersFor] = React.useState<string | null>(null);
  const [historyFor, setHistoryFor] = React.useState<string | null>(null);
  const [askError, setAskError] = React.useState("");
  // Each repo is busy until its own call ends: a press on another card does not free the first one.
  const [busy, setBusy] = React.useState<ReadonlySet<string>>(NONE);
  const mark = (repo: string, on: boolean) => setBusy((s) => { const n = new Set(s); if (on) n.add(repo); else n.delete(repo); return n; });
  const asked = asking !== null && busy.has(asking);
  const canAct = admin === true;

  // A 404 means the repo is already gone: say so in a plain sentence and read again.
  const failed = (e: unknown) => {
    const words = actionError(e as { message?: string; status?: number }, NOT_DONE);
    if ((e as { status?: number }).status === 404) reload();
    return words;
  };
  const retry = async (repo: string) => {
    if (sample) { toast(SAMPLE_NOTE); return; }
    mark(repo, true);
    try { await retryBrainRepo(repo); reload(); } catch (e) { toast(failed(e)); } finally { mark(repo, false); }
  };
  // Remove and Retry removal both end in DELETE. 200 is gone, 202 is a removal in the background:
  // either way read again, and the card says what is left.
  const remove = async (repo: string, fromDialog: boolean) => {
    if (sample) { setAsking(null); toast(SAMPLE_NOTE); return; }
    mark(repo, true); setAskError("");
    try {
      const answer = await removeBrainRepo(repo);
      setAsking(null); reload();
      if (answer.removed) toast(`Removed ${repo}.`);
    } catch (e) {
      const words = failed(e);
      if (fromDialog) setAskError(words); else toast(words);
    } finally { mark(repo, false); }
  };

  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
      <b>Repos did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  const now = sample ? Date.parse(SAMPLE_NOW) : Date.now();
  return (
    <>
      {!data ? <BrainHead>{banner || <Loading />}</BrainHead> : (
        <>
          {banner && <div className="mb-6">{banner}</div>}
          <BrainReposView data={data} admin={canAct} now={now} busy={busy}
                          onRetry={retry} onRemove={(r) => { setAskError(""); setAsking(r); }} onRetryRemoval={(r) => remove(r, false)}
                          onFolders={(r) => setFoldersFor(r)} onHistory={(r) => setHistoryFor(r)} />
        </>
      )}
      <Dialog open={asking !== null} onOpenChange={(o) => { if (!o && !asked) setAsking(null); }} data-testid="brain-remove-dialog"
              title="Remove this repo?" description={REMOVE_WORDS}
              footer={
                <>
                  <Button variant="secondary" disabled={asked} onClick={() => setAsking(null)} data-testid="brain-remove-cancel">Cancel</Button>
                  <Button busy={asked} onClick={() => asking && remove(asking, true)} data-testid="brain-remove-confirm">Remove</Button>
                </>
              }>
        <p className="m-0 font-mono text-[14px] [overflow-wrap:anywhere]" data-user-text data-testid="brain-remove-name">{asking}</p>
        <Collapse>{askError && <Banner tone="error" data-testid="brain-remove-failed"><span data-user-text>{askError}</span></Banner>}</Collapse>
      </Dialog>
      <FoldersDialog repo={foldersFor} sample={sample !== null} now={now} onClose={() => setFoldersFor(null)}
                     onSaved={() => { setFoldersFor(null); toast(sample ? SAMPLE_NOTE : "Saved. The brain reads the folders now."); reload(); }} />
      <HistoryDialog repo={historyFor} sample={sample !== null} now={now} onClose={() => setHistoryFor(null)} />
      <Toaster />
    </>
  );
}
