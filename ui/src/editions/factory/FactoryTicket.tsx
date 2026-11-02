import * as React from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  factoryTicket,
  type FactoryTicketData, type TicketCall, type TicketFinding, type TicketLink, type TicketReview, type TicketStateKey,
} from "./FactoryTicket.api";
import { isSample, sampleLoad } from "./ticketSamples";

// One ticket, from one read (GET /api/factory/projects/{id}/tickets/{ref}): its state, where it
// lives, who works on it, what it depends on and what waits for it, its pull request, every
// review round with its findings, the checks the builder ran, the calls and decisions, its history
// and its working notes. The cell words every string for people. The ticket's title, a finding's
// text, questions, choices and decisions are the person's own text (data-user-text). A review's own
// words, a check's command and result, a history reason, the working notes and the terminal command
// are shown as written (data-words-exempt).

const POLL_MS = 15_000;
const SHOW_ROUNDS = 3;
const SHOW_CHECKS = 8;
const SHOW_HISTORY = 10;
const NOTES_CAP_LINES = 60;

const NONE = "text-[13px] text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";
const KIND = "m-0! font-mono text-[11px]! font-medium uppercase tracking-[0.06em] text-muted-foreground";
const TAG = "whitespace-nowrap border border-border bg-muted px-1.5 py-px text-[11.5px] font-semibold";
const TAG_OK = "border-[color:var(--ok-border)] bg-[var(--ok-soft)] text-[color:var(--ok)]";
const TAG_WAIT = "border-[color:var(--warn)] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]";
const TAG_BAD = "border-[color:var(--danger-border)] bg-[var(--err-soft)] text-[color:var(--err)]";
const MORE = "h-8 self-start text-[13px]!";

const DOT: Record<TicketStateKey, string> = {
  to_do: "border border-[color:var(--ink-faint)]",
  in_progress: "bg-[var(--accent)]",
  in_review: "bg-[var(--accent-deep)]",
  changes_asked: "bg-[var(--warn)]",
  blocked: "bg-[var(--err)]",
  merged: "bg-[var(--ok)]",
  canceled: "bg-[var(--ink-faint)]",
};

const KEY_OF_LABEL: Record<string, TicketStateKey> = {
  "To do": "to_do", "In progress": "in_progress", "In review": "in_review", "Changes asked": "changes_asked",
  Blocked: "blocked", Merged: "merged", Canceled: "canceled",
};

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const Dot = ({ state }: { state: TicketStateKey }) => (
  <i aria-hidden className={cn("inline-block size-[9px] flex-none", DOT[state])} />
);

