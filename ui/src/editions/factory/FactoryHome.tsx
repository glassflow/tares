import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, Dialog, EmptyState, ListRow, ProgressBar, Skeleton, SkeletonRows, StatusDot, Textarea,
  Toaster, useToast, type Tone,
} from "../../components/edition";
import { cn } from "@/lib/utils";
import { ApiError, isLicenceRequired } from "../../api";
import { licenceSentence } from "@/components/LicenceNeeded";
import { useCloud } from "@/cloud";
import { EE_FAILED_WORDS, LICENCE_NO_ANSWER_WORDS, Slot, useEeFailed, useEePending, useEeSamples, useLicenceNoAnswer } from "@/eeSlots";
import { useEdition } from "../edition";
import { copyText } from "../copyText";
import { useSample } from "../sample";
import { StartChecklist, StartChecklistHidden, StartChecklistLoading } from "../StartChecklist";
import { usePagePolling } from "../usePagePolling";
import {
  allowDesk, deskReply, finishProject, getHome, keepAssumption, keepForLater,
  type AllowedGrant,
} from "./FactoryHome.api";
import type {
  HomeAction, HomeCrewMember, HomeCrewState, HomeData, HomeItem, HomeSample, HomeVerb, TeamHomeHandler,
} from "./home.types";
import { HOME_SAMPLES } from "./homeSamples";
import { Detail, DetailLines } from "./HomeDetail";
import { AnswerPanel, type AnswerGiven } from "./WaitingActions";
import { ItemIcon } from "./ProjectIcon";
import { actionName, GroupHead, groupWaiting, OlderFold, QuestionChoices, topItem } from "./waitingGroups";

// The factory home: what waits for the person, the crew, the Claude plan and the connected tools.
// The projects are one click away, on Projects; Home keeps to what needs the person (TR-1275). FactoryHomeView is pure: it draws the HomeData it is given. The default export
// shows a sample while the sample flag is on and the live page (GET /api/home, every 15 seconds)
// otherwise; the buttons that do something are handled there, never in the view.

const SHOW_FIRST = 5;
const POLL_MS = 15000;
const COPIED_MS = 2000;
const CACHE_MS = 5600;   // the daemon keeps its home answer for 5 seconds; read again just after
const GRANT_MAX = 400;
const WARN_AT = 80;

const SECTION_H = "m-0! text-base!";
/** Loading rows on the paper: no inset, as the rows they stand for. */
const FLUSH_ROWS = "[&>div]:px-0!";
const KIND = "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground";
const MUTED = "text-xs text-muted-foreground";
const INK_FILL = "[&>div:first-child]:bg-foreground!";

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

/** What the greeting calls the person: their own name, else the name of the key they signed in
 *  with. The cell calls its one unnamed person "You", which is not a name to greet. */
function useFirstName(): string {
  const { credentialName, personName } = useEdition();
  return firstName(personName && personName !== "You" ? personName : credentialName);
}

function SectionHead({ title, aside }: { title: string; aside?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-t border-border pt-3">
      <h2 className={SECTION_H}>{title}</h2>
      {aside}
    </div>
  );
}

/** What one waiting row is doing: a button at work (`busy`), the sentence of a refusal, or a copy just made. */
export interface RowState { busy?: HomeVerb; error?: string; copied?: boolean }
type OnAction = (a: HomeAction, item?: HomeItem) => void;

const BUSY_LABEL: Partial<Record<HomeVerb, string>> = {
  keep_assumptions: "Keeping...", allow: "Allowing...", close: "Closing...", agree: "Agreeing...",
  answer: "Sending...", choose: "Sending...", decline: "Declining...", later: "Moving...",
};

