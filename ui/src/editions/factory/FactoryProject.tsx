import * as React from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Dialog, Initials, Input, ProgressBar, Textarea, useToast } from "../../components/edition";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Pencil } from "../../components/icons";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "../../lib/utils";
import {
  type ProjectPeople, type ProjectPerson,
  type FactoryProjectData, type MovingTicket, type ProjectCall, type ProjectMilestone, type TicketState,
} from "./FactoryProject.api";
import type { HomeAction, HomeItem } from "./home.types";
import { AllowDialog } from "./FactoryHome";
import { looksLikeCommand } from "./checkText";
import { DetailLines } from "./HomeDetail";
import { GroupHead, groupWaiting, OlderFold, QuestionChoices } from "./waitingGroups";
import { sampleClose } from "./projectSamples";
import { PageHead, pageHref, ProjectShell, waitingItems, type ProjectCtx, type ProjectPage } from "./ProjectShell";

// A factory project's Overview (/projects/:id, TR-734, TR-1245): only what needs attention. What
// waits for you, a line for what waits for others, three cards that open the project's pull
// requests, milestones and decisions, and the few tickets moving now. The rest of the project has
// a page of its own in the project sidebar (ProjectShell); the milestone rows, the people, the
// calls and decisions and Delete are drawn here and used by those pages. Allow is done here, in its
// own dialog, so several can be allowed in a row. Any other button that does something on Home
// (copy the hand-over command) links there with an arrow and never acts here. The cell shapes every
// string for people; names, goals, titles, calls, decisions and doc titles are the person's own
// text and marked data-user-text.

const SHOW_WAITING = 3;
const NOT_CLOSED = "Try again in a moment.";
const NOT_SAVED = "That was not saved. Try again in a moment.";
const STEPS = ["Spec", "Build", "Review", "Done"];

const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-[color:var(--accent-deep)]";
const MUTED = "text-xs text-muted-foreground";
const LINK = "text-[color:var(--accent-deep)]! underline!";
const CARD = "border border-border bg-card";
const SECTION_H = "m-0! text-base!";
const ROW = "border-b border-border px-4 py-3 last:border-b-0";

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
            <span className={cn("grid size-[22px] flex-none place-items-center border border-foreground bg-[var(--input-bg)] font-mono text-[11px] text-foreground",
                                done && "bg-foreground text-background", now && "bg-[var(--accent)] text-[color:var(--on-accent)]")}>
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

function ActionButton({ action, onClose, onKeep, onAllow }: { action: HomeAction; onClose?: () => void; onKeep?: (refs: string[]) => Promise<void>; onAllow?: (a: HomeAction) => void }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const cls = "h-auto min-h-8 whitespace-normal py-1 text-left text-[13px]! max-sm:flex-1 max-sm:justify-center max-sm:text-center";
  const variant = action.do === "close" ? "primary" : "secondary";
  const strong = action.primary && action.do !== "close" ? "border-foreground!" : "";
  if (action.do === "close") {
    return <Button variant={variant} className={cn(cls, strong)} onClick={onClose} data-testid="waiting-close">{action.label}</Button>;
  }
  if (action.do === "keep_assumptions" && onKeep) {
    // kept here, on the project page: no trip to Home
    const refs = Array.isArray(action.refs) ? action.refs.map(String) : [];
    const keep = async () => {
      setBusy(true); setError("");
      try { await onKeep(refs); } catch (e) { setError(e instanceof ApiError ? e.message : NOT_SAVED); setBusy(false); }
    };
    return (
      <span className="grid gap-1">
        <Button variant={variant} className={cn(cls, strong)} busy={busy} onClick={() => void keep()} data-testid="waiting-keep">{action.label}</Button>
        {error && <span role="alert" className="text-[13px] text-[color:var(--err)]">{error}</span>}
      </span>
    );
  }
  if (action.do === "allow" && onAllow) {
    return <Button variant={variant} className={cn(cls, strong)} onClick={() => onAllow(action)} data-testid="waiting-allow">{action.label}</Button>;
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

function WaitingRow({ item, onClose, onKeep, onAllow }: { item: HomeItem; onClose: () => void; onKeep?: (refs: string[]) => Promise<void>; onAllow?: (item: HomeItem, a: HomeAction) => void }) {
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <div className={cn(ROW, "flex flex-wrap items-center justify-between gap-x-4 gap-y-3 hover:bg-[var(--row-hover)]")}
         data-testid="waiting-row" data-kind={item.kind} id={`waiting-${item.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`}>
      <div className="min-w-0 flex-[1_1_260px]">
        <div className={KIND}>{item.label}</div>
        <div className="my-0.5 font-bold [overflow-wrap:anywhere]" data-user-text>{item.title}</div>
        <Detail text={item.detail} />
        <DetailLines lines={item.lines} />
        {item.meta && <div className={cn(MUTED, "mt-0.5")}>{item.meta}</div>}
        {/* the options show here; they are answered on Home, where the Answer button opens */}
        <QuestionChoices item={item} render={(a) => (
          <Link key={a.label} to={`/#waiting`} className="border border-border px-2.5 py-1 text-[13px] text-foreground! no-underline! hover:bg-[var(--row-hover)]" data-user-text>{a.label} ↗</Link>
        )} />
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2 max-sm:w-full">
          {actions.map((a) => <ActionButton key={a.label} action={a} onClose={onClose} onKeep={onKeep} onAllow={onAllow && ((x) => onAllow(item, x))} />)}
        </div>
      )}
    </div>
  );
}

