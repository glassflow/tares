import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, Dialog, EmptyState, ListRow, Panel, ProgressBar, Skeleton, SkeletonRows, StatusDot, Textarea,
  Toaster, useToast, type Tone,
} from "../../components/edition";
import { cn } from "@/lib/utils";
import { ApiError } from "../../api";
import { useEdition } from "../edition";
import { useSample } from "../sample";
import { StartChecklist, StartChecklistHidden, StartChecklistLoading } from "../StartChecklist";
import { usePagePolling } from "../usePagePolling";
import { allowDesk, finishProject, getHome, keepAssumption, type AllowedGrant } from "./FactoryHome.api";
import type {
  HomeAction, HomeCrewMember, HomeCrewState, HomeData, HomeItem, HomeProject, HomeProjectRow, HomeProjectState, HomeVerb,
} from "./home.types";
import { HOME_SAMPLES } from "./homeSamples";

// The factory home: what waits for the person, the projects, the crew, the Claude plan and the
// connected tools. FactoryHomeView is pure: it draws the HomeData it is given. The default export
// shows a sample while the sample flag is on and the live page (GET /api/home, every 15 seconds)
// otherwise; the buttons that do something are handled there, never in the view.

const SHOW_FIRST = 5;
const POLL_MS = 15000;
const COPIED_MS = 2000;
const CACHE_MS = 5600;   // the daemon keeps its home answer for 5 seconds; read again just after
const GRANT_MAX = 400;
const WARN_AT = 80;

const SECTION_H = "m-0! text-base!";
const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-[color:var(--accent-deep)]";
const MUTED = "text-xs text-muted-foreground";
const INK_FILL = "[&>div:first-child]:bg-foreground!";
const WARN_FILL = "[&>div:first-child]:bg-[var(--kit-accent)]!";

const CHIP_TONES: Record<HomeProjectState, string> = {
  building: "border-[color:var(--ok-border)] bg-[var(--ok-soft)] text-[color:var(--ok)]",
  waiting_on_you: "border-[color:var(--warn)] bg-[var(--warn-soft)] text-[color:var(--accent-deep)]",
  not_started: "border-border bg-muted text-muted-foreground",
  ready_to_close: "border-foreground bg-foreground text-background",
};