function ActionButton({ action, item, onAction, state, top }: {
  action: HomeAction; item?: HomeItem; onAction?: OnAction; state?: RowState;
  /** The row is the top decision: its main action is the page's one primary button (TR-1275). */
  top?: boolean;
}) {
  // Row actions are outlined and a row's main one gets the dark outline; only the top row's main
  // action is filled, so the page asks for one thing first.
  const lead = !!top && action.primary;
  const variant = lead ? "primary" : "secondary";
  // An option of a question holds the crew's own words, so it wraps inside the card (TR-1125).
  const fit = action.do === "choose" ? "h-auto min-h-8 max-w-full whitespace-normal py-1! text-left [overflow-wrap:anywhere]" : "h-8";
  const cls = fit + " text-[13px]!" + (action.primary && !lead ? " border-foreground!" : "");
  const named = actionName(action, item);
  const href = action.href;
  if (href) {
    const internal = href.startsWith("/");
    return (
      <Button asChild variant={variant} className={cls} data-primary={lead || undefined}>
        {internal ? <Link to={href} aria-label={named}>{action.label}</Link> : <a href={href} aria-label={named}>{action.label}</a>}
      </Button>
    );
  }
  const working = state?.busy !== undefined;
  const mine = working && state?.busy === action.do;
  const label = mine ? (BUSY_LABEL[action.do as HomeVerb] ?? action.label)
    : action.do === "copy" && state?.copied ? "Copied" : action.label;
  return (
    <Button variant={variant} className={cls} busy={mine} disabled={working && !mine} onClick={() => onAction?.(action, item)}
            aria-label={mine ? undefined : named} data-primary={lead || undefined}>
      {label}
    </Button>
  );
}

function WaitingRow({ item, onAction, state, top }: { item: HomeItem; onAction?: OnAction; state?: RowState; top?: boolean }) {
  // Outline buttons first, the primary one last.
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <ListRow data-testid="waiting-row" className="justify-between gap-y-3 px-0">
      <div className="min-w-0 flex-[1_1_280px]">
        <div className={KIND}>{item.label}</div>
        <div className="my-0.5 font-bold" data-user-text><ItemIcon project={item.project} title={item.title} />{item.title}</div>
        <div className="line-clamp-3" title={item.detail} data-user-text><Detail text={item.detail} /></div>
        <DetailLines lines={item.lines} />
        <div className={cn(MUTED, "mt-0.5")} data-user-text>{item.meta}</div>
        <QuestionChoices item={item} render={(a, it) => <ActionButton key={a.label} action={a} item={it} onAction={onAction} state={state} top={top} />} />
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2">
          {actions.map((a) => <ActionButton key={a.label} action={a} item={item} onAction={onAction} state={state} top={top} />)}
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

/** Stands where "Waiting for you" will be while the enterprise part of the console loads. */
function WaitingPending() {
  return (
    <section aria-label="Waiting for you" className="min-w-0" data-testid="waiting-pending">
      <SectionHead title="Waiting for you" />
      <SkeletonRows className={FLUSH_ROWS} />
    </section>
  );
}

/** A cell with more than one person sends these keys; a cell of one never does. */
const isTeam = (d: HomeData): boolean => d.summary !== undefined;

function Waiting({ items, onAction, rows }: { items: HomeItem[]; onAction?: OnAction; rows?: Record<string, RowState> }) {
  const [all, setAll] = React.useState(false);
  const g = groupWaiting(items);
  const rest = all ? g.rest : g.rest.slice(0, SHOW_FIRST);
  const more = g.rest.length - rest.length;
  const top = topItem(items)?.id;
  const row = (it: HomeItem) => <WaitingRow key={it.id} item={it} onAction={onAction} state={rows?.[it.id]} top={it.id === top} />;
  return (
    <section aria-label="Waiting for you" className="min-w-0">
      <SectionHead title="Waiting for you" aside={<span className={MUTED}>what holds the crew up first</span>} />
      <div>
        {items.length === 0 ? (
          <EmptyState title="Nothing waits for you." className="py-6" />
        ) : (
          <>
            {g.now.length > 0 && <GroupHead title="The crew is waiting on you" testid="waiting-now" flush />}
            {g.now.map(row)}
            {g.now.length > 0 && rest.length > 0 && <GroupHead title="When you have a moment" testid="waiting-rest" flush />}
            {rest.map(row)}
            {more > 0 && (
              <ListRow className="px-0">
                <Button variant="quiet" className="px-0! text-foreground! underline!" onClick={() => setAll(true)}>
                  {more} more
                </Button>
              </ListRow>
            )}
            <OlderFold items={g.older} className="px-0">{(older) => older.map(row)}</OlderFold>
          </>
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
                         className={cn("mt-1 h-1", INK_FILL)} data-testid={high ? "memory-warn" : "memory"} />
            <div className={cn("mt-0.5 text-[11px]", high ? "font-medium text-foreground" : "text-muted-foreground")}>{m.memory ?? `memory ${m.memory_pct}% full`}</div>
          </>
        )}
      </div>
    </div>
  );
}

function CrewCard({ data }: { data: HomeData }) {
  const { crew } = data;
  const limit = isTeam(data) ? data.crew_limit : undefined;
  const autonomy = (
    <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-t border-border pt-3 text-xs">
      <Link to="/settings#crew" className="text-foreground">Autonomy</Link>
      <span className="text-muted-foreground">{crew.how_far.text}</span>
    </div>
  );
  return (
    <section aria-label="Your crew" className="min-w-0">
      <SectionHead title="Your crew" />
      <div>
        <div className="mb-2.5 text-[13px] text-muted-foreground">{crew.summary}</div>
        {crew.members.map((m) => <Member key={m.station} m={m} />)}
        {limit ? <Slot name="home.crewLimit" limit={limit} fallback={autonomy} /> : autonomy}
      </div>
    </section>
  );
}

function PlanCard({ data }: { data: HomeData }) {
  const { plan } = data;
  return (
    <section aria-label={plan.title} className="min-w-0">
      <SectionHead title={plan.title} />
      {plan.known ? (
        <div>
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
        </div>
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
          <Link key={t.name} to="/settings#tools" data-testid="tool-chip" data-on={t.connected}
                className={cn(chip, "border-border bg-card text-foreground! no-underline! hover:bg-[var(--row-hover)]")}>
            <StatusDot tone={t.connected ? "ready" : "quiet"} label={t.connected ? "connected" : "not connected"} />
            <span>{t.name}</span>
            {t.connected && t.short && <span className="text-muted-foreground" data-user-text>· {t.short}</span>}
          </Link>
        ))}
        {data.tools.some((t) => !t.connected) && (
          <Link to="/settings#tools" data-testid="tool-add" className={cn(chip, "border-dashed border-border text-muted-foreground no-underline!")}>
            Add a tool
          </Link>
        )}
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
        {/* not filled: the one primary on Home is the decision waiting on the person (TR-1275) */}
        <Button asChild variant="secondary"><Link to="/projects/new">New project</Link></Button>
      </div>
    </div>
  );
}