function Section({ id, title, aside, children }: { id: string; title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section id={id} aria-label={title} className="min-w-0 scroll-mt-4 border border-border bg-card" data-testid={`ticket-${id}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b border-border px-3.5 py-3">
        <h2 className="m-0! text-base!">{title}</h2>
        {aside && <span className="text-xs text-muted-foreground">{aside}</span>}
      </div>
      <div className="flex min-w-0 flex-col gap-2.5 px-3.5 py-3">{children}</div>
    </section>
  );
}

// ── reviews ───────────────────────────────────────────────────────────────────

function Finding({ f }: { f: TicketFinding }) {
  const open = f.state_text === "open";
  return (
    <div className={cn("flex min-w-0 flex-col gap-1 border border-l-[3px] border-border bg-card px-2.5 py-2", open ? "border-l-[color:var(--warn)]!" : "border-l-[color:var(--ok-border)]!")}
         data-testid="finding" data-state={open ? "open" : "fixed"}>
      <div className="text-[13.5px] [overflow-wrap:anywhere]" data-user-text>{f.text}</div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {f.file && (
          <code className="bg-muted px-1 font-mono text-[11.5px] [overflow-wrap:anywhere]" data-words-exempt>
            {f.line ? `${f.file}:${f.line}` : f.file}
          </code>
        )}
        <span className={cn(TAG, f.blocking && TAG_BAD)}>{f.blocking ? "Must fix" : "Suggestion"}</span>
        <span className={cn(TAG, open ? TAG_WAIT : TAG_OK)}>{sentence(f.state_text)}</span>
      </div>
    </div>
  );
}

const VERDICT: Record<string, string> = { passed: TAG_OK, "asked for changes": TAG_WAIT, blocked: TAG_BAD };

function Round({ r, startOpen }: { r: TicketReview; startOpen: boolean }) {
  const [open, setOpen] = React.useState(startOpen);
  return (
    <div className="border border-border bg-muted" data-testid="round" data-round={r.round} data-open={open}>
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} data-testid="round-toggle"
              className="flex! w-full! flex-wrap items-center gap-2.5! border-0! bg-transparent! px-3! py-2! text-left! text-sm! font-normal! text-foreground! hover:bg-transparent!">
        <i aria-hidden className={cn("size-[7px] flex-none border-b-2 border-r-2 border-foreground", open ? "rotate-45" : "-rotate-45")} />
        <b className="text-sm">Round {r.round}</b>
        <span className={cn(TAG, "font-mono text-[10.5px] uppercase tracking-[0.06em]", VERDICT[r.verdict_text])}>{r.verdict_text}</span>
        <span className="ml-auto text-[12.5px] text-muted-foreground">{[r.by, r.when].filter(Boolean).join(", ")}</span>
      </button>
      {open && (
        <div className="flex min-w-0 flex-col gap-2 border-t border-border px-3 pb-3 pt-2.5">
          <div className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-words-exempt>
            <b className="font-medium text-foreground">Checked:</b> {r.verified}
            {r.not_verified && <>. <b className="font-medium text-foreground">Not checked:</b> {r.not_verified}</>}
          </div>
          {r.findings.length > 0
            ? r.findings.map((f) => <Finding key={`${f.label}-${f.state_text}`} f={f} />)
            : <span className={NONE}>No findings</span>}
        </div>
      )}
    </div>
  );
}

function Reviews({ items }: { items: TicketReview[] }) {
  const [all, setAll] = React.useState(false);
  const sorted = [...items].sort((a, b) => b.round - a.round);
  const shown = all ? sorted : sorted.slice(0, SHOW_ROUNDS);
  const hidden = sorted.length - shown.length;
  return (
    <Section id="reviews" title="Reviews" aside={items.length > 0 ? plural(items.length, "round", "rounds") : undefined}>
      {items.length === 0 ? <span className={NONE} data-testid="reviews-empty">No review yet</span> : (
        <>
          {shown.map((r, i) => <Round key={r.round} r={r} startOpen={i === 0 || sorted.length <= SHOW_ROUNDS} />)}
          {hidden > 0 && (
            <Button variant="secondary" className={MORE} onClick={() => setAll(true)} data-testid="reviews-more">
              Show {plural(hidden, "earlier round", "earlier rounds")}
            </Button>
          )}
        </>
      )}
    </Section>
  );
}

// ── checks, calls and decisions, history ──────────────────────────────────────

/** A result in the colour of what it says: failures red, passes green, anything else plain. */
function resultTone(result: string): string {
  if (/\b[1-9]\d* (failed|errors?)\b|\bFAILED\b|Traceback/.test(result)) return "text-[color:var(--err)]";
  if (/\bpassed\b|\bok\b/i.test(result)) return "text-[color:var(--ok)]";
  return "text-foreground";
}

function Checks({ items }: { items: FactoryTicketData["checks"] }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_CHECKS);
  const hidden = items.length - shown.length;
  return (
    <Section id="checks" title="Checks" aside={items.length > 0 ? `${items.length} ran` : undefined}>
      {items.length === 0 ? <span className={NONE} data-testid="checks-empty">No checks yet</span> : (
        <>
          <div className="flex flex-col">
            {shown.map((c, i) => (
              <div key={i} className="grid grid-cols-1 items-start gap-x-3.5 gap-y-0.5 border-b border-border py-2 first:pt-0 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto]" data-testid="check-row">
                <span className="font-mono text-[12.5px] [overflow-wrap:anywhere]" data-words-exempt>{c.command}</span>
                <span className="whitespace-nowrap text-[12.5px] text-muted-foreground">{c.when}</span>
                <span className={cn("text-[13px] [overflow-wrap:anywhere] sm:col-span-2", resultTone(c.result))} data-words-exempt>{c.result}</span>
              </div>
            ))}
          </div>
          {hidden > 0 && <Button variant="secondary" className={MORE} onClick={() => setAll(true)} data-testid="checks-more">Show {hidden} more</Button>}
        </>
      )}
    </Section>
  );
}

function CallRow({ c }: { c: TicketCall }) {
  const tone = c.state_text === "waiting for you" ? TAG_WAIT : c.state_text === "confirmed" ? TAG_OK : "";
  return (
    <div className="flex min-w-0 flex-col gap-[3px]" data-testid="call-row" data-ref={c.ref}>
      <span className="text-[13.5px] font-semibold [overflow-wrap:anywhere]" data-user-text>{c.question}</span>
      <span className="text-[13.5px] [overflow-wrap:anywhere]" data-user-text>{c.choice}</span>
      {c.why && <span className="text-[12.5px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{c.why}</span>}
      {c.words && <span className="border-l-[3px] border-[color:var(--accent)] pl-2.5 text-[13.5px] [overflow-wrap:anywhere]" data-user-text>{c.words}</span>}
      <span className="text-[12.5px] text-muted-foreground">
        {sentence(c.made)}. <span className={cn(TAG, tone)}>{sentence(c.state_text)}</span>
      </span>
    </div>
  );
}

function Calls({ d }: { d: FactoryTicketData }) {
  const none = d.calls.length === 0 && d.decisions.length === 0;
  return (
    <Section id="calls" title="Calls and decisions">
      {none ? <span className={NONE} data-testid="calls-empty">No calls or decisions on this ticket</span> : (
        <>
          {d.calls.length > 0 && (
            <>
              <h3 className={KIND}>Calls the builder made</h3>
              {d.calls.map((c) => <CallRow key={c.ref} c={c} />)}
            </>
          )}
          {d.decisions.length > 0 && (
            <>
              <h3 className={KIND}>Your decisions</h3>
              {d.decisions.map((x, i) => (
                <div key={i} className="flex min-w-0 flex-col gap-[3px]" data-testid="decision-row">
                  <span className="text-[13.5px] font-semibold [overflow-wrap:anywhere]" data-user-text>{x.question}</span>
                  <span className="text-[13.5px] [overflow-wrap:anywhere]" data-user-text>{x.words}</span>
                  <span className="text-[12.5px] text-muted-foreground">{sentence(x.when)}</span>
                </div>
              ))}
            </>
          )}
        </>
      )}
    </Section>
  );
}

function History({ items }: { items: FactoryTicketData["history"] }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_HISTORY);
  const hidden = items.length - shown.length;
  return (
    <Section id="history" title="History" aside={items.length > 0 ? plural(items.length, "line", "lines") : undefined}>
      {items.length === 0 ? <span className={NONE} data-testid="history-empty">Nothing has happened yet</span> : (
        <>
          <div className="flex flex-col">
            {shown.map((h, i) => (
              <div key={i} className="grid grid-cols-1 gap-x-2.5 border-b border-dashed border-border py-1.5 text-[13.5px] first:pt-0 last:border-b-0 sm:grid-cols-[130px_minmax(0,1fr)_auto]" data-testid="history-row">
                <span className="whitespace-nowrap text-[12.5px] text-muted-foreground">{sentence(h.when)}</span>
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  {h.what}
                  {h.text && <span className="block text-[12.5px] text-muted-foreground" data-words-exempt>{h.text}</span>}
                </span>
                <span className="text-[12.5px] text-muted-foreground">{h.who ? `by ${h.who}` : ""}</span>
              </div>
            ))}
          </div>
          {hidden > 0 && <Button variant="secondary" className={MORE} onClick={() => setAll(true)} data-testid="history-more">Show {hidden} more</Button>}
        </>
      )}
    </Section>
  );
}

// ── working notes ─────────────────────────────────────────────────────────────

// Markdown, never raw HTML: no pictures (they would call out), links open in a new tab, code scrolls.
const MARKDOWN: Components = {
  img: ({ alt }) => <>{alt}</>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener" className="underline!">{children}</a>,
  pre: ({ children }) => <pre className="max-w-full overflow-x-auto whitespace-pre-wrap text-[12px]">{children}</pre>,
  table: ({ children }) => <div className="my-1 max-w-full overflow-x-auto"><table className="border-collapse text-[13px]">{children}</table></div>,
};

function Notes({ text }: { text: string }) {
  const [whole, setWhole] = React.useState(false);
  const lines = text ? text.split("\n").length : 0;
  const long = lines >= NOTES_CAP_LINES;
  const open = whole || !long;
  return (
    <Section id="notes" title="Working notes" aside={lines > 0 ? `${lines.toLocaleString("en-US")} ${lines === 1 ? "line" : "lines"}` : undefined}>
      {!text ? <span className={NONE} data-testid="notes-empty">Nothing noted yet</span> : (
        <>
          <div className="min-w-0 border border-border bg-[var(--white)] text-sm [overflow-wrap:anywhere]" data-testid="notes-box" data-open={open}>
            <div className={cn("md px-3.5 py-3 [&_:is(h1,h2,h3,h4)]:mt-3.5! [&_:is(h1,h2,h3,h4)]:mb-1! [&_:is(h1,h2,h3,h4)]:text-[14px]! [&_:is(h1,h2,h3,h4):first-child]:mt-0!", !open && "max-h-[460px] overflow-auto")} data-words-exempt data-testid="notes-text">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>{text}</ReactMarkdown>
            </div>
            {!open && <div aria-hidden className="pointer-events-none relative -mt-9 h-9 bg-gradient-to-b from-transparent to-[var(--white)]" />}
          </div>
          {long && (
            <Button variant="secondary" className={MORE} onClick={() => setWhole((w) => !w)} aria-expanded={open} data-testid="notes-toggle">
              {open ? "Show less" : "Show the whole document"}
            </Button>
          )}
        </>
      )}
    </Section>
  );
}

// ── the facts ─────────────────────────────────────────────────────────────────

function Fact({ title, children, testid }: { title: string; children: React.ReactNode; testid: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid={testid}>
      <h3 className={KIND}>{title}</h3>
      {children}
    </div>
  );
}

function Links({ items, testid }: { items: TicketLink[]; testid: string }) {
  if (items.length === 0) return <span className={NONE}>Nothing</span>;
  return (
    <ul className="m-0! flex list-none flex-col gap-1.5 p-0!">
      {items.map((t) => {
        const key = KEY_OF_LABEL[t.state_label] ?? "to_do";
        return (
          <li key={t.ref} className="flex items-start gap-2 text-[13.5px]" data-testid={testid} data-ref={t.ref}>
            <span className="mt-[5px] flex"><Dot state={key} /></span>
            <span className="min-w-0">
              <Link to={t.href} className="text-foreground! no-underline! [overflow-wrap:anywhere] hover:underline!">
                <span className="font-mono text-xs text-muted-foreground">{t.ref}</span> <span data-user-text>{t.title}</span>
              </Link>
              <span className="block text-xs text-muted-foreground">{t.state_label}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Command({ command }: { command: string }) {
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
    <div className="flex items-center gap-2 border border-border bg-muted px-2 py-1.5">
      <code data-words-exempt data-testid="command-text" className="min-w-0 flex-1 bg-transparent! font-mono text-[12.5px] [overflow-wrap:anywhere]">{command}</code>
      <Button variant="secondary" className="h-8 text-[13px]!" onClick={copy} data-testid="command-copy">
        {copied === "yes" ? "Copied" : copied === "failed" ? "Select it to copy" : "Copy"}
      </Button>
    </div>
  );
}

function Facts({ d }: { d: FactoryTicketData }) {
  const closed = d.state === "merged" || d.state === "canceled";
  return (
    <aside aria-label="Facts" className="min-w-0 border border-border bg-card max-xl:order-first" data-testid="ticket-facts">
      <div className="flex min-w-0 flex-col gap-3.5 px-3.5 py-3">
        <Fact title="Who works on it" testid="fact-who">
          {d.who ? <div className="text-sm [overflow-wrap:anywhere]">{d.who}</div>
            : <span className={NONE}>{closed ? "Nobody, it is finished" : "Not picked up yet"}</span>}
        </Fact>
        <Fact title="Depends on" testid="fact-depends"><Links items={d.depends_on} testid="depends-row" /></Fact>
        <Fact title="Blocks" testid="fact-blocks"><Links items={d.blocks} testid="blocks-row" /></Fact>
        <Fact title="Pull request" testid="fact-pr">
          {d.pr ? (
            <div className="text-sm">
              {d.pr.url
                ? <a href={d.pr.url} target="_blank" rel="noopener noreferrer" className={LINK}>{d.pr.number ? `PR #${d.pr.number}` : "Pull request"}</a>
                : <span>{d.pr.number ? `PR #${d.pr.number}` : "Pull request"}</span>}
              , {d.pr.state_text}
            </div>
          ) : <span className={NONE}>No pull request yet</span>}
        </Fact>
        {d.terminal.length > 0 && (
          <Fact title="Open in a terminal" testid="fact-terminal">
            {d.terminal.map((t) => (
              <div key={t.command} className="flex flex-col gap-1">
                {d.terminal.length > 1 && <span className="text-xs text-muted-foreground">{t.label}</span>}
                <Command command={t.command} />
              </div>
            ))}
          </Fact>
        )}
      </div>
    </aside>
  );
}