function greetingWord(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** The first name of whoever is signed in: "Alex Kim" gives Alex, "alex.kim@x.dev" gives Alex. */
function firstName(who: string | undefined): string {
  const base = (who ?? "").trim();
  if (!base) return "";
  const name = base.includes("@") ? base.split("@")[0] : base;
  const first = name.split(/[\s._-]+/).filter(Boolean)[0] ?? "";
  return first ? first[0].toUpperCase() + first.slice(1) : "";
}

function SectionHead({ title, aside }: { title: string; aside?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className={SECTION_H}>{title}</h2>
      {aside}
    </div>
  );
}

/** What one waiting row is doing: a button at work (`busy`), the sentence of a refusal, or a copy just made. */
export interface RowState { busy?: HomeVerb; error?: string; copied?: boolean }
type OnAction = (a: HomeAction, item?: HomeItem) => void;

const BUSY_LABEL: Partial<Record<HomeVerb, string>> = {
  keep_assumptions: "Keeping...", allow: "Allowing...", close: "Closing...",
};

function ActionButton({ action, item, onAction, state }: {
  action: HomeAction; item?: HomeItem; onAction?: OnAction; state?: RowState;
}) {
  // Row actions are outlined; the main one gets the dark outline, as in the approved mock.
  const variant = "secondary";
  const cls = "h-8 text-[13px]!" + (action.primary ? " border-foreground!" : "");
  const href = action.href;
  if (href) {
    const internal = href.startsWith("/");
    return (
      <Button asChild variant={variant} className={cls}>
        {internal ? <Link to={href}>{action.label}</Link> : <a href={href}>{action.label}</a>}
      </Button>
    );
  }
  const working = state?.busy !== undefined;
  const mine = working && state?.busy === action.do;
  const label = mine ? (BUSY_LABEL[action.do as HomeVerb] ?? action.label)
    : action.do === "copy" && state?.copied ? "Copied" : action.label;
  return (
    <Button variant={variant} className={cls} busy={mine} disabled={working && !mine} onClick={() => onAction?.(action, item)}>
      {label}
    </Button>
  );
}

/** The crew's text puts a command in backticks; the page shows it in a monospace span. */
function Detail({ text }: { text: string }) {
  const parts = text.split(/`([^`]+)`/);
  return (
    <>
      {parts.map((part, i) => (i % 2 === 1
        ? <code key={i} className="bg-muted px-1 font-mono text-[12.5px]" data-words-exempt>{part}</code>
        : part))}
    </>
  );
}

function WaitingRow({ item, onAction, state }: { item: HomeItem; onAction?: OnAction; state?: RowState }) {
  // Outline buttons first, the primary one last.
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <ListRow data-testid="waiting-row" className="justify-between gap-y-3">
      <div className="min-w-0 flex-[1_1_280px]">
        <div className={KIND}>{item.label}</div>
        <div className="my-0.5 font-bold" data-user-text>{item.title}</div>
        <div className="line-clamp-3" title={item.detail} data-user-text><Detail text={item.detail} /></div>
        <div className={cn(MUTED, "mt-0.5")} data-user-text>{item.meta}</div>
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2">
          {actions.map((a) => <ActionButton key={a.label} action={a} item={item} onAction={onAction} state={state} />)}
        </div>
      )}
      {state?.error && (
        <div role="alert" data-testid="row-error" className="basis-full text-[13px] text-[color:var(--err)]" data-words-exempt>
          {state.error}
        </div>
      )}
    </ListRow>
  );
}

function Waiting({ items, onAction, rows }: { items: HomeItem[]; onAction?: OnAction; rows?: Record<string, RowState> }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_FIRST);
  const more = items.length - shown.length;
  return (
    <section aria-label="Waiting for you" className="min-w-0">
      <SectionHead title="Waiting for you" aside={<span className={MUTED}>oldest first, ready to close last</span>} />
      <div className="border border-border bg-card">
        {items.length === 0 ? (
          <EmptyState title="Nothing waits for you." className="py-6" />
        ) : (
          <>
            {shown.map((it) => <WaitingRow key={it.id} item={it} onAction={onAction} state={rows?.[it.id]} />)}
            {more > 0 && (
              <ListRow>
                <Button variant="quiet" className="px-0! text-[color:var(--accent-deep)]!" onClick={() => setAll(true)}>
                  {more} more
                </Button>
              </ListRow>
            )}
          </>
        )}
      </div>
    </section>
  );
}

// The Team view of a team cell also lists a stub where a private project the person is not on would be.
const isProject = (r: HomeProjectRow): r is HomeProject => !("hidden" in r);

function ProjectRow({ p }: { p: HomeProject }) {
  const { merged, blocked, total } = p.tickets;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);
  return (
    <ListRow to={`/projects/${encodeURIComponent(p.id)}`} data-testid="project-row"
             className="block! text-foreground! py-3.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 font-bold" data-user-text>{p.name}</span>
        <span className={cn("border px-1.5 py-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.06em]", CHIP_TONES[p.state])}>
          {p.state_label}
        </span>
      </div>
      <div className="mt-0.5 line-clamp-2" title={p.goal} data-user-text>{p.goal}</div>
      {p.meta && <div className={cn(MUTED, "mt-0.5")} data-user-text>{p.meta}</div>}
      <ProgressBar value={pct(merged)} blocked={pct(blocked)} aria-label="Tickets merged"
        className="my-2 [&>div:first-child]:bg-foreground! [&_[aria-hidden]]:bg-[repeating-linear-gradient(135deg,var(--kit-accent)_0_3px,transparent_3px_6px)]!" />
      <div className={cn(MUTED, "flex flex-wrap gap-x-3")}>
        <span>{total > 0 ? `${merged} of ${total} tickets merged` : "No tickets yet"}</span>
        {blocked > 0 && <span className="text-[color:var(--err)]">{blocked} blocked</span>}
      </div>
      {p.milestone && <div className={cn(MUTED, "mt-0.5")} data-user-text>{p.milestone}</div>}
    </ListRow>
  );
}

function Projects({ projects, finished }: { projects: HomeProject[]; finished: number }) {
  return (
    <section aria-label="Projects" className="min-w-0">
      <SectionHead title="Projects" aside={projects.length > 0 || finished > 0 ? (
        <Link to="/projects" className="text-xs text-[color:var(--accent-deep)]! underline">All projects, {finished} finished</Link>
      ) : undefined} />
      <div className="border border-border bg-card">
        {projects.length === 0 ? (
          <EmptyState title="No projects yet."
                      action={<Button asChild><Link to="/projects/new">New project</Link></Button>}>
            Start one and hand it to the crew.
          </EmptyState>
        ) : (
          projects.map((p) => <ProjectRow key={p.id} p={p} />)
        )}
      </div>
    </section>
  );
}

const MEMBER_TONE: Record<HomeCrewState, Tone> = {
  working: "working", idle: "ready", waiting: "waiting", "not heard": "quiet", ended: "quiet",
};

function stateLine(m: HomeCrewMember): string {
  if (m.state === "not heard") return m.since ? `Not heard from in ${m.since.replace(/ ago$/, "")}` : "Not heard from";
  const word = m.state[0].toUpperCase() + m.state.slice(1);
  return m.since ? `${word}, ${m.since}` : word;
}

function Member({ m }: { m: HomeCrewMember }) {
  const high = m.memory_pct !== null && m.memory_pct >= WARN_AT;
  return (
    <div data-testid="crew-member" className="grid grid-cols-[8px_1fr] gap-x-2.5 border-t border-border py-2.5 first:border-t-0 first:pt-0">
      <StatusDot tone={MEMBER_TONE[m.state]} label={m.state}
                 className={cn("mt-[5px]", m.state === "working" && "bg-[var(--accent)]")} />
      <div className="min-w-0">
        <div className="font-semibold" data-user-text>{m.name}</div>
        <div className={MUTED}>{stateLine(m)}</div>
        {m.doing && <div className={MUTED} data-user-text>{m.doing}</div>}
        {m.memory_pct !== null && (
          <>
            <ProgressBar value={m.memory_pct} aria-label={`Memory of ${m.name}`}
                         className={cn("mt-1 h-1", high ? WARN_FILL : INK_FILL)} data-testid={high ? "memory-warn" : "memory"} />
            <div className="mt-0.5 text-[11px] text-muted-foreground">{m.memory ?? `memory ${m.memory_pct}% full`}</div>
          </>
        )}
      </div>
    </div>
  );
}

function CrewCard({ data }: { data: HomeData }) {
  const { crew } = data;
  return (
    <section aria-label="Your crew" className="min-w-0">
      <SectionHead title="Your crew" />
      <Panel>
        <div className="mb-2.5 text-[13px] text-muted-foreground">{crew.summary}</div>
        {crew.members.map((m) => <Member key={m.station} m={m} />)}
        <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-t border-border pt-3 text-xs">
          <Link to="/settings#crew" className="text-foreground">How far it may go</Link>
          <span className="text-muted-foreground">{crew.how_far.text}</span>
        </div>
      </Panel>
    </section>
  );
}

function PlanCard({ data }: { data: HomeData }) {
  const { plan } = data;
  return (
    <section aria-label="Your Claude plan" className="min-w-0">
      <SectionHead title="Your Claude plan" />
      {plan.known ? (
        <Panel>
          {plan.limits.map((l) => (
            <div key={l.kind} className="mb-3 last:mb-0">
              <div className="flex flex-wrap justify-between gap-x-3 text-xs">
                <span>{l.label}</span>
                <span className="text-muted-foreground">{l.text}</span>
              </div>
              <ProgressBar value={l.pct} aria-label={l.label} className={cn("mt-1 h-1", INK_FILL)} />
            </div>
          ))}
          {plan.share_text && (
            <div className={cn(MUTED, "mt-3 border-t border-border pt-3")} data-user-text>{plan.share_text}</div>
          )}
        </Panel>
      ) : (
        <p className="m-0 text-sm text-muted-foreground">Plan use shows once the crew reports it.</p>
      )}
    </section>
  );
}

function Tools({ data }: { data: HomeData }) {
  const chip = "inline-flex items-center gap-1.5 border px-2.5 py-1.5 text-[13px]";
  return (
    <section aria-label="Connected" className="min-w-0">
      <SectionHead title="Connected" />
      <div className="flex flex-wrap gap-2">
        {data.tools.map((t) => (
          <span key={t.name} className={cn(chip, "border-border bg-card")}>
            <StatusDot tone={t.connected ? "ready" : "quiet"} label={t.connected ? "connected" : "not connected"} />
            {t.name}
          </span>
        ))}
        <Link to="/settings#tools" className={cn(chip, "border-dashed border-border text-muted-foreground no-underline!")}>
          Add a tool
        </Link>
      </div>
    </section>
  );
}

function TopBar({ name, headline }: { name: string; headline?: React.ReactNode }) {
  const hello = greetingWord(new Date().getHours()) + (name ? `, ${name}` : "");
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <h1 className="mb-1.5! text-[22px]! md:text-[28px]!">{hello}</h1>
        {headline}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="secondary"><Link to="/crew">Talk to the crew</Link></Button>
        <Button asChild><Link to="/projects/new">New project</Link></Button>
      </div>
    </div>
  );
}

const PAGE = "flex min-w-0 flex-col gap-6 md:gap-8";
const COLS = "flex flex-wrap items-start gap-8";
const LEFT = "flex min-w-0 flex-[2_1_560px] flex-col gap-8";
const RIGHT = "flex min-w-0 flex-[1_1_300px] flex-col gap-6";

const START_TITLE = "Get your factory going";

export function FactoryHomeView({ data, onAction, rows, notice, hideStart, onStartHidden, showStart, onStartShown }: {
  data: HomeData; onAction?: OnAction; rows?: Record<string, RowState>; notice?: React.ReactNode;
  /** What Hide this list and Show it again call; a sample passes ones that touch nothing. */
  hideStart?: () => Promise<unknown>; onStartHidden?: () => void;
  showStart?: () => Promise<unknown>; onStartShown?: () => void;
}) {
  const { credentialName } = useEdition();
  const name = firstName(credentialName);
  return (
    <div className={PAGE} data-testid="factory-home">
      <TopBar name={name} headline={<p className="m-0! max-w-[640px] text-[15px] text-muted-foreground" data-testid="home-headline">{data.headline}</p>} />
      {notice}
      <div className={COLS}>
        <div className={LEFT}>
          {data.start?.show && (
            <StartChecklist block={data.start} title={START_TITLE} hide={hideStart} onHidden={onStartHidden} />
          )}
          {data.start && (
            <StartChecklistHidden block={data.start} title={START_TITLE} show={showStart} onShown={onStartShown} />
          )}
          <Waiting items={data.waiting_for_you} onAction={onAction} rows={rows} />
          <Projects projects={data.projects.filter(isProject)} finished={data.finished_count} />
        </div>
        <aside className={RIGHT}>
          <CrewCard data={data} />
          <PlanCard data={data} />
          <Tools data={data} />
        </aside>
      </div>
    </div>
  );
}

function LoadingBox({ rows }: { rows: number }) {
  return <div className="border border-border bg-card"><SkeletonRows count={rows} /></div>;
}

export function FactoryHomeLoading() {
  const { credentialName } = useEdition();
  return (
    <div className={PAGE} data-testid="factory-home-loading">
      <TopBar name={firstName(credentialName)} headline={<Skeleton width={320} height={14} className="mt-2" />} />
      <div className={COLS}>
        <div className={LEFT}>
          <StartChecklistLoading title={START_TITLE} />
          <section className="min-w-0"><SectionHead title="Waiting for you" /><LoadingBox rows={3} /></section>
          <section className="min-w-0"><SectionHead title="Projects" /><LoadingBox rows={3} /></section>
        </div>
        <aside className={RIGHT}>
          <section className="min-w-0"><SectionHead title="Your crew" /><LoadingBox rows={3} /></section>
          <section className="min-w-0"><SectionHead title="Your Claude plan" /><LoadingBox rows={2} /></section>
        </aside>
      </div>
    </div>
  );
}

export function FactoryHomeError({ onRetry }: { onRetry?: () => void }) {
  const { credentialName } = useEdition();
  return (
    <div className={PAGE} data-testid="factory-home-error">
      <TopBar name={firstName(credentialName)} />
      <Banner tone="error" action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}>
        <b>Home did not load.</b> The cell did not answer, so nothing here is current. Try again in a moment.
      </Banner>
    </div>
  );
}

// ── actions ────────────────────────────────────────────────────────────────────

/** The sentence of a refusal, or a plain one when the cell could not be reached. */
function reason(e: unknown): string {
  return e instanceof ApiError && e.message ? e.message : "Could not reach the cell. Try again in a moment.";
}

/** The command a permission item names, in backticks, and the role that wants it ("The builder"). */
function permissionParts(item: HomeItem): { who: string; command: string } {
  const m = /`([\s\S]+)`/.exec(item.detail);
  const who = item.detail.split(" wants to use")[0] || "The crew";
  return { who, command: m ? m[1] : "" };
}

/** The grant as the box first offers it: who, the command and where. A command split over lines
 *  reads as one; one too long for a grant is cut at a word, so the grant covers how it starts. */
function grantWords(who: string, command: string, where: string): string {
  const cmd = command.split("\n").map((l) => l.trim().replace(/\s*\\$/, "")).filter(Boolean).join(" ");
  // "run" is the verb for a command here, as on the Crew page, not the crew's own word for a pass
  const say = (c: string) => `${who} may run \`${c}\` ${where}`; // words-ok
  const room = GRANT_MAX - say("").length;
  if (cmd.length <= room) return say(cmd);
  const cut = cmd.slice(0, Math.max(room - 4, 0));
  return say(`${cut.slice(0, Math.max(cut.lastIndexOf(" "), 1))} ...`);
}

