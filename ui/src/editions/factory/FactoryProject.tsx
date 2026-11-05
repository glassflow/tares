import * as React from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { api, ApiError } from "../../api";
import { Banner, Button, Dialog, ProgressBar, Skeleton, Textarea, Toaster, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  decideCall, factoryProject,
  type FactoryProjectData, type MovingTicket, type ProjectCall, type ProjectMilestone, type ProjectPageState, type TicketState,
} from "./FactoryProject.api";
import type { HomeAction, HomeItem } from "./home.types";
import { isSample, sampleClose, sampleClosed, sampleLoad } from "./projectSamples";

// One factory project, from one read (GET /api/factory/projects/{id}): what waits for you, the few
// tickets moving now, a compact row per milestone, the calls the crew made, your decisions, the
// documents and the command that opens a builder's session. The full ticket list is the Tickets
// screen; "All N tickets" and each milestone's count open it. A button that does something on Home
// (keep, allow, copy the hand-over command) links there with an arrow and never acts here. The cell
// shapes every string for people; names, goals, titles, calls, decisions and doc titles are the
// person's own text and marked data-user-text. The one raw string is the terminal command.

const POLL_MS = 15_000;
const SHOW_WAITING = 3;
const SHOW_DECISIONS = 3;
const NOT_CLOSED = "Try again in a moment.";
const CLOSE_WORDS = "Closed from the project page";
const NOT_SAVED = "That was not saved. Try again in a moment.";
const STEPS = ["Spec", "Build", "Review", "Done"];

const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-[color:var(--accent-deep)]";
const MUTED = "text-xs text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";
const CARD = "border border-border bg-card";
const SECTION_H = "m-0! text-base!";
const ROW = "border-b border-border px-4 py-3 last:border-b-0";

const CHIP: Record<ProjectPageState, string> = {
  waiting_on_you: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  building: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  not_started: "border-border bg-muted text-muted-foreground",
  ready_to_close: "border-[color:var(--ok-border)] bg-[var(--ok-soft)] text-[color:var(--ok)]",
  finished: "border-foreground bg-foreground text-background",
};

const DOT: Record<TicketState, string> = {
  "to do": "border border-[color:var(--ink-faint)]",
  "in progress": "bg-[var(--accent)]",
  "in review": "bg-[var(--accent-deep)]",
  "changes asked": "bg-[var(--warn)]",
  blocked: "bg-[var(--err)]",
  merged: "bg-[var(--ok)]",
  canceled: "bg-[var(--ink-faint)]",
};

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const pct = (n: number, of: number) => (of > 0 ? Math.round((n / of) * 100) : 0);

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

function SectionHead({ title, aside }: { title: string; aside?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h2 className={SECTION_H}>{title}</h2>
      {aside}
    </div>
  );
}

const Empty = ({ children, testid }: { children: React.ReactNode; testid: string }) => (
  <div className={cn(CARD, "px-4 py-5 text-muted-foreground")} data-testid={testid}>{children}</div>
);

// ── the header ────────────────────────────────────────────────────────────────

function Steps({ stage, finished }: { stage: FactoryProjectData["stage"]; finished: boolean }) {
  const current = finished ? STEPS.length : ["spec", "build", "review", "done"].indexOf(stage);
  return (
    <ol aria-label="Stages" data-testid="project-steps" className="m-0! flex max-w-[560px] list-none p-0!">
      {STEPS.map((name, i) => {
        const done = i < current;
        const now = i === current;
        return (
          <li key={name} aria-current={now ? "step" : undefined}
              className={cn("flex min-w-0 flex-[1_1_0] items-center gap-1.5 text-[13px] text-muted-foreground sm:gap-2 last:flex-none",
                            "after:mx-1 after:h-0.5 after:min-w-2 after:flex-1 after:bg-border after:content-[''] last:after:hidden sm:after:mx-2",
                            done && "text-foreground after:bg-foreground", now && "font-semibold text-foreground")}>
            <span className={cn("grid size-[22px] flex-none place-items-center border border-foreground bg-[var(--white)] font-mono text-[11px] text-foreground",
                                done && "bg-foreground text-background", now && "bg-[var(--accent)]")}>
              {done ? "✓" : i + 1}
            </span>
            {name}
          </li>
        );
      })}
    </ol>
  );
}

// ── waiting ───────────────────────────────────────────────────────────────────

