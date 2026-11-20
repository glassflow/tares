import * as React from "react";
import { Link } from "react-router-dom";

import { request } from "../../api";
import { Banner, Button, Collapse, Skeleton, StatusDot, toneOf } from "../../components/edition";
import { cn } from "../../lib/utils";
import {
  NO, YES, answerRow,
  type MergeQueueData, type MqChipKey, type MqGroup, type MqRow,
} from "./FactoryMergeQueue.api";

// The Merge queue tab of a project's Pull requests page (TR-869): the pull requests this project has in
// line to merge, by repo and base branch in merge order. Every sentence is the API's
// (tares/screens/factory_merge_queue.py); this panel lays them out and, for the person a question is
// addressed to, answers it: "Yes, merge" and "No, hold" are the row's own desk question, so answering
// here closes it on Overview and Calls too. The page owns the read (it needs the counts for the tab
// strip); the panel shows what it is given.
//
// A row answers to the width of this panel, not of the window (the `mq` container): at 559px and below
// it is an inset card with its chip at the top right and full-width buttons, as in the approved mock.

const CAP = 5;
const CARD = "border border-border bg-card";
const LINK = "text-brand-deep underline";

const CHIP: Record<MqChipKey, string> = {
  queued: "border-border bg-[var(--chip-bg)] text-muted-foreground",
  next: "border-border bg-[var(--chip-bg)] text-muted-foreground",
  merging: "border-[color-mix(in_srgb,var(--slate)_40%,var(--panel))] bg-[var(--slate-soft)] text-[color:var(--slate)]",
  ci: "border-[color-mix(in_srgb,var(--warn)_45%,var(--panel))] bg-warn-soft text-warn",
  approval: "border-brand bg-brand text-brand-foreground font-semibold",
  other: "border-[color-mix(in_srgb,var(--warn)_45%,var(--panel))] bg-warn-soft text-warn",
  moved: "border-[color-mix(in_srgb,var(--warn)_45%,var(--panel))] bg-warn-soft text-warn",
  approved: "border-ok-border bg-ok-soft text-ok",
  hold: "border-[color-mix(in_srgb,var(--slate)_40%,var(--panel))] bg-[var(--slate-soft)] text-[color:var(--slate)]",
  asking: "border-border bg-[var(--chip-bg)] text-muted-foreground",
  blocked: "border-err-border bg-err-soft text-err",
  sent_back: "border-err-border bg-err-soft text-err",
};

type Phase = "idle" | "saving" | "failed";
type Tone = "ok" | "warn" | "err" | "none";

const DOT: Record<Tone, string> = {
  ok: "bg-ok", warn: "bg-warn", err: "bg-err", none: "bg-faint",
};

function Chevron({ open }: { open: boolean }) {
  return (
    <svg aria-hidden viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"
         className={cn("h-3 w-3 flex-none transition-transform duration-(--dur-1) ease-out", !open && "-rotate-90")}>
      <path d="M2 4l4 4 4-4" />
    </svg>
  );
}

function Check({ label, text, tone }: { label: string; text: string; tone: Tone }) {
  return (
    <div className="flex min-w-0 items-baseline gap-2" data-testid={`mq-check-${label.toLowerCase()}`}>
      <dt className="m-0 w-[82px] flex-none font-mono text-[11px] font-medium uppercase leading-[1.6] tracking-[0.06em] text-muted-foreground @max-[559px]/mq:w-[76px] @min-[760px]/mq:w-auto">{label}</dt>
      <dd className="m-0 min-w-0 [overflow-wrap:anywhere]">
        <span aria-hidden className={cn("mr-1.5 inline-block h-2 w-2", DOT[tone])} data-testid="mq-dot" data-tone={tone} />
        <span data-user-text>{text}</span>
      </dd>
    </div>
  );
}