interface Done { item?: string; project?: string }

function AllowDialog({ item, desk, mayLimit, onClose, onDone }: {
  item: HomeItem; desk: string; mayLimit: boolean; onClose: () => void; onDone: (r: AllowedGrant) => void;
}) {
  const { who, command } = permissionParts(item);
  const project = item.project;
  // Only a builder's grant may hold in its project alone; the crew serves every project. The cell
  // says which (`may_limit`), by the same rule as the Crew page.
  const either = !!project && mayLimit;
  const [scope, setScope] = React.useState<"project" | "all">(either ? "project" : "all");
  const where = (sc: "project" | "all") => (sc === "project" && project ? `in ${project.name}` : "in every project");
  const [words, setWords] = React.useState(() => grantWords(who, command, where(scope)));
  const [edited, setEdited] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const length = words.split(/\s+/).join(" ").trim().length;
  const pick = (sc: "project" | "all") => {
    setScope(sc);
    if (!edited) setWords(grantWords(who, command, where(sc)));
  };
  const submit = async () => {
    setBusy(true); setErr(null);
    try { onDone(await allowDesk(desk, words, scope)); }
    catch (e) { setErr(reason(e)); setBusy(false); }
  };
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !busy) onClose(); }} data-testid="allow-dialog"
      title="Allow this command?" description="Tares keeps the grant in the words below. Change them if you want it narrower."
      footer={(
        <>
          <Button variant="secondary" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button busy={busy} disabled={!length || length > GRANT_MAX} onClick={submit}>Allow</Button>
        </>
      )}>
      <div className="flex min-w-0 flex-col gap-3">
        <pre className="m-0 max-h-32 overflow-auto whitespace-pre-wrap border border-border bg-muted p-2 font-mono text-[12.5px]" data-words-exempt>
          {command}
        </pre>
        <Textarea label="The grant, kept in your words" rows={3} value={words} disabled={busy} data-testid="allow-words"
          onChange={(e) => { setWords(e.target.value); setEdited(true); }} />
        {length > GRANT_MAX && <p className="m-0 text-[13px] text-[color:var(--err)]">The grant is {length} characters; keep it to {GRANT_MAX}.</p>}
        {either ? (
          <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-1 border-0 p-0" disabled={busy}>
            <label className="flex items-center gap-1.5 text-sm">
              <input type="radio" name="allow-scope" checked={scope === "project"} onChange={() => pick("project")} />
              Only <span data-user-text>{project?.name}</span>
            </label>
            <label className="flex items-center gap-1.5 text-sm">
              <input type="radio" name="allow-scope" checked={scope === "all"} onChange={() => pick("all")} />
              Every project
            </label>
          </fieldset>
        ) : (
          <p className="m-0 text-[13px] text-muted-foreground">
            Kept for every project{project ? `: ${who.toLowerCase()} works for all of them` : ""}.
          </p>
        )}
        {err && <p role="alert" data-testid="dialog-error" className="m-0 text-[13px] text-[color:var(--err)]" data-words-exempt>{err}</p>}
      </div>
    </Dialog>
  );
}