const PAGE = "flex min-w-0 flex-col gap-6 md:gap-8";
const COLS = "flex flex-wrap items-start gap-8";
const LEFT = "flex min-w-0 flex-[2_1_560px] flex-col gap-8";
const RIGHT = "flex min-w-0 flex-[1_1_300px] flex-col gap-6";

const START_TITLE = "Get your factory going";

export function FactoryHomeView({ data, onAction, rows, notice, panels, hideStart, onStartHidden, showStart, onStartShown }: {
  data: HomeData; onAction?: OnAction; rows?: Record<string, RowState>; notice?: React.ReactNode;
  /** Hand over anyway on a row of You are waiting on. */
  /** A panel open under a waiting row, by row id (the answer box on a team home). */
  panels?: Record<string, React.ReactNode>;
  /** What Hide this list and Show it again call; a sample passes ones that touch nothing. */
  hideStart?: () => Promise<unknown>; onStartHidden?: () => void;
  showStart?: () => Promise<unknown>; onStartShown?: () => void;
}) {
  const name = useFirstName();
  const eePending = useEePending();
  const top = topItem(data.waiting_for_you)?.id;
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
          {isTeam(data) ? (
            // the team's lists are enterprise: without it the single-person list shows
            <Slot name="home.waiting" items={data.waiting_for_you}
              renderAction={(a: HomeAction, item: HomeItem) => <ActionButton key={a.label} action={a} item={item} onAction={onAction} state={rows?.[item.id]} top={item.id === top} />}
              errors={Object.fromEntries(Object.entries(rows ?? {}).map(([k, v]) => [k, v.error]))}
              panels={panels}
              // while the ee chunk is on its way, core's rows would flash with the team's buttons on them
              fallback={eePending ? <WaitingPending /> : <Waiting items={data.waiting_for_you} onAction={onAction} rows={rows} />} />
          ) : (
            <Waiting items={data.waiting_for_you} onAction={onAction} rows={rows} />
          )}
          {isTeam(data) && <Slot name="home.waitingOn" entries={data.you_are_waiting_on ?? []} />}
        </div>
        <aside className={RIGHT}>
          <CrewCard data={data} />
          {isTeam(data) && <Slot name="home.teamCrews" crews={data.crews ?? []} quiet={data.crews_quiet ?? []} />}
          {isTeam(data) && <Slot name="home.reposYouOwn" areas={data.areas ?? []} />}
          <PlanCard data={data} />
          <Tools data={data} />
        </aside>
      </div>
    </div>
  );
}

