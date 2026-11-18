import * as React from "react";
import { Link } from "react-router-dom";

import { Banner, Button, Dialog, EmptyState, Panel, ProgressBar, Skeleton, StatusLabel, Toaster, useToast, type Tone } from "../../components/edition";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { brainRepos, removeBrainRepo, retryBrainRepo, type BrainReposData, type BrainRepoRow } from "./BrainRepos.api";
import { BRAIN_SAMPLES, isBrainSample, SAMPLE_NOW } from "./brainSamples";
import {
  actionError, addedParts, ago, cardKind, count, EMPTY_WORDS, NOT_SET_UP_WORDS, plainError, percent, pollMs, POLL_SLOW_MS, queuedWords,
  REMOVE_WORDS, REMOVING_WORDS, shortCommit, STATE_WORDS, type CardKind,
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

function Lines({ r, all, now }: { r: BrainRepoRow; all: BrainRepoRow[]; now: number }) {
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
          <p className={MUTED} data-testid="brain-line">
            {started && `Started ${started}. `}Sessions use grep on this repo until it is ready.
          </p>
        </>
      );
    }
    case "ready":
      return (
        <>
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[13px]">
            <dt className="text-muted-foreground">Up to date with</dt>
            <dd className="m-0" data-testid="brain-synced">
              <span className={MONO}>main</span>
              {r.indexed_commit && <> at <span className={MONO} data-testid="brain-commit">{shortCommit(r.indexed_commit)}</span></>}
              {r.last_synced && <>, {ago(r.last_synced, now)}</>}
            </dd>
            <dt className="text-muted-foreground">Files</dt>
            <dd className="m-0" data-testid="brain-file-count">{count(r.files_total)}</dd>
          </dl>
          {r.sync_failures > 0 && (
            <p className={MUTED} data-testid="brain-sync-note">
              {r.sync_failures === 1 ? "The last update from main did not go through." : `The last ${r.sync_failures} updates from main did not go through.`}
              {r.sync_error && <> <span data-user-text>{plainError(r.sync_error, "")}</span></>}
            </p>
          )}
        </>
      );
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

function Card({ r, all, now, admin, busy, onRetry, onRemove, onRetryRemoval }: {
  r: BrainRepoRow; all: BrainRepoRow[]; now: number; admin: boolean; busy: boolean;
  onRetry: (repo: string) => void; onRemove: (repo: string) => void; onRetryRemoval: (repo: string) => void;
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
      <Lines r={r} all={all} now={now} />
      {admin && (kind === "ready" || kind === "failed" || kind === "remove_failed") && (
        <div className="flex flex-wrap gap-2">
          {kind === "failed" && <Button variant="secondary" busy={busy} className={cn(SMALL_BUTTON, "border-foreground")} onClick={() => onRetry(r.repo)} data-testid="brain-retry">Retry</Button>}
          {kind === "remove_failed" && <Button variant="secondary" busy={busy} className={cn(SMALL_BUTTON, "border-foreground")} onClick={() => onRetryRemoval(r.repo)} data-testid="brain-retry-removal">Retry removal</Button>}
          {kind !== "remove_failed" && <Button variant="secondary" disabled={busy} className={SMALL_BUTTON} onClick={() => onRemove(r.repo)} data-testid="brain-remove">Remove</Button>}
        </div>
      )}
    </article>
  );
}

function ConnectButton() {
  return <Button asChild><Link to="/brain/connect" data-testid="brain-connect">Connect a repo</Link></Button>;
}

const NONE: ReadonlySet<string> = new Set();

export function BrainReposView({ data, admin, now, busy = NONE, onRetry = () => {}, onRemove = () => {}, onRetryRemoval = () => {} }: {
  data: BrainReposData; admin: boolean;
  /** The clock the "9 minutes ago" words count from (ms); a sample passes a fixed one. */
  now: number;
  /** The repos whose button was pressed and has not been answered yet, each until its own call ends. */
  busy?: ReadonlySet<string>;
  onRetry?: (repo: string) => void; onRemove?: (repo: string) => void; onRetryRemoval?: (repo: string) => void;
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
                onRetry={onRetry} onRemove={onRemove} onRetryRemoval={onRetryRemoval} />
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
          <p className="m-0 max-w-[640px] text-[15px] text-muted-foreground">
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
                          onRetry={retry} onRemove={(r) => { setAskError(""); setAsking(r); }} onRetryRemoval={(r) => remove(r, false)} />
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
        {askError && <Banner tone="error" data-testid="brain-remove-failed"><span data-user-text>{askError}</span></Banner>}
      </Dialog>
      <Toaster />
    </>
  );
}