/** The text of an item, with a backticked command shown as code (it is the crew's own text). */
function Detail({ text }: { text: string }) {
  const parts = text.split(/`([^`]+)`/);
  return (
    <div className="[overflow-wrap:anywhere]" data-user-text>
      {parts.map((p, i) => (i % 2 ? <code key={i} data-words-exempt className="bg-muted px-1 font-mono text-[12.5px]">{p}</code> : p))}
    </div>
  );
}

function ActionButton({ action, onClose }: { action: HomeAction; onClose?: () => void }) {
  const cls = "h-auto min-h-8 whitespace-normal py-1 text-left text-[13px]! max-sm:flex-1 max-sm:justify-center max-sm:text-center";
  const variant = action.do === "close" ? "primary" : "secondary";
  const strong = action.primary && action.do !== "close" ? "border-foreground!" : "";
  if (action.do === "close") {
    return <Button variant={variant} className={cn(cls, strong)} onClick={onClose} data-testid="waiting-close">{action.label}</Button>;
  }
  if (action.href) {
    const external = !action.href.startsWith("/");
    return (
      <Button asChild variant={variant} className={cn(cls, strong)}>
        {external ? <a href={action.href}>{action.label}</a> : <Link to={action.href}>{action.label}</Link>}
      </Button>
    );
  }
  // something the page does on Home: the arrow says so
  return (
    <Button asChild variant={variant} className={cn(cls, strong)}>
      <Link to="/" title="Opens Home, where this is done">{action.label} <span aria-hidden className="not-italic text-[color:var(--accent-deep)]">{"↗"}</span></Link>
    </Button>
  );
}

function WaitingRow({ item, onClose }: { item: HomeItem; onClose: () => void }) {
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <div className={cn(ROW, "flex flex-wrap items-center justify-between gap-x-4 gap-y-3 hover:bg-[var(--row-hover)]")}
         data-testid="waiting-row" data-kind={item.kind} id={`waiting-${item.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`}>
      <div className="min-w-0 flex-[1_1_260px]">
        <div className={KIND}>{item.label}</div>
        <div className="my-0.5 font-bold [overflow-wrap:anywhere]" data-user-text>{item.title}</div>
        <Detail text={item.detail} />
        {item.meta && <div className={cn(MUTED, "mt-0.5")}>{item.meta}</div>}
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2 max-sm:w-full">
          {actions.map((a) => <ActionButton key={a.label} action={a} onClose={onClose} />)}
        </div>
      )}
    </div>
  );
}

function Waiting({ items, onClose }: { items: HomeItem[]; onClose: () => void }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_WAITING);
  const more = items.length - shown.length;
  const arrows = items.some((i) => i.actions.some((a) => a.do && a.do !== "close"));
  return (
    <section id="waiting" aria-label="Waiting for you" className="min-w-0 scroll-mt-4" data-testid="waiting">
      <SectionHead title="Waiting for you"
                   aside={items.length > 0 && <span className={MUTED} data-testid="waiting-count">{plural(items.length, "thing", "things")}</span>} />
      {items.length === 0 ? (
        <Empty testid="waiting-empty">Nothing waits for you in this project.</Empty>
      ) : (
        <>
          {arrows && <div className={cn(MUTED, "-mt-1 mb-2")}>Buttons with an arrow open Home, where they are done.</div>}
          <div className={CARD}>
            {shown.map((it) => <WaitingRow key={it.id} item={it} onClose={onClose} />)}
            {more > 0 && (
              <div className="border-t border-border px-4 py-2.5">
                <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => setAll(true)} data-testid="waiting-more">
                  Show {more} more
                </Button>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

// ── tickets moving now ────────────────────────────────────────────────────────

function TicketRow({ t }: { t: MovingTicket }) {
  const sub = [t.who, t.review].filter(Boolean).join(" · ");
  return (
    <Link to={t.href} data-testid="moving-row" data-ref={t.ref}
          className="grid grid-cols-[46px_minmax(0,1fr)_14px] items-start gap-x-3.5 gap-y-1 border-b border-border px-3.5 py-3 text-foreground! no-underline! last:border-b-0 hover:bg-[var(--row-hover)] md:grid-cols-[48px_minmax(0,1fr)_116px_148px_14px] md:px-4">
      <span className="pt-0.5 font-mono text-xs font-medium [overflow-wrap:anywhere]">{t.ref}</span>
      <span className="min-w-0">
        <span className="line-clamp-2 block font-semibold [overflow-wrap:anywhere]" data-user-text>{t.title}</span>
        {(sub || t.blocked_by.length > 0) && (
          <span className={cn(MUTED, "mt-0.5 block [overflow-wrap:anywhere]")}>
            {sub}
            {t.blocked_by.length > 0 && (
              <>
                {sub && " · "}
                <span className="text-[color:var(--err)]">waits for {t.blocked_by.join(", ")}</span>
              </>
            )}
          </span>
        )}
      </span>
      <span className="col-start-2 flex items-center gap-[7px] whitespace-nowrap pt-px text-[13px] md:col-start-auto">
        <i aria-hidden className={cn("inline-block size-[9px] flex-none", DOT[t.state])} />
        {sentence(t.state)}
      </span>
      <span className="col-start-2 pt-0.5 text-xs text-muted-foreground [overflow-wrap:anywhere] md:col-start-auto md:row-start-auto">{t.pr?.state_text ?? ""}</span>
      <span aria-hidden className="col-start-3 row-start-1 text-right text-muted-foreground md:col-start-auto md:row-start-auto">{"›"}</span>
    </Link>
  );
}

function Moving({ d }: { d: FactoryProjectData }) {
  const { total, merged, canceled } = d.counts;
  const live = total - canceled;
  const rest = d.moving_total - d.moving.length;
  const allLink = total > 0 && (
    <Link to={d.tickets_href} data-testid="all-tickets-moving" className={cn("text-xs", LINK)}>All {total} tickets {"›"}</Link>
  );
  let body: React.ReactNode;
  if (total === 0) {
    body = <div className="px-4 py-5 text-muted-foreground" data-testid="moving-empty">No tickets yet. They show here once the crew has planned the build.</div>;
  } else if (d.moving.length === 0) {
    body = (
      <div className="px-4 py-5 text-muted-foreground" data-testid="moving-empty">
        {merged === live ? `Nothing is moving. All ${live} tickets are merged.` : "Nothing is moving right now."}
      </div>
    );
  } else {
    body = (
      <>
        {d.moving.map((t) => <TicketRow key={t.ref} t={t} />)}
        {rest > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-border px-4 py-2.5 text-[13px]" data-testid="moving-more">
            <span>{rest} more {rest === 1 ? "ticket is" : "tickets are"} moving.</span>
            <Link to={d.tickets_href} className={LINK} data-testid="all-tickets-more">All {total} tickets {"›"}</Link>
          </div>
        )}
      </>
    );
  }
  return (
    <section id="moving" aria-label="Moving now" className="min-w-0 scroll-mt-4" data-testid="moving">
      <SectionHead title="Moving now" aside={allLink} />
      {total > 0 && (
        <div className={cn(MUTED, "-mt-1 mb-2")} data-testid="moving-caption">
          {merged} of {live} tickets merged{canceled > 0 && `, ${canceled} canceled`}
        </div>
      )}
      <div className={CARD}>
        {body}
        {d.next && (
          <>
            <div className="border-y border-border bg-muted px-4 py-2 text-xs font-semibold text-muted-foreground">Next up</div>
            <Link to={`/projects/${encodeURIComponent(d.id)}/tickets/${encodeURIComponent(d.next.ticket)}`} data-testid="next-up"
                  className="grid grid-cols-[46px_minmax(0,1fr)_14px] items-start gap-x-3.5 gap-y-1 px-3.5 py-3 text-foreground! no-underline! hover:bg-[var(--row-hover)] md:grid-cols-[48px_minmax(0,1fr)_14px] md:px-4">
              <span className="pt-0.5 font-mono text-xs font-medium [overflow-wrap:anywhere]">{d.next.ticket}</span>
              <span className="min-w-0">
                <span className="block font-semibold [overflow-wrap:anywhere]" data-user-text>{d.next.title}</span>
                <span className="block text-xs text-muted-foreground">{sentence(d.next.why)}</span>
              </span>
              <span aria-hidden className="text-right text-muted-foreground">{"›"}</span>
            </Link>
          </>
        )}
      </div>
    </section>
  );
}

// ── milestones ────────────────────────────────────────────────────────────────

function MilestoneRow({ m }: { m: ProjectMilestone }) {
  const { total, merged } = m.counts;
  return (
    <div className={ROW} id={`milestone-${m.id}`} data-testid="milestone-row" data-ms={m.number}>
      <div className="grid gap-x-6 gap-y-1.5 md:grid-cols-[minmax(0,1fr)_250px]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 font-semibold [overflow-wrap:anywhere]">
            <span data-user-text>{m.name}</span>
            {m.done && (
              <span className="border border-[color:var(--ok-border)] bg-[var(--ok-soft)] px-1.5 py-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.06em] text-[color:var(--ok)]">Done</span>
            )}
          </div>
          {m.goal && <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{m.goal}</div>}
          {m.checks.length > 0 && (
            <details className="group mt-1" data-testid="milestone-checks">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 py-0.5 text-[13px] text-[color:var(--accent-deep)] [&::-webkit-details-marker]:hidden">
                <span aria-hidden className="text-[11px] transition-transform group-open:rotate-90">{"▸"}</span>
                {plural(m.checks.length, "check", "checks")} to pass
              </summary>
              <ol className="mb-0.5 mt-1.5 list-decimal pl-5">
                {m.checks.map((c, i) => (
                  <li key={i} className="my-1.5 text-[13px] [overflow-wrap:anywhere]">
                    <span data-user-text>{c.check}</span>
                    <span className="block text-xs text-muted-foreground">Should show: <span data-user-text>{c.expect}</span></span>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
        <div className="flex items-center gap-2.5 pt-0.5 text-[13px] md:self-start">
          <ProgressBar value={pct(merged, total)} aria-label={`${merged} of ${total} tickets merged`}
                       className="flex-1 [&>div:first-child]:bg-foreground" />
          <Link to={m.tickets_href} className={cn("whitespace-nowrap", LINK)} data-testid="milestone-count">{merged} of {total} merged</Link>
        </div>
      </div>
    </div>
  );
}

// ── calls, decisions, docs, terminal ──────────────────────────────────────────

function CallRow({ c, onDecide }: { c: ProjectCall; onDecide: (c: ProjectCall, state: "kept" | "overturned", words?: string) => Promise<void> }) {
  const [open, setOpen] = React.useState(false);
  const [words, setWords] = React.useState("");
  const [busy, setBusy] = React.useState<"" | "keep" | "send">("");
  const [error, setError] = React.useState("");
  const decide = async (state: "kept" | "overturned") => {
    setBusy(state === "kept" ? "keep" : "send");
    setError("");
    try {
      await onDecide(c, state, state === "overturned" ? words.trim() : undefined);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : NOT_SAVED);
      setBusy("");
    }
  };
  return (
    <div className={ROW} data-testid="call-row" data-ref={c.ref}>
      <div className="flex items-baseline gap-2.5">
        <span className="font-mono text-xs font-medium">{c.ref}</span>
        <span className="font-bold [overflow-wrap:anywhere]" data-user-text>{c.question}</span>
      </div>
      <div className="mt-1 [overflow-wrap:anywhere]" data-user-text>{c.choice}</div>
      {c.why && <div className={cn(MUTED, "mt-0.5 [overflow-wrap:anywhere]")} data-user-text>{c.why}</div>}
      <div className={cn(MUTED, "mt-0.5")}>
        {sentence(c.made)}.{c.tickets.length > 0 && ` Touches ${c.tickets.join(", ")}.`}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button variant="secondary" className="h-8 border-foreground! text-[13px]!" busy={busy === "keep"} disabled={busy === "send"}
                onClick={() => decide("kept")} data-testid="call-keep">Keep</Button>
        <Button variant="secondary" className="h-8 text-[13px]!" aria-expanded={open} disabled={busy !== ""}
                onClick={() => setOpen((o) => !o)} data-testid="call-overturn">Overturn</Button>
      </div>
      {open && (
        <div className="mt-2.5 grid gap-2" data-testid="call-overturn-box">
          <Textarea label="Say what you want instead. The crew reads your words." value={words}
                    placeholder="For example: use the stronger model for drafts too" data-testid="call-words"
                    onChange={(e) => setWords(e.target.value)} className="min-h-14" />
          <div className="flex flex-wrap gap-2">
            <Button className="h-8 text-[13px]!" busy={busy === "send"} disabled={busy === "keep" || words.trim() === ""}
                    onClick={() => decide("overturned")} data-testid="call-send">Send</Button>
            <Button variant="secondary" className="h-8 text-[13px]!" disabled={busy !== ""} onClick={() => setOpen(false)}>Cancel</Button>
          </div>
        </div>
      )}
      {error && <div role="alert" className="mt-2 text-[13px] text-[color:var(--err)]" data-testid="call-error">{error}</div>}
    </div>
  );
}

function Calls({ d, gone, onDecide }: {
  d: FactoryProjectData; gone: string[];
  onDecide: (c: ProjectCall, state: "kept" | "overturned", words?: string) => Promise<void>;
}) {
  const list = d.calls.filter((c) => !gone.includes(c.ref));
  return (
    <section id="calls" aria-label="Calls to keep" className="min-w-0 scroll-mt-4" data-testid="calls">
      <SectionHead title="Calls to keep" aside={<span className={MUTED}>Made by the crew without you</span>} />
      {list.length === 0 ? (
        <Empty testid="calls-empty">No open calls. When the crew decides something without you, it shows here.</Empty>
      ) : (
        <div className={CARD}>{list.map((c) => <CallRow key={c.ref} c={c} onDecide={onDecide} />)}</div>
      )}
    </section>
  );
}

function Decisions({ items }: { items: FactoryProjectData["decisions"] }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_DECISIONS);
  const more = items.length - shown.length;
  return (
    <section id="decisions" aria-label="Decisions" className="min-w-0 scroll-mt-4" data-testid="decisions">
      <SectionHead title="Decisions" aside={<span className={MUTED}>Your answers, newest first</span>} />
      {items.length === 0 ? (
        <Empty testid="decisions-empty">No decisions yet. When you answer the crew, it is kept here.</Empty>
      ) : (
        <div className={CARD}>
          {shown.map((x, i) => (
            <div key={i} className={ROW} data-testid="decision-row">
              <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-user-text>{x.question}</div>
              <div className="my-0.5 border-l-[3px] border-[color:var(--accent)] pl-2.5 font-medium [overflow-wrap:anywhere]" data-user-text data-testid="decision-answer">{x.answer}</div>
              {x.said && <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-testid="decision-said">You said: <span data-user-text>{x.said}</span></div>}
              <div className={MUTED}>{x.when}</div>
            </div>
          ))}
          {more > 0 && (
            <div className="border-t border-border px-4 py-2.5">
              <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => setAll(true)} data-testid="decisions-more">Show {more} older</Button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Docs({ items }: { items: FactoryProjectData["docs"] }) {
  return (
    <section id="docs" aria-label="Docs" className="min-w-0 scroll-mt-4" data-testid="docs">
      <SectionHead title="Docs" />
      {items.length === 0 ? (
        <Empty testid="docs-empty">No docs yet. The spec and plan show here once they are written.</Empty>
      ) : (
        <div className={CARD}>
          {items.map((x) => (
            <Link key={x.id} to={x.href} data-testid="doc-row"
                  className="flex flex-col gap-px border-b border-border px-4 py-2.5 text-foreground! no-underline! last:border-b-0 hover:bg-[var(--row-hover)]">
              <span className="font-semibold [overflow-wrap:anywhere]" data-user-text>{x.title}</span>
              <span className={MUTED}>{x.kind_label}, updated {x.updated}</span>
            </Link>
          ))}
        </div>
      )}
    </section>
  );
}

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
              className="flex min-w-0 flex-1 items-center border border-border bg-[var(--white)] px-2 py-1.5 font-mono text-[12.5px] [overflow-wrap:anywhere]">{command}</code>
        <Button variant="secondary" className="h-auto min-h-[30px] text-[13px]!" onClick={copy} data-testid="command-copy">{copied === "yes" ? "Copied" : copied === "failed" ? "Select it to copy" : "Copy"}</Button>
      </div>
    </div>
  );
}

function Terminal({ items }: { items: FactoryProjectData["terminal"] }) {
  const [first, ...rest] = items;
  return (
    <section id="terminal" aria-label="Work on it yourself" className="min-w-0 scroll-mt-4" data-testid="terminal">
      <SectionHead title="Work on it yourself" />
      {!first ? (
        <Empty testid="terminal-empty">No builder is working on this project now. The command that opens its session shows here when one is.</Empty>
      ) : (
        <div className={cn(CARD, "p-4")}>
          <Command label={first.label} command={first.command} />
          {rest.length > 0 && (
            <details className="mt-2.5 border-t border-border pt-2" data-testid="terminal-more">
              <summary className="cursor-pointer text-[13px] text-[color:var(--accent-deep)]">{plural(rest.length, "more command", "more commands")}</summary>
              <div className="mt-2.5 grid gap-3">
                {rest.map((c) => <Command key={c.command} label={c.label} command={c.command} />)}
              </div>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

// ── the page ──────────────────────────────────────────────────────────────────

function Crumb({ name }: { name?: string }) {
  return (
    <div className={MUTED} data-testid="crumb">
      <Link to="/projects" className={LINK}>Projects</Link>
      {name !== undefined && <> / <span data-user-text>{name}</span></>}
    </div>
  );
}

function Loading() {
  const block = (n: number) => (
    <div className="grid gap-2.5">
      <Skeleton width={180} height={16} />
      <div className={cn(CARD, "grid gap-2.5 p-4")}>
        {Array.from({ length: n }, (_, i) => <Skeleton key={i} width={`${[90, 70, 80, 60][i % 4]}%`} />)}
      </div>
    </div>
  );
  return (
    <div role="status" aria-busy="true" aria-label="Loading" data-testid="project-loading" className="grid gap-5">
      <Skeleton width={120} />
      <Skeleton width={280} height={30} />
      <Skeleton width="80%" />
      <Skeleton width={260} height={22} />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_380px] xl:gap-8">
        <div className="grid min-w-0 content-start gap-6">{block(3)}{block(5)}{block(4)}</div>
        <div className="grid min-w-0 content-start gap-6">{block(4)}{block(4)}</div>
      </div>
    </div>
  );
}

/** The page for a project that was read. `onClose` closes the project and answers when it is done or
 *  throws the cell's answer; `onDecide` keeps or overturns a call the same way. Without them the
 *  buttons only open and shut what they open. `start` is a Close part way, for a sample. */
export function FactoryProjectView({ data: d, onClose, onDecide, start }: {
  data: FactoryProjectData;
  onClose?: () => Promise<void>;
  onDecide?: (c: ProjectCall, state: "kept" | "overturned", words?: string) => Promise<void>;
  start?: { open: boolean; error: string };
}) {
  const { toast } = useToast();
  const { hash } = useLocation();
  const [asking, setAsking] = React.useState(Boolean(start?.open));
  const [busy, setBusy] = React.useState(false);
  const [closeError, setCloseError] = React.useState(start?.error ?? "");
  const [gone, setGone] = React.useState<string[]>([]);

  const finished = d.state === "finished";
  // a button that opens this very page has no place on it
  const here = `/projects/${d.id}`;
  const own = d.waiting.map((w) => ({ ...w, actions: w.actions.filter((a) => a.href !== here) }));
  const items = d.can_close && !own.some((w) => w.actions.some((a) => a.do === "close")) ? [...own, readyRow(d)] : own;

  // an address with a #section scrolls to it once the page is there; a #desk-* one waits in Waiting
  React.useEffect(() => {
    if (!hash) return;
    let id = hash.slice(1);
    try { id = decodeURIComponent(id); } catch { /* a malformed address keeps its raw text */ }
    const el = document.getElementById(id) ?? (id.startsWith("desk-") ? document.getElementById("waiting") : null);
    el?.scrollIntoView({ block: "start" });
  }, [hash]);

  const confirm = async () => {
    setBusy(true);
    try {
      await onClose?.();
      setCloseError("");
      toast(`${d.name} is closed. It moved to Done.`);
    } catch (err) {
      setCloseError(err instanceof ApiError ? err.message : NOT_CLOSED);
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };

  const decide = async (c: ProjectCall, state: "kept" | "overturned", words?: string) => {
    await onDecide?.(c, state, words);
    setGone((g) => [...g, c.ref]);
  };

  return (
    <div className="grid min-w-0 gap-6 md:gap-8" data-testid="factory-project" data-state={d.state}>
      <div className="grid min-w-0 gap-3">
        <Crumb name={d.name} />
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
          <div className="flex min-w-0 flex-[1_1_320px] flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="m-0! text-[22px]! md:text-[28px]! [overflow-wrap:anywhere]" data-user-text data-testid="project-name">{d.name}</h1>
            <span data-testid="project-chip"
                  className={cn("border px-2 py-1 font-mono text-[11px] font-medium uppercase tracking-[0.06em]", CHIP[d.state])}>
              {d.state_label}
            </span>
          </div>
          {d.can_close && !finished && (
            <Button onClick={() => setAsking(true)} data-testid="project-close">Close</Button>
          )}
        </div>
        {d.goal && <p className="m-0! max-w-[820px] text-[15px] [overflow-wrap:anywhere]" data-user-text data-testid="project-goal">{d.goal}</p>}
        <Steps stage={d.stage} finished={finished} />
        <div className="flex flex-wrap gap-x-[18px] gap-y-1 text-[13px]" data-testid="project-links">
          <Link to={`/setup?project=${encodeURIComponent(d.id)}`} className={LINK}>Edit plan</Link>
          {d.counts.total > 0 && <Link to={d.tickets_href} className={LINK} data-testid="all-tickets-head">All {d.counts.total} tickets</Link>}
          {d.linked.tickets_in && d.linked.url && (
            <a href={d.linked.url} target="_blank" rel="noopener noreferrer" className={LINK} data-testid="tracker-link">
              Tickets in {d.linked.tickets_in} {"↗"}
            </a>
          )}
        </div>
        {closeError && (
          <Banner tone="error" data-testid="close-error">
            <b>The project was not closed.</b> <span className="text-foreground" data-user-text>{closeError}</span>
          </Banner>
        )}
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_380px] xl:gap-8">
        <div className="grid min-w-0 gap-6">
          <Waiting items={finished ? [] : items} onClose={() => setAsking(true)} />
          <Moving d={d} />
          <section id="milestones" aria-label="Milestones" className="min-w-0 scroll-mt-4" data-testid="milestones">
            <SectionHead title="Milestones" />
            {d.milestones.length === 0 ? (
              <Empty testid="milestones-empty">No milestones yet. The crew adds them when it plans the build.</Empty>
            ) : (
              <div className={CARD}>{d.milestones.map((m) => <MilestoneRow key={m.id} m={m} />)}</div>
            )}
          </section>
        </div>
        <div className="grid min-w-0 gap-6">
          <Calls d={d} gone={gone} onDecide={decide} />
          <Decisions items={d.decisions} />
          <Docs items={d.docs} />
          <Terminal items={d.terminal} />
        </div>
      </div>

      <Dialog open={asking} onOpenChange={(o) => { if (!o && !busy) setAsking(false); }} data-testid="close-dialog"
              title={`Close ${d.name}?`}
              description="It moves to Done. Handing it to the crew again opens it."
              footer={
                <>
                  <Button variant="secondary" disabled={busy} onClick={() => setAsking(false)} data-testid="close-cancel">Cancel</Button>
                  <Button busy={busy} onClick={confirm} data-testid="close-confirm">Close</Button>
                </>
              } />
      <Toaster />
    </div>
  );
}

export default function FactoryProject({ onOther }: { onOther?: () => void } = {}) {
  const { id = "" } = useParams();
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [closedHere, setClosedHere] = React.useState(false);
  const { data, error, reload } = usePagePolling<FactoryProjectData>(
    () => (sample ? sampleLoad(sample) : factoryProject(id).catch((e) => {
      // the cell answers 404 for a project this page is not for: today's page takes it
      if (e instanceof ApiError && e.status === 404 && onOther) onOther();
      throw e;
    })), POLL_MS);

  // a sample has no cell to close on: `fail` answers as the cell does, any other moves the page on this copy
  const close = async () => {
    if (sample) {
      if (sampleClose(sample).error) throw new ApiError(sampleClose(sample).error, 409);
      setClosedHere(true);
      return;
    }
    await api.finishProject(id, CLOSE_WORDS);
    reload();
  };
  const decide = async (c: ProjectCall, state: "kept" | "overturned", words?: string) => {
    if (!sample) await decideCall(id, c.ref, state, words);
    if (!sample) reload();
  };

  if (!data) {
    return (
      <div className="min-w-0" data-testid="factory-project-wait">
        {error ? (
          <div className="grid gap-3">
            <Crumb />
            <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
              <b>We could not load this project.</b> <span className="text-foreground">Check your connection, then try again.</span>
            </Banner>
          </div>
        ) : <Loading />}
      </div>
    );
  }
  const shown = closedHere ? sampleClosed(data) : data;
  return (
    <>
      {error && (
        <div className="mb-6">
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
            <b>This page did not refresh.</b> <span className="text-foreground">Check your connection, then try again.</span>
          </Banner>
        </div>
      )}
      <FactoryProjectView data={shown} onClose={close} onDecide={decide} start={sample ? sampleClose(sample) : undefined} />
    </>
  );
}
