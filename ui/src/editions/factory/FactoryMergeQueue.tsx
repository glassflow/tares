import * as React from "react";
import { Link } from "react-router-dom";

import { request } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { PageHead, ProjectShell } from "./ProjectShell";
import {
  NO, YES, answerRow, mergeQueue,
  type MergeQueueData, type MqChipKey, type MqGroup, type MqRow,
} from "./FactoryMergeQueue.api";

// /projects/:id/merge-queue (TR-869): the pull requests this project has in line to merge, by repo and
// base branch in merge order. Every sentence is the API's (tares/screens/factory_merge_queue.py); this
// page lays them out and, for the person a question is addressed to, answers it: "Yes, merge" and "No,
// hold" are the row's own desk question, so answering here closes it on Overview and Calls too.

const POLL_MS = 30000;
const CAP = 5;
const CARD = "border border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.07)]";
const LINK = "text-[color:var(--accent-deep)]! underline!";

const CHIP: Record<MqChipKey, string> = {
  queued: "border-border bg-muted text-muted-foreground",
  next: "border-border bg-muted text-foreground",
  merging: "border-border bg-muted text-foreground",
  ci: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  approval: "border-[color:var(--accent-deep)] bg-[var(--accent-soft)] text-[color:var(--accent-deep)]",
  other: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  moved: "border-[#e3c777] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  approved: "border-[color:var(--ok-border)] bg-[var(--ok-soft)] text-[color:var(--ok)]",
  hold: "border-border bg-muted text-foreground",
  asking: "border-border bg-muted text-muted-foreground",
  blocked: "border-[color:var(--err)] bg-[var(--err-soft)] text-[color:var(--err)]",
  sent_back: "border-[color:var(--err)] bg-[var(--err-soft)] text-[color:var(--err)]",
};

type Phase = "idle" | "saving" | "failed";

function Check({ label, text, tone }: { label: string; text: string; tone: "ok" | "warn" | "err" | "none" }) {
  const dot = { ok: "bg-[color:var(--ok)]", warn: "bg-[color:var(--accent-deep)]", err: "bg-[color:var(--err)]", none: "bg-border" }[tone];
  return (
    <div className="flex min-w-0 items-baseline gap-2" data-testid={`mq-check-${label.toLowerCase()}`}>
      <dt className="w-[84px] flex-none text-muted-foreground">{label}</dt>
      <dd className="m-0 flex min-w-0 items-baseline gap-2 [overflow-wrap:anywhere]">
        <span aria-hidden className={cn("inline-block h-2 w-2 flex-none translate-y-[-1px] rounded-full", dot)} />
        <span data-user-text>{text}</span>
      </dd>
    </div>
  );
}

function reviewTone(t: string): "ok" | "warn" | "err" | "none" {
  if (t.startsWith("Approved") && !t.includes("older")) return "ok";
  if (t.includes("blocked") || t.includes("asked for changes")) return "err";
  return "warn";
}
function ciTone(t: string): "ok" | "warn" | "err" | "none" {
  if (t === "Passed") return "ok";
  if (t.startsWith("Failing")) return "err";
  if (t.startsWith("Pending")) return "warn";
  return "none";
}
function railTone(t: string): "ok" | "warn" | "err" | "none" {
  if (t === "Allowed") return "ok";
  if (t.startsWith("Blocked")) return "err";
  return "warn";
}