function CloseDialog({ item, project, onClose, onDone }: {
  item: HomeItem; project: string; onClose: () => void; onDone: () => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const name = item.project?.name ?? item.title;
  const submit = async () => {
    setBusy(true); setErr(null);
    try { await finishProject(project); onDone(); }
    catch (e) { setErr(reason(e)); setBusy(false); }
  };
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !busy) onClose(); }} data-testid="close-dialog"
      title={`Close ${name}?`} description="It moves to Done. Handing it to the crew again opens it."
      footer={(
        <>
          <Button variant="secondary" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button busy={busy} onClick={submit} data-testid="close-confirm">Close</Button>
        </>
      )}>
      {err && <p role="alert" data-testid="dialog-error" className="m-0 text-[13px] text-[color:var(--err)]" data-words-exempt>{err}</p>}
    </Dialog>
  );
}

/** Everything the buttons of the page do. With `live` off (a sample) only Copy works, so a sample
 *  never calls the cell. `changed` tells the page what to hide at once and to read again. */
function useHomeActions(live: boolean, changed: (done?: Done) => void) {
  const { toast } = useToast();
  const [rows, setRows] = React.useState<Record<string, RowState>>({});
  const [allow, setAllow] = React.useState<{ item: HomeItem; desk: string; mayLimit: boolean } | null>(null);
  const [close, setClose] = React.useState<{ item: HomeItem; project: string } | null>(null);
  const working = React.useRef(new Set<string>());
  const timers = React.useRef(new Set<ReturnType<typeof setTimeout>>());
  React.useEffect(() => () => { timers.current.forEach(clearTimeout); }, []);

  const update = (id: string, f: (s: RowState) => RowState | null) => setRows((r) => {
    const next = f(r[id] ?? {});
    const out = { ...r };
    if (next && Object.keys(next).length) out[id] = next; else delete out[id];
    return out;
  });

  const onAction = (a: HomeAction, item?: HomeItem) => {
    if (a.do === "copy") {
      const text = typeof a.text === "string" ? a.text : "";
      navigator.clipboard.writeText(text).then(
        () => {
          toast("Copied. Paste it in Claude Code to hand the project over.");
          if (!item) return;
          update(item.id, (s) => ({ ...s, copied: true }));
          const t = setTimeout(() => { timers.current.delete(t); update(item.id, ({ copied: _c, ...s }) => s); }, COPIED_MS);
          timers.current.add(t);
        },
        () => toast("Could not copy. Try again in a moment."),
      );
      return;
    }
    if (!live || !item || working.current.has(item.id)) return;
    if (a.do === "keep_assumptions") {
      const project = String(a.project ?? item.project?.id ?? "");
      const refs = Array.isArray(a.refs) ? a.refs.map(String) : [];
      if (!project || refs.length === 0) return;
      working.current.add(item.id);
      update(item.id, () => ({ busy: "keep_assumptions" }));
      void (async () => {
        try {
          for (const ref of refs) await keepAssumption(project, ref);
          toast(`${refs.length === 1 ? "Kept it" : refs.length === 2 ? "Kept both" : `Kept all ${refs.length}`}. The crew carries on.`);
          update(item.id, () => null);
          changed({ item: item.id });
        } catch (e) {
          update(item.id, () => ({ error: reason(e) }));
          changed();   // some may have been kept: read again
        } finally {
          working.current.delete(item.id);
        }
      })();
    } else if (a.do === "allow") {
      if (typeof a.desk === "string") setAllow({ item, desk: a.desk, mayLimit: a.may_limit === true });
    } else if (a.do === "close") {
      const project = String(a.project ?? item.project?.id ?? "");
      if (project) setClose({ item, project });
    }
  };

  const dialogs = (
    <>
      {allow && (
        <AllowDialog key={allow.item.id} item={allow.item} desk={allow.desk} mayLimit={allow.mayLimit} onClose={() => setAllow(null)}
          onDone={(r) => {
            toast(r.added ? "Allowed. The crew restarts it with the permission once it is between turns."
              : "That grant was already kept, so nothing changed.");
            setAllow(null);
            changed({ item: allow.item.id });
          }} />
      )}
      {close && (
        <CloseDialog key={close.item.id} item={close.item} project={close.project} onClose={() => setClose(null)}
          onDone={() => {
            toast(`${close.item.project?.name ?? close.item.title} is closed. It moved to Done.`);
            setClose(null);
            changed({ item: close.item.id, project: close.project });
          }} />
      )}
    </>
  );
  return { rows, onAction, dialogs };
}

