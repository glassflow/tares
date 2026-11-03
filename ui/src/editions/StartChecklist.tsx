import * as React from "react";
import { Link } from "react-router-dom";

import { Button, ProgressBar, Skeleton } from "../components/edition";
import { ROW_CLASS } from "../components/edition/ListRow";
import { cn } from "@/lib/utils";
import { hideStart, showStart, type StartAction, type StartBlock, type StartStep } from "./StartChecklist.api";

// The Start checklist: the few steps that take a new cell from empty to working. It draws the block
// the home gives it and knows no edition words; the home passes the title. Rows never reorder: a
// done step keeps its place and loses its buttons. Hiding is optimistic: the list goes at once and
// comes back with a short line if the cell refuses.

const COPIED_MS = 2000;
const MUTED = "text-xs text-muted-foreground";
const HIDE_FAILED = "Could not hide the list. Try again in a moment.";
const SHOW_FAILED = "Could not show the list. Try again in a moment.";

function Tick({ done }: { done: boolean }) {
  return (
    <span role="img" aria-label={done ? "Done" : "Not done"} data-testid="start-tick" data-done={done}
          className={cn("mt-0.5 grid size-5 flex-none place-items-center border-[1.5px] border-foreground",
                        done && "bg-foreground text-background")}>
      {done && (
        <svg aria-hidden viewBox="0 0 16 16" className="size-3.5" fill="none" stroke="currentColor" strokeWidth="2.2">
          <path d="M3 8.5l3.2 3.2L13 4.8" />
        </svg>
      )}
    </span>
  );
}