function Row({ row, last, onChanged }: { row: MqRow; last: boolean; onChanged: () => void }) {
  const [phase, setPhase] = React.useState<Phase>("idle");
  const a = row.answer;
  const ask = a && a.kind === "mine" && a.can_answer && a.item ? a.item.id : null;
  const approveOnly = a && a.kind === "withdrawn" && a.can_answer;
  const send = async (go: () => Promise<unknown>) => {
    setPhase("saving");
    try {
      await go();
      setPhase("idle");
      onChanged();
    } catch {
      setPhase("failed");
    }
  };
  const saving = phase === "saving";
  const sentBack = row.state === "sent_back";
  return (
    <li className={cn("grid min-w-0 gap-3 px-4 py-4 sm:px-5", !last && "border-b border-border")}
        data-testid="mq-row" data-state={row.state} data-answer={a?.kind ?? ""} data-attn={row.needs_you ? "yes" : ""}
        data-ticket={row.ticket.ref}>
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-[1_1_320px] items-baseline gap-3">
          <span className="grid h-7 w-7 flex-none place-items-center border border-border bg-muted font-mono text-xs font-semibold" data-testid="mq-position">
            <span aria-hidden>{sentBack ? "↩" : row.position}</span>
            <span className="sr-only">{sentBack ? "Out of line, sent back" : `Position ${row.position}`}</span>
          </span>
          <div className="min-w-0">
            {row.ticket.ref ? (
              <Link to={`../tickets/${encodeURIComponent(row.ticket.ref)}`} relative="path"
                    className={cn(LINK, "mr-2 font-mono text-xs font-medium no-underline!")} data-testid="mq-ticket" data-user-text>
                {row.ticket.ref}
              </Link>
            ) : null}
            <span className="font-semibold [overflow-wrap:anywhere]" data-testid="mq-title" data-user-text>{row.ticket.title}</span>
          </div>
        </div>
        <span data-testid="mq-chip" data-chip={row.chip.key}
              className={cn("flex-none border px-2 py-0.5 font-mono text-[11px] font-medium uppercase tracking-[0.06em]", CHIP[row.chip.key])}>
          {row.chip.text}
        </span>
      </div>

      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 pl-10 text-[13px] text-muted-foreground" data-testid="mq-facts">
        <a href={row.pr.url} target="_blank" rel="noopener noreferrer" className={LINK} data-testid="mq-pr">#{row.pr.number} <span aria-hidden>{"↗"}</span></a>
        {row.builder && <><span aria-hidden>{"·"}</span><span data-user-text>{row.builder.text}</span></>}
        {row.branch && <><span aria-hidden>{"·"}</span><code className="font-mono text-xs [overflow-wrap:anywhere]" data-user-text>{row.branch}</code></>}
      </div>

      <dl className="m-0 grid min-w-0 gap-1 pl-10 text-[13px]" data-testid="mq-checks">
        <Check label="Review" text={row.review} tone={reviewTone(row.review)} />
        <Check label="CI" text={row.ci} tone={ciTone(row.ci)} />
        <Check label="Guardrails" text={row.guardrail} tone={railTone(row.guardrail)} />
      </dl>

      <div className="min-w-0 pl-10 text-[13px]" data-testid="mq-status" role={saving || phase === "failed" ? "status" : undefined}>
        {saving ? (
          <div className="font-semibold">Saving your answer.</div>
        ) : (
          <>
            <div className="[overflow-wrap:anywhere]" data-user-text>
              {row.status.lead && row.status.text.startsWith(row.status.lead)
                ? <><b>{row.status.lead}</b>{row.status.text.slice(row.status.lead.length)}</>
                : row.status.text}
            </div>
            {row.status.note && <div className="mt-0.5 text-muted-foreground" data-testid="mq-note">{row.status.note}</div>}
          </>
        )}
        {phase === "failed" && (
          <div className="mt-1 text-[color:var(--err)]" data-testid="mq-failed">
            Your answer was not saved. Nothing changed, so you can answer again.
          </div>
        )}
        {row.bounce && (
          <div className="mt-1 text-muted-foreground" data-testid="mq-bounce">{row.bounce.text}</div>
        )}
      </div>

      {(ask || approveOnly) && (
        <div className="flex flex-wrap gap-2 pl-10" role="group" aria-label={`Merge ${row.ticket.ref}`} data-testid="mq-actions">
          <Button className="h-8 text-[13px]!" disabled={saving} busy={saving}
                  onClick={() => send(() => (ask ? answerRow(ask, YES) : request(`/api/factory/merge-queue/${encodeURIComponent(row.id)}/approve`, { method: "POST" })))}
                  data-testid="mq-yes">
            Yes, merge
          </Button>
          {ask && (
            <Button variant="secondary" className="h-8 text-[13px]!" disabled={saving}
                    onClick={() => send(() => answerRow(ask, NO))} data-testid="mq-no">
              No, hold
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

function Group({ g, rows, open, onToggle, onChanged }: {
  g: MqGroup; rows: MqRow[]; open: boolean; onToggle: () => void; onChanged: () => void;
}) {
  const [mergedOpen, setMergedOpen] = React.useState(false);
  return (
    <section className={cn(CARD, "min-w-0")} aria-label={g.title} data-testid="mq-group">
      <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border px-4 py-3 sm:px-5">
        <button type="button" onClick={onToggle} aria-expanded={open} data-testid="mq-group-toggle"
                className="flex min-w-0 items-baseline gap-2 border-0 bg-transparent p-0 text-left font-mono text-sm font-semibold text-foreground">
          <span aria-hidden className="inline-block w-3 flex-none text-muted-foreground">{open ? "▾" : "▸"}</span>
          <span className="[overflow-wrap:anywhere]" data-user-text>{g.title}</span>
        </button>
        <span className="text-[13px] text-muted-foreground" data-testid="mq-group-count">{g.count}</span>
        <div className="basis-full pl-5 text-[13px] text-muted-foreground" data-testid="mq-group-sub">{g.sub}</div>
      </div>
      {open && rows.length > 0 && (
        <ol className="m-0 list-none p-0" aria-label={`Merge order for ${g.title}`}>
          {rows.map((r, i) => <Row key={r.id} row={r} last={i === rows.length - 1 && !g.merged} onChanged={onChanged} />)}
        </ol>
      )}
      {open && g.merged && (
        <div className={cn("px-4 py-3 text-[13px] sm:px-5", rows.length > 0 && "border-t border-border")} data-testid="mq-merged">
          <button type="button" onClick={() => setMergedOpen(!mergedOpen)} aria-expanded={mergedOpen}
                  className="border-0 bg-transparent p-0 text-[13px] font-semibold text-foreground" data-testid="mq-merged-toggle">
            <span aria-hidden className="mr-2 text-muted-foreground">{mergedOpen ? "▾" : "▸"}</span>{g.merged.title}
          </button>
          {mergedOpen && (
            <ul className="m-0 mt-2 grid list-none gap-1 p-0 text-muted-foreground">
              {g.merged.items.map((m) => (
                <li key={m.n} className="[overflow-wrap:anywhere]" data-testid="mq-merged-item">
                  <span className="font-mono" data-user-text>{m.tk}</span> <span aria-hidden>{"·"}</span> <span data-user-text>{m.title}</span>{" "}
                  <span aria-hidden>{"·"}</span> #{m.n} <span aria-hidden>{"·"}</span> merged {m.when} by <span data-user-text>{m.by}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function Queue({ d, reload }: { d: MergeQueueData; reload: () => void }) {
  const total = d.groups.reduce((n, g) => n + g.rows.length, 0);
  const [all, setAll] = React.useState(false);
  const [closed, setClosed] = React.useState<Set<string>>(new Set());
  const cap = all ? total : CAP;
  let left = cap;
  const shown = d.groups.map((g) => {
    const rows = g.rows.slice(0, Math.max(left, 0));
    left -= rows.length;
    return rows;
  });
  const toggle = (key: string) => setClosed((c) => { const n = new Set(c); if (!n.delete(key)) n.add(key); return n; });
  return (
    <>
      <div className="grid gap-3" data-testid="mq-groups">
        {d.groups.map((g, i) => {
          const key = `${g.repo}\u0000${g.base}`;
          if (!shown[i].length && g.rows.length) return null;
          return <Group key={key} g={g} rows={shown[i]} open={!closed.has(key)} onToggle={() => toggle(key)} onChanged={reload} />;
        })}
      </div>
      {total > CAP && (
        <div className="flex flex-wrap items-center gap-3 text-[13px] text-muted-foreground" data-testid="mq-more">
          <span>Showing {Math.min(cap, total)} of {total} pull requests.</span>
          <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => setAll(!all)} data-testid="mq-more-toggle">
            {all ? "Show fewer" : `Show ${total - CAP} more`}
          </Button>
        </div>
      )}
    </>
  );
}

function MergeQueue({ id }: { id: string }) {
  const { data, error, reload } = usePagePolling<MergeQueueData>(() => mergeQueue(id), POLL_MS);
  if (!data) {
    return (
      <div className="grid min-w-0 gap-5" data-testid="mq-wait">
        <PageHead title="Merge queue" />
        {error ? (
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Try again</Button>} data-testid="mq-error">
            <b>The merge queue did not load.</b> <span className="text-foreground">Check your connection, then try again.</span>
          </Banner>
        ) : <div role="status" aria-busy="true" aria-label="Loading the merge queue"><Skeleton className="h-40" /></div>}
      </div>
    );
  }
  return (
    <div className="grid min-w-0 max-w-[980px] gap-5" data-testid="mq-page">
      <PageHead title="Merge queue" sub={<span className="text-xs" data-testid="mq-aside">{data.aside || data.tab.note}</span>} />
      {error && <Banner tone="warn" data-testid="mq-stale"><b>The merge queue could not be refreshed.</b> <span className="text-foreground">What shows is from the last time it loaded.</span></Banner>}
      {data.intro && <p className="m-0 max-w-[760px] text-[15px] text-muted-foreground" data-testid="mq-intro">{data.intro}</p>}
      {data.groups.length === 0
        ? <div className={cn(CARD, "px-5 py-4 text-muted-foreground")} data-testid="mq-empty">{data.empty}</div>
        : <Queue d={data} reload={reload} />}
      <div className="grid gap-1 text-[13px] text-muted-foreground" data-testid="mq-foot">
        {data.footer.map((f, i) => <p key={i} className="m-0">{f}</p>)}
      </div>
    </div>
  );
}

export default function FactoryMergeQueue() {
  return <ProjectShell page="merge-queue">{({ id }) => <MergeQueue id={id} />}</ProjectShell>;
}