/** What waits on someone else in this project (a team cell), each item named for the people it
 *  waits for (factory_owners on the cell). Nothing to press: only they may act on it. */
function WaitingOthers({ items }: { items: HomeItem[] }) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Waiting for others" className="min-w-0" data-testid="waiting-others">
      <SectionHead title="Waiting for others"
                   aside={<span className={MUTED} data-testid="waiting-others-count">{plural(items.length, "thing", "things")}</span>} />
      <div className={CARD}>
        {items.map((it) => (
          <div key={it.id} className={cn(ROW, "min-w-0")} data-testid="waiting-other-row" data-kind={it.kind}>
            <div className={KIND} data-user-text>{it.label}</div>
            <div className="my-0.5 font-bold [overflow-wrap:anywhere]" data-user-text>{it.title}</div>
            <div className="mt-0.5" data-user-text>{it.detail}</div>
            <DetailLines lines={it.lines} />
            {it.meta && <div className={cn(MUTED, "mt-0.5")}>{it.meta}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}

function Waiting({ items, onClose, onKeep, onAllow }: {
  items: HomeItem[]; onClose: () => void; onKeep?: (refs: string[]) => Promise<void>; onAllow?: (item: HomeItem, a: HomeAction) => void;
}) {
  const [all, setAll] = React.useState(false);
  const g = groupWaiting(items);
  const count = items.length;
  const rest = all ? g.rest : g.rest.slice(0, SHOW_WAITING);
  const more = g.rest.length - rest.length;
  const arrows = items.some((i) => i.question || i.actions.some((a) => a.do && a.do !== "close" && !(a.do === "keep_assumptions" && onKeep) && !(a.do === "allow" && onAllow)));
  const row = (it: HomeItem) => <WaitingRow key={it.id} item={it} onClose={onClose} onKeep={onKeep} onAllow={onAllow} />;
  return (
    <section id="waiting" aria-label="Waiting for you" className="min-w-0 scroll-mt-4" data-testid="waiting">
      <SectionHead title="Waiting for you"
                   aside={count > 0 && <span className={MUTED} data-testid="waiting-count">{plural(count, "thing", "things")}</span>} />
      {count === 0 ? (
        <Empty testid="waiting-empty">Nothing waits for you in this project.</Empty>
      ) : (
        <>
          {arrows && <div className={cn(MUTED, "-mt-1 mb-2")}>Buttons with an arrow open Home, where they are done.</div>}
          <div className={CARD}>
            {g.now.length > 0 && <GroupHead title="The crew is waiting on you" testid="waiting-now" />}
            {g.now.map(row)}
            {g.now.length > 0 && rest.length > 0 && <GroupHead title="When you have a moment" testid="waiting-rest" />}
            {rest.map(row)}
            {more > 0 && (
              <div className="border-t border-border px-4 py-2.5">
                <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => setAll(true)} data-testid="waiting-more">
                  Show {more} more
                </Button>
              </div>
            )}
            <OlderFold items={g.older}>{(older) => older.map(row)}</OlderFold>
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

/** At most this many moving tickets on Overview, so it fits on one screen; the Tickets page has the rest. */
const SHOW_MOVING = 3;

function Moving({ d }: { d: FactoryProjectData }) {
  const { total, merged, canceled } = d.counts;
  const live = total - canceled;
  const shown = d.moving.slice(0, SHOW_MOVING);
  const rest = d.moving_total - shown.length;
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
        {shown.map((t) => <TicketRow key={t.ref} t={t} />)}
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

/** What a check's latest result says, in words: passed, failed, or not checked yet. */
function checkWords(c: ProjectMilestone["checks"][number], forWhom = "you"): string {
  if (c.result === "pass") return c.for === "person" ? "Works" : `Passed${c.by ? `, checked by ${c.by}` : ""}`;
  if (c.result === "fail") return c.for === "person" ? "Something's wrong" : `Failed${c.by ? `, checked by ${c.by}` : ""}`;
  return c.for === "person" ? `Waiting for ${forWhom}` : "Not checked yet";
}

/** Whom a milestone's person checks wait for, in words: "you", or "Kiran", "Kiran and Adam", "no one". */
function checksForWhom(m: ProjectMilestone): string {
  if (m.yours !== false) return "you";
  const names = (m.checks_for ?? []).map((w) => w.name);
  if (names.length === 0) return "no one";
  return names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** One check for a person: Works, or Something's wrong with a sentence (TR-1017). */
function PersonCheck({ c, onAnswer }: {
  c: ProjectMilestone["checks"][number];
  onAnswer?: (n: number, result: "pass" | "fail", note?: string) => Promise<void>;
}) {
  const [open, setOpen] = React.useState(false);
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState<"" | "pass" | "fail">("");
  const [error, setError] = React.useState("");
  const answer = async (result: "pass" | "fail") => {
    setBusy(result); setError("");
    try {
      await onAnswer?.(c.n, result, result === "fail" ? note.trim() : undefined);
      setOpen(false); setNote("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : NOT_SAVED);
    } finally {
      setBusy("");
    }
  };
  if (c.result || !onAnswer) return null;
  return (
    <div className="mt-1.5 grid gap-2" data-testid="person-check-actions">
      <div className="flex flex-wrap gap-2">
        <Button className="h-8 text-[13px]!" busy={busy === "pass"} disabled={busy === "fail"} onClick={() => answer("pass")} data-testid="check-works">Works</Button>
        <Button variant="secondary" className="h-8 text-[13px]!" aria-expanded={open} disabled={busy !== ""}
                onClick={() => setOpen((o) => !o)} data-testid="check-wrong">Something's wrong</Button>
      </div>
      {open && (
        <div className="grid gap-2">
          <Textarea label="What did you see? The crew gets a ticket with your words." value={note} data-testid="check-note"
                    onChange={(e) => setNote(e.target.value)} className="min-h-14" />
          <div className="flex flex-wrap gap-2">
            <Button className="h-8 text-[13px]!" busy={busy === "fail"} disabled={note.trim() === ""} onClick={() => answer("fail")} data-testid="check-send">Send to the crew</Button>
            <Button variant="secondary" className="h-8 text-[13px]!" disabled={busy !== ""} onClick={() => setOpen(false)}>Cancel</Button>
          </div>
        </div>
      )}
      {error && <div role="alert" className="text-[13px] text-[color:var(--err)]" data-testid="check-error">{error}</div>}
    </div>
  );
}

export function MilestoneRow({ m, onAnswer, onAccept }: {
  m: ProjectMilestone;
  onAnswer?: (m: ProjectMilestone, n: number, result: "pass" | "fail", note?: string) => Promise<void>;
  onAccept?: (m: ProjectMilestone) => Promise<void>;
}) {
  const { total, merged } = m.counts;
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const accept = async () => {
    setBusy(true); setError("");
    try { await onAccept?.(m); } catch (e) { setError(e instanceof ApiError ? e.message : NOT_SAVED); } finally { setBusy(false); }
  };
  const mine = m.checks.filter((c) => c.for === "person" && !c.result).length;
  return (
    <div className={ROW} id={`milestone-${m.id}`} data-testid="milestone-row" data-ms={m.number} data-accepted={m.accepted}>
      <div className="grid gap-x-6 gap-y-1.5 md:grid-cols-[minmax(0,1fr)_250px]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 font-semibold [overflow-wrap:anywhere]">
            <span data-user-text>{m.name}</span>
            {m.done && (
              <span className="border border-[color:var(--ok-border)] bg-[var(--ok-soft)] px-1.5 py-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.06em] text-[color:var(--ok)]">
                {m.accepted ? "Accepted" : "Merged"}
              </span>
            )}
          </div>
          {m.goal && <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{m.goal}</div>}
          {m.needs_person && (
            <div className="mt-1 text-[13px] font-semibold text-[color:var(--accent-deep)]" data-testid="milestone-needs">
              {mine > 0 ? `${plural(mine, "check", "checks")} for you to try` : "The crew has not recorded its checks. Accept it as it is, or ask the crew."}
            </div>
          )}
          {m.checks.length > 0 && (
            <details className="group mt-1" data-testid="milestone-checks" open={m.needs_person && mine > 0}>
              <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 py-0.5 text-[13px] text-[color:var(--accent-deep)] [&::-webkit-details-marker]:hidden">
                <span aria-hidden className="text-[11px] transition-transform group-open:rotate-90">{"▸"}</span>
                {plural(m.checks.length, "check", "checks")} to pass
              </summary>
              <ol className="mb-0.5 mt-1.5 list-decimal pl-5">
                {m.checks.map((c) => (
                  <li key={c.n} className="my-1.5 text-[13px] [overflow-wrap:anywhere]" data-testid="milestone-check" data-for={c.for} data-result={c.result ?? ""}>
                    {looksLikeCommand(c.check)
                      ? <code data-user-text className="whitespace-pre-wrap font-mono text-[12.5px]">{c.check}</code>
                      : <span data-user-text>{c.check}</span>}
                    {c.expect && <span className="block text-xs text-muted-foreground">Should show: <span data-user-text>{c.expect}</span></span>}
                    <span className="block text-xs text-muted-foreground" data-testid="check-state">
                      {c.for === "person" ? (m.yours === false ? `For ${checksForWhom(m)}. ` : "For you. ") : "The crew's. "}{checkWords(c, checksForWhom(m))}
                      {c.note && <>: <span data-user-text>{c.note}</span></>}
                    </span>
                    {c.for === "person" && m.done && m.yours !== false && <PersonCheck c={c} onAnswer={onAnswer && ((n, r, note) => onAnswer(m, n, r, note))} />}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {m.can_accept && onAccept && (
            <div className="mt-1.5">
              <Button variant="secondary" className="h-8 text-[13px]!" busy={busy} onClick={accept} data-testid="milestone-accept">Accept as it is</Button>
              {error && <div role="alert" className="mt-1 text-[13px] text-[color:var(--err)]">{error}</div>}
            </div>
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

// ── who is on the project ────────────────────────────────────────────────────

function PersonLine({ p, children }: { p: ProjectPerson; children?: React.ReactNode }) {
  return (
    <div className="flex min-w-0 items-center gap-2.5 py-1" data-testid="people-person">
      <Initials name={p.name} picture={p.picture} className="size-7 text-[11px]" aria-hidden />
      <span className="min-w-0 truncate" data-user-text>{p.name}</span>
      {children}
      <span className={cn(MUTED, "ml-auto whitespace-nowrap")}>{plural(p.tickets ?? 0, "ticket", "tickets")}</span>
    </div>
  );
}

/** The pencil on the lead's line: pick someone else to lead. */
function ChangeLead({ people, lead, onPick }: { people: ProjectPerson[]; lead: string; onPick: (id: string) => Promise<void> }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const choices = people.filter((x) => x.id !== lead);
  if (choices.length === 0) return null;
  const pick = async (id: string) => {
    setBusy(true); setError("");
    try { await onPick(id); } catch (e) { setError(e instanceof ApiError ? e.message : NOT_SAVED); } finally { setBusy(false); }
  };
  return (
    <>
      <Select value="" disabled={busy} onValueChange={(v) => void pick(v)}>
        <SelectPrimitive.Trigger aria-label="Change the lead" title="Change the lead" data-testid="people-set-lead"
          className="-ml-1.5 inline-flex! size-6! shrink-0 items-center justify-center border-0! bg-transparent! p-0! text-muted-foreground! hover:text-foreground! disabled:opacity-45!">
          <Pencil className="size-3.5" />
        </SelectPrimitive.Trigger>
        <SelectContent align="start">
          {choices.map((x) => <SelectItem key={x.id} value={x.id} data-user-text>{x.name}</SelectItem>)}
        </SelectContent>
      </Select>
      {error && <div role="alert" className="text-[13px] text-[color:var(--err)]">{error}</div>}
    </>
  );
}

/** Pick a person from the cell, then act: set the lead or add a member. */
function PickPerson({ people, exclude, label, testid, onPick }: {
  people: ProjectPerson[]; exclude: string[]; label: string; testid: string; onPick: (id: string) => Promise<void>;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const choices = people.filter((x) => !exclude.includes(x.id));
  if (choices.length === 0) return null;
  const pick = async (id: string) => {
    setBusy(true); setError("");
    try { await onPick(id); } catch (e) { setError(e instanceof ApiError ? e.message : NOT_SAVED); } finally { setBusy(false); }
  };
  return (
    <div className="mt-1.5 grid gap-1">
      <Select value="" disabled={busy} onValueChange={(v) => void pick(v)}>
        <SelectTrigger aria-label={label} data-testid={testid} className="h-8! text-[13px]!">
          <SelectValue placeholder={busy ? "Saving..." : label} />
        </SelectTrigger>
        <SelectContent>
          {choices.map((x) => <SelectItem key={x.id} value={x.id} data-user-text>{x.name}</SelectItem>)}
        </SelectContent>
      </Select>
      {error && <div role="alert" className="text-[13px] text-[color:var(--err)]">{error}</div>}
    </div>
  );
}

export function People({ people, onLead, onAdd }: {
  people: ProjectPeople; onLead?: (id: string) => Promise<void>; onAdd?: (id: string) => Promise<void>;
}) {
  const { lead, members } = people;
  const on = [lead?.id, ...members.map((m) => m.id)].filter(Boolean) as string[];
  return (
    <section id="people" aria-label="People" className="min-w-0 scroll-mt-4" data-testid="project-people">
      <SectionHead title="People" />
      <div className={cn(CARD, "px-4 py-3")}>
        <div className={cn(MUTED, "mb-0.5")}>Lead</div>
        {lead ? (
          <PersonLine p={lead}>
            {people.can_set_lead && onLead && <ChangeLead people={people.people} lead={lead.id} onPick={onLead} />}
          </PersonLine>
        ) : (
          <>
            <div className="py-1 text-[13px]" data-testid="people-no-lead">No lead yet.</div>
            {people.can_set_lead && onLead && (
              <PickPerson people={people.people} exclude={[]} label="Set a lead" testid="people-set-lead" onPick={onLead} />
            )}
          </>
        )}
        <div className={cn(MUTED, "mb-0.5 mt-3")}>Members</div>
        {members.length === 0 ? (
          <div className="py-1 text-[13px] text-muted-foreground" data-testid="people-no-members">No members yet.</div>
        ) : members.map((m) => <PersonLine key={m.id} p={m} />)}
        {people.can_add && onAdd && (
          <PickPerson people={people.people} exclude={on} label="Add a member" testid="people-add-member" onPick={onAdd} />
        )}
      </div>
    </section>
  );
}


// ── calls and decisions ───────────────────────────────────────────────────────

export function CallRow({ c, onDecide }: { c: ProjectCall; onDecide: (c: ProjectCall, state: "kept" | "overturned", words?: string) => Promise<void> }) {
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
                onClick={() => setOpen((o) => !o)} data-testid="call-overturn">Change</Button>
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

/** One decision: the question, the answer chosen, the person's own words when they add something. */
export function DecisionRow({ x, wide }: { x: FactoryProjectData["decisions"][number]; wide?: boolean }) {
  if (wide) {
    // its own page: the question and when it was decided on one line, the answer below
    return (
      <div className="px-5 py-4" data-testid="decision-row">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <div className="min-w-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{x.question}</div>
          <div className={cn(MUTED, "whitespace-nowrap")}>{x.when}</div>
        </div>
        <div className="mt-1.5 border-l-[3px] border-[color:var(--accent)] pl-3 text-[15px] font-medium [overflow-wrap:anywhere]" data-user-text data-testid="decision-answer">{x.answer}</div>
        {x.said && <div className="mt-1.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="decision-said">You said: <span data-user-text>{x.said}</span></div>}
      </div>
    );
  }
  return (
    <div className={ROW} data-testid="decision-row">
      <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-user-text>{x.question}</div>
      <div className="my-0.5 border-l-[3px] border-[color:var(--accent)] pl-2.5 font-medium [overflow-wrap:anywhere]" data-user-text data-testid="decision-answer">{x.answer}</div>
      {x.said && <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-testid="decision-said">You said: <span data-user-text>{x.said}</span></div>}
      <div className={MUTED}>{x.when}</div>
    </div>
  );
}

// ── Delete ────────────────────────────────────────────────────────────────────

/** Deleting the project, behind a dialog that asks for its name. */
export function DeleteProject({ name, onDelete }: { name: string; onDelete: () => Promise<void> }) {
  const [open, setOpen] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const go = async () => {
    setBusy(true);
    try { await onDelete(); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Could not reach the cell. Try again in a moment."); setBusy(false); }
  };
  const shut = () => { if (busy) return; setOpen(false); setTyped(""); setError(""); };
  return (
    <section aria-label="Delete project" className="min-w-0 border-t border-border pt-5" data-testid="delete-project">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-[1_1_320px]">
          <h2 className="m-0! text-base!">Delete this project</h2>
          <p className="m-0! mt-0.5 text-[13px] text-muted-foreground">Removes its tickets, decisions, calls and docs from Tares. Pull requests and branches on GitHub stay.</p>
        </div>
        <Button variant="secondary" className="text-[color:var(--err)]!" onClick={() => setOpen(true)} data-testid="delete-open">Delete project</Button>
      </div>
      <Dialog open={open} onOpenChange={(o) => { if (!o) shut(); }} data-testid="delete-dialog"
              title={`Delete ${name}?`}
              description="This cannot be undone. Its tickets, decisions, calls and docs are deleted. Pull requests and branches on GitHub stay."
              footer={<>
                <Button variant="secondary" disabled={busy} onClick={shut} data-testid="delete-cancel">Cancel</Button>
                <Button busy={busy} disabled={typed.trim() !== name} onClick={() => void go()} data-testid="delete-confirm">Delete project</Button>
              </>}>
        <form onSubmit={(e) => { e.preventDefault(); if (typed.trim() === name && !busy) void go(); }}>
          <Input label={`Type ${name} to confirm`} value={typed} autoComplete="off" spellCheck={false}
                 error={error || undefined} onChange={(e) => { setTyped(e.target.value); if (error) setError(""); }} data-testid="delete-name" />
        </form>
      </Dialog>
    </section>
  );
}

// ── Overview ──────────────────────────────────────────────────────────────────

/** "Waiting for others: 2 things", with the list behind Show. */
function OthersLine({ items }: { items: HomeItem[] }) {
  const [open, setOpen] = React.useState(false);
  if (items.length === 0) return null;
  return (
    <div className="grid min-w-0 gap-3">
      <div className="text-[13px] text-muted-foreground" data-testid="waiting-others-line">
        Waiting for others: {plural(items.length, "thing", "things")}.{" "}
        <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} data-testid="waiting-others-toggle"
                className={cn("border-0! bg-transparent! p-0! text-[13px]! font-normal! shadow-none!", LINK)}>
          {open ? "Hide" : "Show"}
        </button>
      </div>
      {open && <WaitingOthers items={items} />}
    </div>
  );
}

function SummaryCard({ to, title, big, line, more, testid }: {
  to: string; title: string; big: string; line: React.ReactNode; more: string; testid: string;
}) {
  return (
    <Link to={to} data-testid={testid}
          className={cn(CARD, "flex min-h-[132px] min-w-0 flex-col gap-1.5 p-4 text-foreground! no-underline! hover:border-foreground! hover:bg-[var(--row-hover)]")}>
      <span className="text-[13px] text-muted-foreground">{title}</span>
      <span className="text-[24px] font-bold leading-tight" data-testid="summary-big">{big}</span>
      <span className="line-clamp-2 text-[13px] [overflow-wrap:anywhere]" data-testid="summary-line">{line}</span>
      <span className="mt-auto pt-1 text-[13px] text-[color:var(--accent-deep)]">{more} {"›"}</span>
    </Link>
  );
}

function Summary({ d }: { d: FactoryProjectData }) {
  const prs = d.prs_counts ?? { open: 0, merged: 0, closed: 0 };
  const prTotal = d.prs_total ?? 0;
  const pr = d.prs_recent?.[0];
  const ms = d.milestones;
  const now = ms.find((m) => !m.done) ?? ms[ms.length - 1];
  const open = d.calls.length;
  return (
    <div className="grid gap-4 md:grid-cols-3" data-testid="summary">
      <SummaryCard testid="summary-prs" to={pageHref(d.id, "prs")} title="Pull requests"
                   big={prTotal === 0 ? "None yet" : prs.open > 0 ? `${prs.open} open` : `${prs.merged} merged`}
                   line={pr ? <>{pr.number ? `#${pr.number} ` : ""}<span data-user-text>{pr.title}</span>{pr.ci ? `, ${pr.ci}` : ""}</>
                     : "Each pull request the crew opens shows here."}
                   more={prTotal > 0 ? `All ${plural(prTotal, "pull request", "pull requests")}` : "Pull requests"} />
      <SummaryCard testid="summary-milestones" to={pageHref(d.id, "milestones")} title="Milestones"
                   big={now ? `${now.counts.merged} of ${now.counts.total} merged` : "None yet"}
                   line={now ? <span data-user-text>{now.name}</span> : "The crew adds them when it plans the build."}
                   more={ms.length > 0 ? `All ${plural(ms.length, "milestone", "milestones")}` : "Milestones"} />
      <SummaryCard testid="summary-decisions" to={open > 0 ? pageHref(d.id, "decisions") : `/projects/${encodeURIComponent(d.id)}/decisions`}
                   title="Decisions and calls"
                   big={open > 0 ? `${open} to confirm` : d.decisions.length > 0 ? plural(d.decisions.length, "decision", "decisions") : "None yet"}
                   line={open > 0 ? <span data-user-text>{d.calls[0].question}</span>
                     : d.decisions[0] ? <span data-user-text>{d.decisions[0].answer}</span> : "When you answer the crew, it is kept here."}
                   more={open > 0 ? "Calls to confirm" : "All decisions"} />
    </div>
  );
}

/** Overview, for a project that was read. `start` is a Close part way, for a sample. */
function Overview({ ctx, start }: { ctx: ProjectCtx; start?: { open: boolean; error: string } }) {
  const { d, act, sample, reload } = ctx;
  const { toast } = useToast();
  const [asking, setAsking] = React.useState(Boolean(start?.open));
  const [busy, setBusy] = React.useState(false);
  const [closeError, setCloseError] = React.useState(start?.error ?? "");
  const [kept, setKept] = React.useState<string[]>([]);
  const [allowing, setAllowing] = React.useState<{ item: HomeItem; desk: string; mayLimit: boolean } | null>(null);
  const [moving, setMoving] = React.useState(false);
  const finished = d.state === "finished";
  const items = waitingItems(d, kept);

  const startNow = async () => {
    setMoving(true);
    try {
      await act.later(false);
      toast(`${d.name} is started again. It is back on your home.`);
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Could not reach the cell. Try again in a moment.");
    } finally {
      setMoving(false);
    }
  };
  const confirm = async () => {
    setBusy(true);
    try {
      await act.close();
      setCloseError("");
      toast(`${d.name} is closed. It moved to Done.`);
    } catch (err) {
      setCloseError(err instanceof ApiError ? err.message : NOT_CLOSED);
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };

  // "Keep both" on a calls item keeps each call here; the item leaves the list once they are kept
  const onKeep = async (refs: string[]) => {
    for (const ref of refs) {
      const c = d.calls.find((x) => x.ref === ref) ?? { ref, question: "", choice: "", why: "", made: "", tickets: [] };
      await act.decide(c, "kept");
    }
    const item = d.waiting.find((w) => w.actions.some((a) => a.do === "keep_assumptions" && Array.isArray(a.refs) && a.refs.join() === refs.join()));
    if (item) setKept((k) => [...k, item.id]);
    toast(refs.length === 1 ? "Kept it. The crew carries on." : refs.length === 2 ? "Kept both. The crew carries on." : `Kept all ${refs.length}. The crew carries on.`);
  };

  return (
    <div className="grid min-w-0 gap-6" data-testid="factory-project" data-state={d.state}>
      <PageHead title="Overview" actions={(d.later || (d.can_close && !finished)) && (
        <>
          {d.later && <Button busy={moving} onClick={() => void startNow()} data-testid="project-start-now">Start now</Button>}
          {d.can_close && !finished && <Button onClick={() => setAsking(true)} data-testid="project-close">Close</Button>}
        </>
      )}>
        {d.goal && <p className="m-0! max-w-[760px] text-[15px] [overflow-wrap:anywhere]" data-user-text data-testid="project-goal">{d.goal}</p>}
        <Steps stage={d.stage} finished={finished} />
      </PageHead>
      {closeError && (
        <Banner tone="error" data-testid="close-error">
          <b>The project was not closed.</b> <span className="text-foreground" data-user-text>{closeError}</span>
        </Banner>
      )}
      <div className="grid min-w-0 gap-3">
        <Waiting items={items} onClose={() => setAsking(true)} onKeep={onKeep}
                 onAllow={sample ? undefined : (item, a) => { if (typeof a.desk === "string") setAllowing({ item, desk: a.desk, mayLimit: a.may_limit === true }); }} />
        {!finished && <OthersLine items={d.waiting_others ?? []} />}
      </div>
      <Summary d={d} />
      <Moving d={d} />

      <Dialog open={asking} onOpenChange={(o) => { if (!o && !busy) setAsking(false); }} data-testid="close-dialog"
              title={`Close ${d.name}?`}
              description="It moves to Done. Handing it to the crew again opens it."
              footer={
                <>
                  <Button variant="secondary" disabled={busy} onClick={() => setAsking(false)} data-testid="close-cancel">Cancel</Button>
                  <Button busy={busy} onClick={confirm} data-testid="close-confirm">Close</Button>
                </>
              } />
      {allowing && (
        <AllowDialog key={allowing.item.id} item={allowing.item} desk={allowing.desk} mayLimit={allowing.mayLimit} onClose={() => setAllowing(null)}
          onDone={(r) => {
            toast(r.added ? "Allowed. The crew restarts it with the permission once it is between turns."
              : "That grant was already kept, so nothing changed.");
            setKept((k) => [...k, allowing.item.id]);
            setAllowing(null);
            reload();
          }} />
      )}
    </div>
  );
}

// today's sections that have a page of their own now: an old #section link opens that page
const MOVED: [RegExp, ProjectPage][] = [
  [/^(people|secrets)$/, "people"],
  [/^(milestones|milestone-.+)$/, "milestones"],
  [/^decisions$/, "decisions"],
  [/^docs$/, "docs"],
  [/^pull-requests$/, "prs"],
];

export default function FactoryProject({ onOther }: { onOther?: () => void } = {}) {
  const { id = "" } = useParams();
  const { hash, search } = useLocation();
  let at = hash.slice(1);
  try { at = decodeURIComponent(at); } catch { /* a malformed address keeps its raw text */ }
  const moved = at ? MOVED.find(([re]) => re.test(at))?.[1] : undefined;
  if (moved) {
    const keep = moved === "people" || moved === "milestones" ? hash : "";
    // #decisions is the Decisions tab, not the item's first tab (Calls)
    const to = moved === "decisions" ? `/projects/${encodeURIComponent(id)}/decisions` : pageHref(id, moved);
    return <Navigate replace to={`${to}${search}${keep}`} />;
  }
  return (
    <ProjectShell page="overview" onOther={onOther}>
      {(ctx) => <Overview ctx={ctx} start={ctx.sample ? sampleClose(ctx.sample) : undefined} />}
    </ProjectShell>
  );
}