// ── the page ──────────────────────────────────────────────────────────────────

function Back({ href, name }: { href: string; name?: string }) {
  return (
    <Link to={href} data-testid="ticket-back" className="w-fit text-[13px] text-[color:var(--accent-deep)]! no-underline! hover:underline!">
      {"←"} {name !== undefined ? <span data-user-text>{name}</span> : "Back to the project"}
    </Link>
  );
}

function Loading({ href }: { href: string }) {
  const bars = (n: number) => Array.from({ length: n }, (_, i) => <Skeleton key={i} width={`${[90, 70, 80][i % 3]}%`} className="mb-2" />);
  return (
    <div role="status" aria-busy="true" aria-label="Loading" data-testid="ticket-loading" className="grid min-w-0 gap-4">
      <Back href={href} />
      <div className="grid gap-2.5">
        <div className="flex gap-2.5"><Skeleton width={60} height={22} /><Skeleton width={110} height={22} /></div>
        <Skeleton width="55%" height={30} />
        <Skeleton width="35%" />
      </div>
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="grid min-w-0 gap-[18px]">
          {["Reviews", "Checks", "Calls and decisions", "History", "Working notes"].map((n) => (
            <div key={n} className="border border-border bg-card">
              <div className="border-b border-border px-3.5 py-3"><h2 className="m-0! text-base!">{n}</h2></div>
              <div className="p-3.5">{bars(3)}</div>
            </div>
          ))}
        </div>
        <div className="border border-border bg-card p-3.5 max-xl:order-first">{bars(5)}</div>
      </div>
    </div>
  );
}