// ── pages ──────────────────────────────────────────────────────────────────────

function SampleHome({ sample }: { sample: string }) {
  const [tries, setTries] = React.useState(0);
  const { rows, onAction, dialogs } = useHomeActions(false, () => {});
  let body: React.ReactNode;
  if (sample === "loading") body = <FactoryHomeLoading />;
  else if (sample === "error") body = <FactoryHomeError key={tries} onRetry={() => setTries((n) => n + 1)} />;
  else body = <FactoryHomeView data={HOME_SAMPLES[sample]} onAction={onAction} rows={rows} hideStart={() => Promise.resolve()} showStart={() => Promise.resolve()} />;
  return (
    <>
      {body}
      {dialogs}
      <Toaster />
    </>
  );
}

/** What the page shows at once after a change, until the daemon's own answer catches up. */
interface Overlay { items: Set<string>; projects: Set<string>; until: number }

function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function LiveHome() {
  const { data, error, reload } = usePagePolling(getHome, POLL_MS);
  const [readAt, setReadAt] = React.useState<Date>();
  const [overlay, setOverlay] = React.useState<Overlay | null>(null);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => { if (data) setReadAt(new Date()); }, [data]);
  // the daemon keeps its answer for a few seconds: once that has passed, the next read is the truth
  React.useEffect(() => {
    if (data && overlay && Date.now() >= overlay.until) setOverlay(null);
  }, [data, overlay]);
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const { rows, onAction, dialogs } = useHomeActions(true, (done) => {
    if (done) {
      setOverlay((o) => ({
        items: new Set([...(o?.items ?? []), ...(done.item ? [done.item] : [])]),
        projects: new Set([...(o?.projects ?? []), ...(done.project ? [done.project] : [])]),
        until: Date.now() + CACHE_MS,
      }));
    }
    reload();
    clearTimeout(timer.current);
    timer.current = setTimeout(reload, CACHE_MS);
  });

  let body: React.ReactNode;
  if (!data) {
    body = error ? <FactoryHomeError onRetry={reload} /> : <FactoryHomeLoading />;
  } else {
    const shown: HomeData = overlay ? {
      ...data,
      waiting_for_you: data.waiting_for_you.filter((i) => !overlay.items.has(i.id)),
      projects: data.projects.filter((p) => !isProject(p) || !overlay.projects.has(p.id)),
      finished_count: data.finished_count + data.projects.filter((p) => isProject(p) && overlay.projects.has(p.id)).length,
    } : data;
    const notice = error ? (
      <Banner tone="warn" data-testid="stale-banner">
        Could not refresh. Showing what we had at {hhmm(readAt ?? new Date())}.
      </Banner>
    ) : undefined;
    body = <FactoryHomeView data={shown} onAction={onAction} rows={rows} notice={notice} onStartHidden={reload} onStartShown={reload} />;
  }
  return (
    <>
      {body}
      {dialogs}
      <Toaster />
    </>
  );
}

export default function FactoryHome() {
  const sample = useSample();
  if (sample && (sample === "loading" || sample === "error" || Object.prototype.hasOwnProperty.call(HOME_SAMPLES, sample))) {
    return <SampleHome sample={sample} />;
  }
  return <LiveHome />;
}