// The API words these lines for people and sends no tone with them, so the dot reads its colour from the
// start of the sentence.
function reviewTone(t: string): Tone {
  if (t.startsWith("Approved") && !t.includes("older")) return "ok";
  if (t.includes("stopped it") || t.includes("asked for changes")) return "err";
  return "warn";
}
function ciTone(t: string): Tone {
  if (t === "Checks passed") return "ok";
  if (t.startsWith("Checks failing")) return "err";
  if (t.startsWith("Checks running")) return "warn";
  return "none";
}
function railTone(t: string): Tone {
  if (t === "Allowed") return "ok";
  if (t.startsWith("Blocked")) return "err";
  return "warn";
}

/** The sentence with its lead (the API's first words) in bold; the lead is red on a row held up. */
function Lead({ text, lead, red }: { text: string; lead: string; red?: boolean }) {
  if (lead && text.startsWith(lead)) {
    return <><b className={cn(red && "text-err")} data-testid="mq-lead-b">{lead}</b>{text.slice(lead.length)}</>;
  }
  return <>{text}</>;
}

/** `text` with the first `phrase` in it turned into `link`; the text unchanged when it holds none. */
function withLink(text: string, phrase: string, link: (words: string) => React.ReactNode): React.ReactNode {
  const at = text.indexOf(phrase);
  if (at < 0) return text;
  return <>{text.slice(0, at)}{link(phrase)}{text.slice(at + phrase.length)}</>;
}