/** The page for a ticket, from what the cell answered. `data` is the ticket; with none, `notFound`
 *  says the cell has no such ticket, `error` says it did not answer, and neither is still loading.
 *  `projectHref` is where the back link goes while the ticket's project is not known. */
export function FactoryTicketView({ data: d, error, notFound, projectHref, onRetry }: {
  data?: FactoryTicketData;
  error?: string;
  notFound?: boolean;
  projectHref: string;
  onRetry?: () => void;
}) {
  if (!d) {
    if (notFound) {
      return (
        <div className="grid min-w-0 gap-4" data-testid="ticket-notfound">
          <Back href={projectHref} />
          <div className="grid gap-2 border border-border bg-card px-3.5 py-3">
            <h2 className="m-0! text-base!">This ticket was not found</h2>
            <span className={NONE}>It may have been deleted, or the link may be old.</span>
          </div>
        </div>
      );
    }
    if (error) {
      return (
        <div className="grid min-w-0 gap-4" data-testid="ticket-error">
          <Back href={projectHref} />
          <Banner tone="error" action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}>
            <b>The ticket page did not load.</b> Tares did not answer. Try again in a moment.
          </Banner>
        </div>
      );
    }
    return <Loading href={projectHref} />;
  }

  const ms = d.milestone;
  const msText = ms ? (ms.name.startsWith(ms.number) ? ms.name : `${ms.number} ${ms.name}`) : "";
  return (
    <div className="grid min-w-0 gap-4" data-testid="factory-ticket" data-ref={d.ref} data-state={d.state}>
      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}>
          <b>This page did not refresh.</b> Check your connection, then try again.
        </Banner>
      )}
      <Back href={d.project.href} name={d.project.name} />
      <div className="grid min-w-0 gap-2.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="border border-border bg-muted px-2 py-0.5 font-mono text-[13px] font-medium" data-testid="ticket-ref">{d.ref}</span>
          <span className="inline-flex items-center gap-[7px] border border-border bg-card px-2.5 py-[3px] text-[12.5px] font-semibold" data-testid="ticket-state">
            <Dot state={d.state} />
            {d.state_label}
          </span>
        </div>
        <h1 className="m-0! text-[22px]! md:text-[28px]! [overflow-wrap:anywhere]" data-user-text data-testid="ticket-title">{d.title}</h1>
        <div className="flex flex-wrap gap-x-[18px] gap-y-1.5 text-[13px] text-muted-foreground">
          {ms && <span data-testid="ticket-milestone">Milestone <b className="font-medium text-foreground" data-user-text>{msText}</b></span>}
          {d.blocked_reason && <span data-testid="ticket-blocked-reason" data-user-text>{d.blocked_reason}</span>}
          {d.url && (
            <span data-testid="ticket-lives-in">
              Lives in <a href={d.url} target="_blank" rel="noopener noreferrer" className={LINK}>{d.in}</a>
            </span>
          )}
        </div>
      </div>
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="flex min-w-0 flex-col gap-[18px]">
          <Reviews items={d.reviews} />
          <Checks items={d.checks} />
          <Calls d={d} />
          <History items={d.history} />
          <Notes text={d.notes} />
        </div>
        <Facts d={d} />
      </div>
    </div>
  );
}

function TicketPage({ id, reference }: { id: string; reference: string }) {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [missing, setMissing] = React.useState(false);
  const { data, error, reload } = usePagePolling<FactoryTicketData>(
    () => (sample ? sampleLoad(sample) : factoryTicket(id, reference)).then(
      (d) => { setMissing(false); return d; },
      (e) => { setMissing(e instanceof ApiError && e.status === 404); throw e; },
    ), POLL_MS);
  return (
    <FactoryTicketView data={data} error={error} notFound={missing && !data}
                       projectHref={`/projects/${encodeURIComponent(id)}`} onRetry={reload} />
  );
}

export default function FactoryTicket() {
  const { id = "", ref = "" } = useParams();
  // another ticket is another page: its own state, its own read
  return <TicketPage key={`${id}/${ref}`} id={id} reference={ref} />;
}