function LoadingBox({ rows }: { rows: number }) {
  return <SkeletonRows count={rows} className={FLUSH_ROWS} />;
}

export function FactoryHomeLoading() {
  const name = useFirstName();
  return (
    <div className={PAGE} data-testid="factory-home-loading">
      <TopBar name={name} headline={<Skeleton width={320} height={14} className="mt-2" />} />
      <div className={COLS}>
        <div className={LEFT}>
          <StartChecklistLoading title={START_TITLE} />
          <section className="min-w-0"><SectionHead title="Waiting for you" /><LoadingBox rows={3} /></section>
        </div>
        <aside className={RIGHT}>
          <section className="min-w-0"><SectionHead title="Your crew" /><LoadingBox rows={3} /></section>
          <section className="min-w-0"><SectionHead title="Claude plan" /><LoadingBox rows={2} /></section>
        </aside>
      </div>
    </div>
  );
}

export function FactoryHomeError({ onRetry }: { onRetry?: () => void }) {
  const name = useFirstName();
  return (
    <div className={PAGE} data-testid="factory-home-error">
      <TopBar name={name} />
      <Banner tone="error" action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}>
        <b>Home did not load.</b> The cell did not answer, so nothing here is current. Try again in a moment.
      </Banner>
    </div>
  );
}

// ── actions ────────────────────────────────────────────────────────────────────

/** The sentence of a refusal, or a plain one when the cell could not be reached. */
function reason(e: unknown): string {
  if (isLicenceRequired(e)) return licenceSentence();   // plain words under the row, never the raw answer
  return e instanceof ApiError && e.message ? e.message : "Could not reach the cell. Try again in a moment.";
}