/** A command in a bordered box with its Copy button, which reads Copied for two seconds. */
function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => () => clearTimeout(timer.current), []);
  const copy = () => {
    navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), COPIED_MS);
    }, () => { /* no clipboard: the command stays visible to select */ });
  };
  return (
    <div className="flex h-8 max-w-full min-w-0 items-center border border-border bg-background pl-2.5">
      <code className="min-w-0 truncate bg-transparent! p-0! font-mono text-[12.5px]" title={command}>{command}</code>
      <Button variant="secondary" className="ml-2 h-[30px] border-0! border-l! text-[13px]!" onClick={copy}
              aria-label={copied ? "Copied" : `Copy ${command}`} data-testid="start-copy">
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

interface DoProps {
  /** Runs a `do` action; the page that owns the work passes it. Without one the button is disabled. */
  onDo?: (name: string) => void;
  /** The `do` action running now, if any. */
  doing?: string | null;
}

function ActionButton({ action, primary, onDo, doing }: DoProps & { action: Exclude<StartAction, { copy: string }>; primary: boolean }) {
  const cls = cn("h-8 text-[13px]!", primary && "border-foreground!");
  if ("do" in action) {
    const busy = doing === action.do;
    return (
      <Button variant="secondary" className={cls} disabled={!onDo || doing != null} onClick={() => onDo?.(action.do)}
              data-testid="start-do" data-do={action.do}>
        {busy ? "Starting" : action.label}
      </Button>
    );
  }
  if ("to" in action) {
    return <Button asChild variant="secondary" className={cls}><Link to={action.to}>{action.label}</Link></Button>;
  }
  return <Button asChild variant="secondary" className={cls}><a href={action.href}>{action.label}</a></Button>;
}

function Actions({ actions, onDo, doing }: DoProps & { actions: StartAction[] }) {
  let marked = false;   // the first button that is not a command gets the dark outline
  return (
    <div className="flex max-w-full min-w-0 flex-none flex-wrap gap-2">
      {actions.map((a) => {
        if ("copy" in a) return <CopyCommand key={a.label + a.copy} command={a.copy} />;
        const primary = !marked;
        marked = true;
        return <ActionButton key={a.label} action={a} primary={primary} onDo={onDo} doing={doing} />;
      })}
    </div>
  );
}

function Step({ step, onDo, doing }: DoProps & { step: StartStep }) {
  return (
    <li data-testid="start-step" data-key={step.key} data-done={step.done}
        className={cn(ROW_CLASS, "items-start gap-y-3 hover:bg-[var(--row-hover)]")}>
      <div className="flex min-w-0 flex-[1_1_240px] items-start gap-4">
        <Tick done={step.done} />
        <div className="min-w-0 flex-1">
          <div className={cn("font-bold", step.done && "font-semibold text-muted-foreground")}>{step.title}</div>
          <div className="text-muted-foreground">{step.gets}</div>
          {step.detail && (
            <div className="mt-0.5 line-clamp-3 text-[13px]" title={step.detail} data-user-text>{step.detail}</div>
          )}
        </div>
      </div>
      {step.actions.length > 0 && <Actions actions={step.actions} onDo={onDo} doing={doing} />}
    </li>
  );
}

const PANEL = "min-w-0 border border-border bg-card [overflow-wrap:anywhere]";
const HEAD = "flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-[18px] py-3.5";

export interface StartChecklistProps {
  block: StartBlock;
  /** The heading and the name of the section. */
  title: string;
  /** What hiding calls; the daemon's own call by default. A sample passes one that touches nothing. */
  hide?: () => Promise<unknown>;
  /** Called once the cell has taken the hide, so the page can read again. */
  onHidden?: () => void;
  /** Runs a `do` action of a step. */
  onDo?: (name: string) => void;
  /** The `do` action running now; its button reads Starting and the others wait. */
  doing?: string | null;
  /** A short line under the heading, such as why a `do` action failed. */
  notice?: React.ReactNode;
}

export function StartChecklist({ block, title, hide = hideStart, onHidden, onDo, doing, notice }: StartChecklistProps) {
  const [hidden, setHidden] = React.useState(false);
  const [error, setError] = React.useState(false);
  if (!block.show || hidden) return null;
  const pct = block.total > 0 ? (block.done / block.total) * 100 : 0;
  const onHide = () => {
    setHidden(true);
    setError(false);
    hide().then(
      () => onHidden?.(),
      () => { setHidden(false); setError(true); },
    );
  };
  return (
    <section aria-label={title} className={PANEL} data-testid="start-checklist">
      <div className={cn(HEAD, "border-b border-border")}>
        <div className="min-w-0">
          <h2 className="m-0! text-base!">{title}</h2>
          <div className={MUTED} data-testid="start-count">{block.done} of {block.total} done</div>
        </div>
        <div className="flex items-center gap-4">
          <ProgressBar value={pct} aria-label="Steps done" className="h-1 w-40 [&>div:first-child]:bg-foreground!" />
          <Button variant="quiet" className="px-1! underline" onClick={onHide}>Hide this list</Button>
        </div>
      </div>
      {error && (
        <div role="alert" data-testid="start-error" className="border-b border-border px-[18px] py-2.5 text-[13px] text-[color:var(--err)]">
          {HIDE_FAILED}
        </div>
      )}
      {notice && (
        <div role="alert" data-testid="start-notice" className="border-b border-border px-[18px] py-2.5 text-[13px] text-[color:var(--err)]">
          {notice}
        </div>
      )}
      <ul className="m-0! list-none! p-0!">
        {block.steps.map((s) => <Step key={s.key} step={s} onDo={onDo} doing={doing} />)}
      </ul>
    </section>
  );
}

export interface StartChecklistHiddenProps {
  block: StartBlock;
  /** The name of the list, as the page words it. */
  title: string;
  /** What Show it again calls; the daemon's own call by default. A sample passes one that touches nothing. */
  show?: () => Promise<unknown>;
  /** Called once the cell has shown the list again, so the page can read again. */
  onShown?: () => void;
}

/** Where the list was, once this person hid it: one quiet line with the way back. Nothing while the list shows
 *  or when every step is done. */
export function StartChecklistHidden({ block, title, show = showStart, onShown }: StartChecklistHiddenProps) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(false);
  if (block.show || !block.hidden) return null;
  const onShow = () => {
    setBusy(true);
    setError(false);
    show().then(
      () => { setBusy(false); onShown?.(); },
      () => { setBusy(false); setError(true); },
    );
  };
  return (
    <div data-testid="start-hidden" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
      <span>The list &ldquo;{title}&rdquo; is hidden.</span>
      <Button variant="quiet" className="px-1! underline" disabled={busy} onClick={onShow} data-testid="start-show">
        Show it again
      </Button>
      {error && <span role="alert" data-testid="start-show-error" className="text-[color:var(--err)]">{SHOW_FAILED}</span>}
    </div>
  );
}

/** The list while the home is loading: the same panel, every row saying Checking. */
export function StartChecklistLoading({ title, rows = 3 }: { title: string; rows?: number }) {
  return (
    <section aria-label={title} aria-busy="true" className={PANEL} data-testid="start-checklist-loading">
      <div className={cn(HEAD, "border-b border-border")}>
        <div className="min-w-0">
          <h2 className="m-0! text-base!">{title}</h2>
          <div className={MUTED}>Checking</div>
        </div>
      </div>
      <ul className="m-0! list-none! p-0!">
        {Array.from({ length: rows }, (_, i) => (
          <li key={i} className={cn(ROW_CLASS, "items-start")}>
            <span aria-hidden className="mt-0.5 size-5 flex-none border-[1.5px] border-foreground" />
            <div className="min-w-0 flex-1">
              <Skeleton width="38%" height={10} className="my-1.5" />
              <div className="text-muted-foreground">Checking</div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