function Row({ row, pid, sample, onChanged }: {
  row: MqRow; pid: string; sample: boolean; onChanged: () => Promise<void>;
}) {
  const [phase, setPhase] = React.useState<Phase>("idle");
  const [choice, setChoice] = React.useState<"yes" | "no">("yes");
  const a = row.answer;
  const ask = a && a.kind === "mine" && a.can_answer && a.item ? a.item.id : null;
  const approveOnly = !!a && a.kind === "withdrawn" && a.can_answer;
  // A new answer on the row (someone answered, the commit moved) makes an old failure stale.
  const answerKey = a ? `${a.kind}|${a.decision ?? ""}|${a.commit}|${a.item?.id ?? ""}` : "";
  React.useEffect(() => { setPhase((p) => (p === "failed" ? "idle" : p)); }, [answerKey]);

  const send = async (which: "yes" | "no", go: () => Promise<unknown>) => {
    setChoice(which);
    setPhase("saving");
    try {
      await (sample ? Promise.resolve() : go());
    } catch {
      setPhase("failed");
      return;
    }
    // The answer is saved. The row stays in "saving" until the page has read the queue again, so it
    // never shows the old question with live buttons between the save and the refresh.
    await onChanged();
    setPhase("idle");
  };
  const saving = phase === "saving";
  const failed = phase === "failed";
  const sentBack = row.state === "sent_back";
  const calls = `/projects/${encodeURIComponent(pid)}/calls`;
  const callsLink = (words: string) => <Link to={calls} className={LINK} data-testid="mq-calls">{words}</Link>;

  // "mine" and a withdrawn question point at the Calls page; so does a blocked pull request that has no
  // answer, because the one question for it is there.
  let tail: React.ReactNode = null;
  let note: React.ReactNode = row.status.note;
  if (a && (a.kind === "mine" || a.kind === "withdrawn")) tail = callsLink("Open it on the Calls page");
  else if (row.state === "blocked" && !a) {
    if (row.status.note.includes("Calls page")) note = withLink(row.status.note, "Calls page", callsLink);
    else tail = <>{callsLink("The question is on the Calls page")}.</>;
  }
  const bar = sentBack || (row.state === "blocked" && !a) ? "bad" : row.needs_you ? "you" : "";
  const redLead = row.state === "blocked" || sentBack;
  const pips = row.bounce ? Array.from({ length: 5 }, (_, i) => i < row.bounce!.count) : [];
  return (
    <li className={cn(
      "relative grid min-w-0 grid-cols-[26px_minmax(0,1fr)_auto] gap-x-3 border-t border-border px-[18px] py-3.5 first:border-t-0",
      "@max-[559px]/mq:grid-cols-[auto_minmax(0,1fr)] @max-[559px]/mq:gap-y-1.5 @max-[559px]/mq:border @max-[559px]/mq:bg-background @max-[559px]/mq:p-3 @max-[559px]/mq:first:border-t",
      bar === "you" && "shadow-[inset_3px_0_0_var(--accent)]",
      bar === "bad" && "shadow-[inset_3px_0_0_var(--err)]",
    )}
        data-testid="mq-row" data-state={row.state} data-answer={a?.kind ?? ""} data-attn={row.needs_you ? "yes" : ""}
        data-bar={bar} data-phase={phase} data-ticket={row.ticket.ref} data-turn={row.turn ?? ""}>
      <span className={cn(
        "col-start-1 row-start-1 grid h-[26px] w-[26px] place-items-center border border-border font-mono text-xs font-semibold",
        sentBack ? "bg-transparent text-muted-foreground" : "bg-[var(--chip-bg)]",
      )} data-testid="mq-position">
        <span aria-hidden>{sentBack ? "↩" : row.position}</span>
        <span className="sr-only">{sentBack ? "Out of line, sent back" : `Position ${row.position}`}</span>
      </span>
      <div className="col-start-2 row-start-1 flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-0.5 @max-[559px]/mq:col-[1/-1] @max-[559px]/mq:row-start-2">
        {row.ticket.ref ? (
          <Link to={`../tickets/${encodeURIComponent(row.ticket.ref)}`} relative="path"
                className={cn(LINK, "font-mono text-[13px] font-medium no-underline")} data-testid="mq-ticket" data-user-text>
            {row.ticket.ref}
          </Link>
        ) : null}
        <span className="min-w-0 font-semibold [overflow-wrap:anywhere]" data-testid="mq-title" data-user-text>{row.ticket.title}</span>
      </div>
      <span data-testid="mq-chip" data-chip={row.chip.key}
            className={cn("col-start-3 row-start-1 self-start whitespace-nowrap border px-2 py-0.5 font-mono text-[11px] font-medium uppercase leading-[1.4] tracking-[0.06em] @max-[559px]/mq:col-start-2 @max-[559px]/mq:justify-self-end", CHIP[row.chip.key])}>
        {row.chip.text}
      </span>

      <div className="col-[2/-1] row-start-2 mt-1 min-w-0 @max-[559px]/mq:col-[1/-1] @max-[559px]/mq:row-start-3 @max-[559px]/mq:mt-0">
        <div className="flex min-w-0 flex-wrap gap-x-2 text-[13px] text-muted-foreground" data-testid="mq-facts">
          <a href={row.pr.url} target="_blank" rel="noopener noreferrer" className={LINK} data-testid="mq-pr"
             aria-label={`Pull request ${row.pr.number} on GitHub, opens in a new tab`}>#{row.pr.number} <span aria-hidden>{"↗"}</span></a>
          {row.turn && <><span aria-hidden>{"·"}</span>
            <span data-testid="mq-turn" className={cn(row.whose === "person" && "font-medium text-foreground")}>
              <StatusDot tone={toneOf("turn", row.turn)} className="mr-1.5 rounded-full" />{row.turn_label}
            </span></>}
          {row.builder && <><span aria-hidden>{"·"}</span><span data-user-text>{row.builder.text}</span></>}
          {row.branch && <><span aria-hidden>{"·"}</span><code className="font-mono text-[0.92em] [overflow-wrap:anywhere]" data-user-text>{row.branch}</code></>}
        </div>

        <dl className="m-0 mt-2 flex min-w-0 flex-col gap-0.5 text-[13px] @min-[760px]/mq:flex-row @min-[760px]/mq:flex-wrap @min-[760px]/mq:gap-x-6" data-testid="mq-checks">
          <Check label="Review" text={row.review} tone={reviewTone(row.review)} />
          <Check label="CI" text={row.ci} tone={ciTone(row.ci)} />
          <Check label="Guardrails" text={row.guardrail} tone={railTone(row.guardrail)} />
        </dl>

        <div className="mt-2.5 min-w-0 text-sm" data-testid="mq-status" role="status">
          {saving ? (
            <>
              <p className="m-0 [overflow-wrap:anywhere]" data-testid="mq-saving">
                <b>Saving your answer.</b> {approveOnly || choice === "yes" ? YES.choice : NO.choice}
                {a?.commit ? <> for commit <code className="font-mono text-[0.92em]" data-user-text>{a.commit}</code></> : null}.
              </p>
              <p className="m-0 mt-0.5 text-[13px] text-muted-foreground">The question stays open until the cell has saved it.</p>
            </>
          ) : failed ? (
            <>
              <p className="m-0 [overflow-wrap:anywhere]" data-testid="mq-failed">
                <b className="text-err">Your answer was not saved.</b> The cell did not answer, so the question is still open on every page.
              </p>
              <p className="m-0 mt-0.5 text-[13px] text-muted-foreground">Nothing was sent to GitHub. Answer again when you are ready.</p>
            </>
          ) : (
            <>
              <p className="m-0 [overflow-wrap:anywhere]" data-testid="mq-lead" data-user-text>
                <Lead text={row.status.text} lead={row.status.lead} red={redLead} />
              </p>
              {(row.status.note || tail) && (
                <p className="m-0 mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="mq-note">
                  {note}{note && tail ? " " : null}{tail}
                </p>
              )}
              {row.bounce && (
                <p className="m-0 mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="mq-bounce">
                  <span aria-hidden className="mr-2 inline-flex gap-[3px]" data-testid="mq-pips">
                    {pips.map((on, i) => (
                      <span key={i} data-on={on ? "yes" : ""}
                            className={cn("inline-block h-2 w-2 border", on ? "border-err bg-err" : "border-muted-foreground")} />
                    ))}
                  </span>
                  {withLink(row.bounce.text, "Calls page", callsLink)}
                </p>
              )}
            </>
          )}
        </div>

        {(ask || approveOnly) && (
          <div className="mt-2.5 flex flex-wrap gap-2" role="group" aria-label={`Answer for ${row.ticket.ref}`} data-testid="mq-actions">
            <Button className="min-h-9 px-4 text-[13px] @max-[559px]/mq:flex-1" disabled={saving} busy={saving && choice === "yes"}
                    onClick={() => send("yes", () => (ask
                      ? answerRow(ask, YES)
                      : request(`/api/factory/merge-queue/${encodeURIComponent(row.id)}/approve`, { method: "POST" })))}
                    data-testid="mq-yes">
              {YES.choice}
            </Button>
            {ask && (
              <Button variant="secondary" className="min-h-9 px-4 text-[13px] @max-[559px]/mq:flex-1" disabled={saving} busy={saving && choice === "no"}
                      onClick={() => send("no", () => answerRow(ask, NO))} data-testid="mq-no">
                {NO.choice}
              </Button>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function Group({ g, pid, sample, rows, hidden, open, onToggle, onChanged }: {
  g: MqGroup; pid: string; sample: boolean; rows: MqRow[]; hidden: number; open: boolean;
  onToggle: () => void; onChanged: () => Promise<void>;
}) {
  const [mergedOpen, setMergedOpen] = React.useState(false);
  return (
    <section className={cn(CARD, "min-w-0")} aria-label={g.title} data-testid="mq-group">
      <div className={cn("grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 bg-[var(--th-bg)] py-2 pl-2.5 pr-[18px] @max-[559px]/mq:pl-1.5 @max-[559px]/mq:pr-3.5", open && "border-b border-border")}>
        <button type="button" onClick={onToggle} aria-expanded={open} data-testid="mq-group-toggle"
                className="flex min-w-0 items-center gap-2 border-0 bg-transparent px-2 py-1 text-left text-sm font-semibold leading-[1.4] text-foreground hover:bg-wash-strong">
          <Chevron open={open} />
          <span className="min-w-0 [overflow-wrap:anywhere]" data-user-text>
            {g.repo} <span className="font-normal text-muted-foreground">into</span> {g.base}
          </span>
        </button>
        <span className="whitespace-nowrap font-mono text-xs font-medium text-muted-foreground" data-testid="mq-group-count">{g.count}</span>
        <p className="col-span-full m-0 pb-0.5 pl-9 pr-2 text-[13px] text-muted-foreground [overflow-wrap:anywhere] @max-[559px]/mq:pl-2" data-testid="mq-group-sub">{g.sub}</p>
      </div>
      {open && rows.length > 0 && (
        <ol className="m-0 block list-none p-0 @max-[559px]/mq:grid @max-[559px]/mq:gap-2.5 @max-[559px]/mq:p-2.5" aria-label={`Merge order for ${g.title}`}>
          {rows.map((r) => <Row key={r.id} row={r} pid={pid} sample={sample} onChanged={onChanged} />)}
        </ol>
      )}
      {open && hidden > 0 && (
        <p className="m-0 border-t border-border px-[18px] py-2.5 text-[13px] text-muted-foreground @max-[559px]/mq:px-3.5" data-testid="mq-hidden">
          {hidden} more in this group {hidden === 1 ? "is" : "are"} behind Show more below.
        </p>
      )}
      {open && g.merged && (
        <div className="border-t border-border" data-testid="mq-merged">
          <button type="button" onClick={() => setMergedOpen(!mergedOpen)} aria-expanded={mergedOpen}
                  className="flex w-full items-center gap-2 border-0 bg-transparent px-[18px] py-2 text-left text-[13px] text-muted-foreground hover:bg-row-hover hover:text-foreground @max-[559px]/mq:px-3.5"
                  data-testid="mq-merged-toggle">
            <Chevron open={mergedOpen} />{g.merged.title}
          </button>
          {mergedOpen && (
            <ul className="m-0 grid list-none gap-1.5 pb-2.5 pl-[38px] pr-[18px] @max-[559px]/mq:pl-9 @max-[559px]/mq:pr-3.5">
              {g.merged.items.map((m) => (
                <li key={m.n} className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-[13px] [overflow-wrap:anywhere]" data-testid="mq-merged-item">
                  <span aria-hidden className="inline-block h-2 w-2 self-center bg-ok" />
                  <Link to={`../tickets/${encodeURIComponent(m.tk)}`} relative="path"
                        className={cn(LINK, "font-mono font-medium no-underline")} data-user-text>{m.tk}</Link>
                  <span data-user-text>{m.title}</span>
                  <span className="sr-only">Pull request </span>
                  <span>#{m.n}</span>
                  <span className="text-muted-foreground">merged {m.when} by <span data-user-text>{m.by}</span></span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

/** The ids of the rows to show: every row that needs the person first (those are never hidden), then the
 *  rest in merge order until the cap. Null when everything shows. */
function visibleIds(d: MergeQueueData, all: boolean): Set<string> | null {
  const rows = d.groups.flatMap((g) => g.rows);
  if (all || rows.length <= CAP) return null;
  const ids = new Set(rows.filter((r) => r.needs_you).map((r) => r.id));
  for (const r of rows) {
    if (ids.size >= CAP) break;
    ids.add(r.id);
  }
  return ids;
}

function Queue({ d, pid, sample, reload }: { d: MergeQueueData; pid: string; sample: boolean; reload: () => Promise<void> }) {
  const total = d.groups.reduce((n, g) => n + g.rows.length, 0);
  const [all, setAll] = React.useState(false);
  const [toggled, setToggled] = React.useState<Record<string, boolean>>({});
  const ids = visibleIds(d, all);
  const shown = ids ? ids.size : total;
  return (
    <>
      <div className="grid gap-4" data-testid="mq-groups">
        {d.groups.map((g) => {
          const key = `${g.repo}\u0000${g.base}`;
          const rows = ids ? g.rows.filter((r) => ids.has(r.id)) : g.rows;
          const open = key in toggled ? toggled[key] : (!ids || rows.length > 0);
          return (
            <Group key={key} g={g} pid={pid} sample={sample} rows={rows} hidden={g.rows.length - rows.length} open={open}
                   onToggle={() => setToggled((t) => ({ ...t, [key]: !open }))} onChanged={reload} />
          );
        })}
      </div>
      {total > CAP && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border border-border bg-card px-[18px] py-2.5 text-[13px] text-muted-foreground @max-[559px]/mq:flex-col @max-[559px]/mq:items-start @max-[559px]/mq:px-3.5"
             data-testid="mq-more">
          <span>{all ? `Showing all ${total} pull requests.` : `Showing ${shown} of ${total} pull requests.`}</span>
          <Button variant="secondary" className="min-h-[30px] text-[13px]" onClick={() => setAll(!all)} data-testid="mq-more-toggle">
            {all ? "Show fewer" : `Show ${total - shown} more`}
          </Button>
        </div>
      )}
    </>
  );
}

function Head({ aside }: { aside?: string }) {
  // "3 in line, 1 needs you": the count is the bold part
  const at = aside ? aside.indexOf(", ") : -1;
  return (
    <div className="grid gap-1.5 pt-1">
      <h2 className="m-0 text-[22px] font-bold leading-tight tracking-[-0.02em] @max-[559px]/mq:text-[20px]" id="mq-title" data-testid="mq-heading">Merge queue</h2>
      {aside ? (
        <p className="m-0 text-sm text-muted-foreground" data-testid="mq-aside">
          {at >= 0 ? <><b className="font-semibold text-foreground">{aside.slice(0, at)}</b>{aside.slice(at)}</> : <b className="font-semibold text-foreground">{aside}</b>}
        </p>
      ) : null}
    </div>
  );
}

/** The footer's sentence names the Pull requests tab: that part is the link that switches to it. */
function FootLine({ text, prsHref }: { text: string; prsHref: string }) {
  return <>{withLink(text, "Pull requests tab", (w) => <Link to={prsHref} replace className={LINK} data-testid="mq-foot-prs">{w}</Link>)}</>;
}

export function MergeQueuePanel({ id, data, error, reload, prsHref, sample }: {
  id: string;
  data: MergeQueueData | undefined;
  error: string | undefined;
  /** Reads the queue again and answers once the new read has landed (or failed). */
  reload: () => Promise<void>;
  /** The address of the Pull requests tab. */
  prsHref: string;
  /** A sample shows static data, so an answer on a row does not call the cell. */
  sample: boolean;
}) {
  if (!data) {
    return (
      <section className="@container/mq grid min-w-0 gap-4" data-testid="mq-wait" aria-labelledby="mq-title">
        <Head />
        {error ? (
          <Banner tone="error" action={<Button variant="secondary" onClick={() => void reload()}>Try again</Button>} data-testid="mq-error">
            <b>The merge queue did not load.</b>{" "}
            <span className="text-foreground">The cell did not answer the read, so Tares cannot say what is waiting. The pull requests are not affected.</span>
          </Banner>
        ) : (
          <div role="status" aria-busy="true" aria-label="Loading the merge queue" className="grid gap-4" data-testid="mq-loading">
            <Skeleton className="h-40" />
            <Skeleton className="h-40" />
          </div>
        )}
      </section>
    );
  }
  return (
    <section className="@container/mq grid min-w-0 gap-4" data-testid="mq-page" aria-labelledby="mq-title">
      <Head aside={data.aside} />
      <Collapse>{error && (
        <Banner tone="warn" data-testid="mq-stale">
          <b>The merge queue could not be refreshed.</b> <span className="text-foreground">What shows is from the last time it loaded.</span>
        </Banner>
      )}</Collapse>
      {data.intro && <p className="m-0 max-w-[760px] text-[13px] text-muted-foreground" data-testid="mq-intro">{data.intro}</p>}
      {data.groups.length === 0
        ? <p className={cn(CARD, "m-0 px-[18px] py-4 @max-[559px]/mq:px-3.5")} data-testid="mq-empty">{data.empty}</p>
        : <Queue d={data} pid={id} sample={sample} reload={reload} />}
      <div className="grid gap-1 pt-1 text-[13px] text-muted-foreground" data-testid="mq-foot">
        {data.footer.map((f, i) => <p key={i} className="m-0"><FootLine text={f} prsHref={prsHref} /></p>)}
      </div>
    </section>
  );
}