/** The command a permission item names, in backticks, and the role that wants it ("The builder"). */
function permissionParts(item: HomeItem): { who: string; command: string } {
  const m = /`([\s\S]+)`/.exec(item.detail);
  const station = item.actions?.find((a) => a.do === "allow")?.station;
  // by name, as the Crew page says it: the watch wakes a builder that has finished only when its grant names it
  const who = (typeof station === "string" && station) || item.detail.split(" wants to use")[0] || "The crew";
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

interface Done { item?: string; project?: string; later?: string }

export function AllowDialog({ item, desk, mayLimit, onClose, onDone }: {
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

const SAMPLE_NOTE = "This is a sample, so nothing was sent.";
const SAMPLE_OPENS = ["answer", "decline"];

/** Everything the buttons of the page do. With `live` off (a sample) Copy works and the answer
 *  and decline panels open, but nothing reaches the cell: the page says so. `changed` tells the page what
 *  to hide at once and to read again. */
function useHomeActions(live: boolean, changed: (done?: Done) => void) {
  const { toast } = useToast();
  const [rows, setRows] = React.useState<Record<string, RowState>>({});
  const [allow, setAllow] = React.useState<{ item: HomeItem; desk: string; mayLimit: boolean } | null>(null);
  const [close, setClose] = React.useState<{ item: HomeItem; project: string } | null>(null);
  const [answering, setAnswering] = React.useState<{ item: HomeItem; action: HomeAction } | null>(null);
  // The team's part (agree, decline, the answer to a spec question) is enterprise: its component
  // registers a handler here when it is loaded, and `dialogRow` is the row whose dialog it shows.
  const eeFailed = useEeFailed();
  const licenceNoAnswer = useLicenceNoAnswer();
  useCloud();   // renders again when /health lands: licenceSentence() says "licence" only on a self-hosted cell
  // what a refused team action says: the part did not load, the cell did not say whether it is licensed, or no licence
  const missing = eeFailed ? EE_FAILED_WORDS : licenceNoAnswer ? LICENCE_NO_ANSWER_WORDS : licenceSentence();
  const team = React.useRef<TeamHomeHandler | null>(null);
  const [dialogRow, setDialogRow] = React.useState<string | null>(null);
  const onTeamHandler = React.useCallback((h: TeamHomeHandler | null) => { team.current = h; }, []);
  const working = React.useRef(new Set<string>());
  const timers = React.useRef(new Set<ReturnType<typeof setTimeout>>());
  React.useEffect(() => () => { timers.current.forEach(clearTimeout); }, []);

  const update = (id: string, f: (s: RowState) => RowState | null) => setRows((r) => {
    const next = f(r[id] ?? {});
    const out = { ...r };
    if (next && Object.keys(next).length) out[id] = next; else delete out[id];
    return out;
  });

  /** One call for one row: the button shows work, the row leaves on success and shows the sentence of a refusal. */
  const run = async (item: HomeItem, verb: HomeVerb, call: () => Promise<unknown>, message: string, finish: () => void) => {
    if (working.current.has(item.id)) return;
    if (!live) { toast(SAMPLE_NOTE); finish(); return; }   // a sample never calls the cell
    working.current.add(item.id);
    update(item.id, () => ({ busy: verb }));
    try {
      await call();
      toast(message);
      update(item.id, () => null);
      finish();
      changed({ item: item.id });
    } catch (e) {
      update(item.id, () => ({ error: reason(e) }));
      changed();
    } finally {
      working.current.delete(item.id);
    }
  };
  const clearError = (id: string) => update(id, ({ error: _e, ...s }) => s);

  const onAction = (a: HomeAction, item?: HomeItem) => {
    if (a.do === "copy") {
      const text = typeof a.text === "string" ? a.text : "";
      copyText(text).then(
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
    if (!item || working.current.has(item.id)) return;
    // A sample opens the panels and dialogs, and says nothing was sent when one is confirmed; the rest only say so.
    if (!live && !SAMPLE_OPENS.includes(String(a.do))) { toast(SAMPLE_NOTE); return; }
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
    } else if (a.do === "keep_batch") {
      // a desk item holding a batch of calls: one reply decides them all and closes it. Its label is
      // also the choice, since a batch may offer options of its own and an answer has to say which.
      const desk = typeof a.desk === "string" ? a.desk : "";
      const kept = Array.isArray(a.kept) ? a.kept.map(String) : [];
      if (desk) void run(item, "keep_batch", () => deskReply(desk, a.label, false, a.label, kept), "Kept. The crew carries on.", () => {});
    } else if (a.do === "allow") {
      if (typeof a.desk === "string") setAllow({ item, desk: a.desk, mayLimit: a.may_limit === true });
    } else if (a.do === "close") {
      const project = String(a.project ?? item.project?.id ?? "");
      if (project) setClose({ item, project });
    } else if (a.do === "choose") {
      // one of the question's own options, sent as the answer from the console (TR-1021)
      const desk = typeof a.desk === "string" ? a.desk : "";
      const choice = typeof a.choice === "string" ? a.choice : "";
      const letter = typeof a.letter === "string" ? a.letter : choice;
      if (desk && choice) {
        void run(item, "choose", () => deskReply(desk, letter, false, choice),
          "Answered. It is on its way back to whoever asked.", () => {});
      }
    } else if (a.do === "answer") {
      // a spec question is the team's part: without it there is no box to open, so the row says what is missing
      if (live && typeof a.desk !== "string" && a.project && a.question && !team.current) {
        update(item.id, () => ({ error: missing }));
      } else if (typeof a.desk === "string" || (a.project && a.question)) { clearError(item.id); setAnswering({ item, action: a }); }
    } else if (a.do === "later") {
      const project = String(a.project ?? item.project?.id ?? "");
      if (!project) return;
      const name = item.project?.name ?? item.title;
      working.current.add(item.id);
      update(item.id, () => ({ busy: "later" }));
      void (async () => {
        try {
          await keepForLater(project);
          toast(`${name} is kept for later. It is under Later in All projects.`);
          update(item.id, () => null);
          changed({ item: item.id, later: project });
        } catch (e) {
          update(item.id, () => ({ error: reason(e) }));
          changed();
        } finally {
          working.current.delete(item.id);
        }
      })();
    } else if (a.do === "agree" || a.do === "decline") {
      // without the enterprise part the buttons say what is missing, as any refused call does
      if (!team.current?.act(a, item)) {
        // a sample sends nothing whatever the licence says; a live row says what is missing
        if (!live) toast(SAMPLE_NOTE);
        else update(item.id, () => ({ error: missing }));
      }
    }
  };

  const sendAnswer = ({ item, action }: { item: HomeItem; action: HomeAction }, given: AnswerGiven) => {
    const desk = typeof action.desk === "string" ? action.desk : "";
    const call = desk ? () => deskReply(desk, given.words, given.standing, given.choice)
      : () => {
          if (!team.current) return Promise.reject(new ApiError(missing, eeFailed || licenceNoAnswer ? 503 : 402));
          return team.current.answer(String(action.project), String(action.question), given.words);
        };
    void run(item, "answer", call, "Answered. It is on its way back to whoever asked.", () => setAnswering(null));
  };
  const closeAnswer = () => { if (answering) clearError(answering.item.id); setAnswering(null); };
  const panels: Record<string, React.ReactNode> = answering ? {
    [answering.item.id]: (
      <AnswerPanel key={answering.item.id} item={answering.item} action={answering.action}
        busy={rows[answering.item.id]?.busy === "answer"} onSend={(g) => sendAnswer(answering, g)} onCancel={closeAnswer} />
    ),
  } : {};

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
      <Slot name="home.teamActions" run={run} clearError={clearError} rows={rows} onHandler={onTeamHandler} onDialog={setDialogRow} />
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
  // A refusal shows once: in the dialog while one is open for the row, under the row otherwise.
  const underRows = Object.fromEntries(Object.entries(rows).map(([id, s]) =>
    [id, id === dialogRow ? { ...s, error: undefined } : s]));
  return { rows: underRows, onAction, dialogs, panels };
}

// ── pages ──────────────────────────────────────────────────────────────────────

/** The samples by name: core's, and the ones ee registers (the team's) once it has loaded. */
function useAllSamples(): Record<string, HomeSample> {
  const ee = useEeSamples("home");
  return React.useMemo(() => ({ ...HOME_SAMPLES, ...ee }), [ee]);
}

function SampleHome({ sample, all }: { sample: string; all: Record<string, HomeSample> }) {
  const [tries, setTries] = React.useState(0);
  const { rows, onAction, dialogs, panels } = useHomeActions(false, () => {});
  let body: React.ReactNode;
  if (sample === "loading") body = <FactoryHomeLoading />;
  else if (sample === "error") {
    body = <FactoryHomeError key={tries} onRetry={() => setTries((n) => n + 1)} />;
  }
  else body = <FactoryHomeView data={sampleData(all[sample])} onAction={onAction} rows={rows} panels={panels} hideStart={() => Promise.resolve()} showStart={() => Promise.resolve()} />;
  return (
    <>
      {body}
      {dialogs}
      <Toaster />
    </>
  );
}

const sampleData = (s: HomeSample): HomeData => (typeof s === "function" ? s("mine") : s);

/** What the page shows at once after a change, until the daemon's own answer catches up. */
interface Overlay { items: Set<string>; later: Set<string>; until: number }

function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function LiveHome() {
  const { data, error, reload } = usePagePolling(() => getHome(), POLL_MS);
  const [readAt, setReadAt] = React.useState<Date>();
  const [overlay, setOverlay] = React.useState<Overlay | null>(null);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => { if (data) { setReadAt(new Date()); } }, [data]);
  // the daemon keeps its answer for a few seconds: once that has passed, the next read is the truth
  React.useEffect(() => {
    if (data && overlay && Date.now() >= overlay.until) setOverlay(null);
  }, [data, overlay]);
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const { rows, onAction, dialogs, panels } = useHomeActions(true, (done) => {
    if (done) {
      setOverlay((o) => ({
        items: new Set([...(o?.items ?? []), ...(done.item ? [done.item] : [])]),
        later: new Set([...(o?.later ?? []), ...(done.later ? [done.later] : [])]),
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
      ...(data.you_are_waiting_on ? { you_are_waiting_on: data.you_are_waiting_on.filter((w) => !overlay.later.has(w.project.id)) } : {}),
    } : data;
    const notice = error ? (
      <Banner tone="warn" data-testid="stale-banner">
        Could not refresh. Showing what we had at {hhmm(readAt ?? new Date())}.
      </Banner>
    ) : undefined;
    body = <FactoryHomeView data={shown} onAction={onAction} rows={rows} panels={panels} notice={notice} onStartHidden={reload} onStartShown={reload} />;
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
  const all = useAllSamples();
  const eePending = useEePending();
  if (sample && (sample === "loading" || sample === "error" || Object.prototype.hasOwnProperty.call(all, sample))) {
    return <SampleHome key={sample} sample={sample} all={all} />;
  }
  // a team sample is ee's: while the chunk loads, wait for it instead of showing the live home and swapping
  if (sample && eePending) return <FactoryHomeLoading />;
  return <LiveHome />;
}
