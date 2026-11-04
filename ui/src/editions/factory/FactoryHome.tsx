import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, Dialog, EmptyState, ListRow, Panel, ProgressBar, Skeleton, SkeletonRows, StatusDot, Textarea,
  Toaster, useToast, type Tone,
} from "../../components/edition";
import { cn } from "@/lib/utils";
import { ApiError } from "../../api";
import { useEdition } from "../edition";
import { copyText } from "../copyText";
import { useSample } from "../sample";
import { StartChecklist, StartChecklistHidden, StartChecklistLoading } from "../StartChecklist";
import { usePagePolling } from "../usePagePolling";
import {
  allowDesk, answerQuestion, consentGrant, declineGrant, deskReply, finishProject, getHome, handOverAnyway, keepAssumption, signOff,
  type AllowedGrant,
} from "./FactoryHome.api";
import type {
  HomeAction, HomeCrewMember, HomeCrewState, HomeData, HomeHiddenProject, HomeItem, HomeProject, HomeProjectRow, HomeProjectState, HomeVerb, HomeWaitingOn,
} from "./home.types";
import { HOME_SAMPLES } from "./homeSamples";
import { HOME_SAMPLES_TEAM, sampleView } from "./homeSamplesTeam";
import { Detail } from "./HomeDetail";
import { CrewLimit, ReposYouOwn, TeamCrews } from "./TeamSide";
import { HiddenLine, HiddenRow, LeadLine, PrivateLock, ProjectsSwitch, type ProjectsView } from "./TeamProjects";
import { AnswerPanel, DeclineDialog, HandOverDialog, SignOffDialog, type AnswerGiven } from "./WaitingActions";
import { WaitingForYou } from "./WaitingForYou";
import { YouAreWaitingOn } from "./YouAreWaitingOn";

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

/** What the greeting calls the person: their own name, else the name of the key they signed in
 *  with. The cell calls its one unnamed person "You", which is not a name to greet. */
function useFirstName(): string {
  const { credentialName, personName } = useEdition();
  return firstName(personName && personName !== "You" ? personName : credentialName);
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
  keep_assumptions: "Keeping...", allow: "Allowing...", close: "Closing...", agree: "Agreeing...",
  answer: "Sending...", sign_off: "Signing...", decline: "Declining...",
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

/** A cell with more than one person sends these keys; a cell of one never does. */
const isTeam = (d: HomeData): boolean => d.summary !== undefined;

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

// The Team view of a team cell also lists a stub where a private project the person is not on would be;
// Team draws each as a quiet row after the projects, Mine shows the cell's one line about them instead.
const isProject = (r: HomeProjectRow): r is HomeProject => !("hidden" in r);
const isStub = (r: HomeProjectRow): r is HomeHiddenProject => "hidden" in r;

function ProjectRow({ p, team, me }: { p: HomeProject; team?: boolean; me?: string }) {
  const { merged, blocked, total } = p.tickets;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);
  return (
    <ListRow to={`/projects/${encodeURIComponent(p.id)}`} data-testid="project-row"
             className="block! text-foreground! py-3.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {team && p.scope === "private" && <PrivateLock />}
        <span className={cn("min-w-0 font-bold", team && "[overflow-wrap:anywhere]")} data-user-text>{p.name}</span>
        <span className={cn("border px-1.5 py-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.06em]", CHIP_TONES[p.state])}>
          {p.state_label}
        </span>
      </div>
      <div className="mt-0.5 line-clamp-2" title={p.goal} data-user-text>{p.goal}</div>
      {team ? <LeadLine p={p} me={me} /> : p.meta && <div className={cn(MUTED, "mt-0.5")} data-user-text>{p.meta}</div>}
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

/** What a team cell adds to the Projects section: the switch, who the person is, the line about hidden ones. */
interface TeamProjectsProps {
  counts: { mine: number; team: number }; view: ProjectsView; onView?: (v: ProjectsView) => void; me?: string; hidden?: string;
}

const PROJECTS_FIRST = 6;

function Projects({ projects, stubs, finished, team }: {
  projects: HomeProject[]; stubs: HomeHiddenProject[]; finished: number; team?: TeamProjectsProps;
}) {
  const [all, setAll] = React.useState(false);
  const shown = team && !all ? projects.slice(0, PROJECTS_FIRST) : projects;
  const link = projects.length > 0 || finished > 0 ? (
    <Link to="/projects" className="text-xs text-[color:var(--accent-deep)]! underline">All projects, {finished} finished</Link>
  ) : undefined;
  return (
    <section aria-label="Projects" className="min-w-0" data-testid="projects">
      <SectionHead title="Projects" aside={team ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {link}
          <ProjectsSwitch view={team.view} counts={team.counts} onView={(v) => { setAll(false); team.onView?.(v); }} />
        </div>
      ) : link} />
      <div className="border border-border bg-card">
        {projects.length === 0 ? (
          stubs.length > 0 ? null : team && team.view === "mine" ? (
            <EmptyState title="No projects of yours yet." />
          ) : (
            <EmptyState title="No projects yet."
                        action={<Button asChild><Link to="/projects/new">New project</Link></Button>}>
              Start one and hand it to the crew.
            </EmptyState>
          )
        ) : (
          shown.map((p) => <ProjectRow key={p.id} p={p} team={!!team} me={team?.me} />)
        )}
        {team && projects.length > shown.length && (
          <ListRow>
            <Button variant="quiet" className="px-0! text-[color:var(--accent-deep)]!" onClick={() => setAll(true)}>
              Show all {projects.length} projects
            </Button>
          </ListRow>
        )}
        {stubs.map((s, i) => <HiddenRow key={i} stub={s} />)}
        {team?.view === "mine" && team.hidden && <HiddenLine text={team.hidden} />}
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
  const limit = isTeam(data) ? data.crew_limit : undefined;
  return (
    <section aria-label="Your crew" className="min-w-0">
      <SectionHead title="Your crew" />
      <Panel>
        <div className="mb-2.5 text-[13px] text-muted-foreground">{crew.summary}</div>
        {crew.members.map((m) => <Member key={m.station} m={m} />)}
        {limit ? <CrewLimit limit={limit} /> : (
          <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-t border-border pt-3 text-xs">
            <Link to="/settings#crew" className="text-foreground">How far it may go</Link>
            <span className="text-muted-foreground">{crew.how_far.text}</span>
          </div>
        )}
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

export function FactoryHomeView({ data, onAction, onHandOver, rows, notice, panels, hideStart, onStartHidden, showStart, onStartShown, projectsView, onProjectsView }: {
  data: HomeData; onAction?: OnAction; rows?: Record<string, RowState>; notice?: React.ReactNode;
  /** Mine or Team on a team cell's Projects (Mine unless told); the page reads the list again for the other. */
  projectsView?: ProjectsView; onProjectsView?: (v: ProjectsView) => void;
  /** Hand over anyway on a row of You are waiting on. */
  onHandOver?: (entry: HomeWaitingOn) => void;
  /** A panel open under a waiting row, by row id (the answer box on a team home). */
  panels?: Record<string, React.ReactNode>;
  /** What Hide this list and Show it again call; a sample passes ones that touch nothing. */
  hideStart?: () => Promise<unknown>; onStartHidden?: () => void;
  showStart?: () => Promise<unknown>; onStartShown?: () => void;
}) {
  const { personName } = useEdition();
  const name = useFirstName();
  const team = isTeam(data) && data.counts
    ? { counts: data.counts, view: projectsView ?? "mine", onView: onProjectsView, me: personName, hidden: data.hidden_summary }
    : undefined;
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
            <WaitingForYou
              items={data.waiting_for_you}
              renderAction={(a, item) => <ActionButton key={a.label} action={a} item={item} onAction={onAction} state={rows?.[item.id]} />}
              errors={Object.fromEntries(Object.entries(rows ?? {}).map(([k, v]) => [k, v.error]))}
              panels={panels}
            />
          ) : (
            <Waiting items={data.waiting_for_you} onAction={onAction} rows={rows} />
          )}
          {isTeam(data) && <YouAreWaitingOn entries={data.you_are_waiting_on ?? []} onHandOver={onHandOver} />}
          <Projects projects={data.projects.filter(isProject)} stubs={data.projects.filter(isStub)} finished={data.finished_count} team={team} />
        </div>
        <aside className={RIGHT}>
          <CrewCard data={data} />
          {isTeam(data) && <TeamCrews crews={data.crews ?? []} quiet={data.crews_quiet ?? []} />}
          {isTeam(data) && <ReposYouOwn areas={data.areas ?? []} />}
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
  const name = useFirstName();
  return (
    <div className={PAGE} data-testid="factory-home-loading">
      <TopBar name={name} headline={<Skeleton width={320} height={14} className="mt-2" />} />
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

interface Done { item?: string; project?: string; handedOver?: string }

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

const SAMPLE_NOTE = "This is a sample, so nothing was sent.";
const SAMPLE_OPENS = ["answer", "sign_off", "decline"];

/** Everything the buttons of the page do. With `live` off (a sample) Copy works and the answer, sign-off
 *  and decline panels open, but nothing reaches the cell: the page says so. `changed` tells the page what
 *  to hide at once and to read again. */
function useHomeActions(live: boolean, changed: (done?: Done) => void) {
  const { toast } = useToast();
  const [rows, setRows] = React.useState<Record<string, RowState>>({});
  const [allow, setAllow] = React.useState<{ item: HomeItem; desk: string; mayLimit: boolean } | null>(null);
  const [close, setClose] = React.useState<{ item: HomeItem; project: string } | null>(null);
  const [answering, setAnswering] = React.useState<{ item: HomeItem; action: HomeAction } | null>(null);
  const [signing, setSigning] = React.useState<{ item: HomeItem; project: string; repo: string; tickets: string[]; more: number } | null>(null);
  const [declining, setDeclining] = React.useState<{ item: HomeItem; grant: string } | null>(null);
  const [handing, setHanding] = React.useState<{ entry: HomeWaitingOn; busy: boolean; error: string | null } | null>(null);
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
    } else if (a.do === "allow") {
      if (typeof a.desk === "string") setAllow({ item, desk: a.desk, mayLimit: a.may_limit === true });
    } else if (a.do === "close") {
      const project = String(a.project ?? item.project?.id ?? "");
      if (project) setClose({ item, project });
    } else if (a.do === "answer") {
      if (typeof a.desk === "string" || (a.project && a.question)) { clearError(item.id); setAnswering({ item, action: a }); }
    } else if (a.do === "sign_off") {
      const project = String(a.project ?? item.project?.id ?? "");
      const repo = String(a.repo ?? item.repo ?? "");
      if (project && repo) {
        clearError(item.id);
        setSigning({ item, project, repo, tickets: Array.isArray(a.tickets) ? a.tickets.map(String) : [], more: Number(a.more_tickets) || 0 });
      }
    } else if (a.do === "agree") {
      if (typeof a.grant === "string") {
        const grant = a.grant;
        void run(item, "agree", () => consentGrant(grant), "Agreed. The grant starts once everyone it waits for has agreed.", () => {});
      }
    } else if (a.do === "decline") {
      if (typeof a.grant === "string") { clearError(item.id); setDeclining({ item, grant: a.grant }); }
    }
  };

  const handOver = (entry: HomeWaitingOn) => setHanding({ entry, busy: false, error: null });
  const confirmHandOver = async (entry: HomeWaitingOn, why: string) => {
    if (!live) { setHanding(null); toast(SAMPLE_NOTE); return; }   // a sample never calls the cell
    setHanding({ entry, busy: true, error: null });
    try {
      await handOverAnyway(entry.project.id, why);
      toast(`${entry.project.name} is handed over. The crew can start it.`);
      setHanding(null);
      changed({ handedOver: entry.project.id });
    } catch (e) {
      const notLead = e instanceof ApiError && e.status === 403;
      setHanding({ entry, busy: false, error: notLead ? `Only the lead of ${entry.project.name} can hand it over.` : reason(e) });
    }
  };

  const sendAnswer = ({ item, action }: { item: HomeItem; action: HomeAction }, given: AnswerGiven) => {
    const desk = typeof action.desk === "string" ? action.desk : "";
    const call = desk ? () => deskReply(desk, given.words, given.standing, given.choice)
      : () => answerQuestion(String(action.project), String(action.question), given.words);
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
      {signing && (
        <SignOffDialog key={signing.item.id} item={signing.item} repo={signing.repo} tickets={signing.tickets} more={signing.more}
          busy={rows[signing.item.id]?.busy === "sign_off"} error={rows[signing.item.id]?.error ?? null}
          onClose={() => { clearError(signing.item.id); setSigning(null); }}
          onConfirm={() => void run(signing.item, "sign_off", () => signOff(signing.project, signing.repo),
            `Signed off ${signing.repo}.`, () => setSigning(null))} />
      )}
      {declining && (
        <DeclineDialog key={declining.item.id} item={declining.item}
          busy={rows[declining.item.id]?.busy === "decline"} error={rows[declining.item.id]?.error ?? null}
          onClose={() => { clearError(declining.item.id); setDeclining(null); }}
          onConfirm={(why) => void run(declining.item, "decline", () => declineGrant(declining.grant, why),
            "Declined. The grant is revoked.", () => setDeclining(null))} />
      )}
      {handing && (
        <HandOverDialog key={handing.entry.project.id} entry={handing.entry} busy={handing.busy} error={handing.error}
          onClose={() => setHanding(null)} onConfirm={(why) => void confirmHandOver(handing.entry, why)} />
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
  // A refusal shows once: in the dialog while one is open for the row, under the row otherwise.
  const underRows = Object.fromEntries(Object.entries(rows).map(([id, s]) =>
    [id, id === signing?.item.id || id === declining?.item.id ? { ...s, error: undefined } : s]));
  return { rows: underRows, onAction, onHandOver: handOver, dialogs, panels };
}

// ── pages ──────────────────────────────────────────────────────────────────────

const ALL_SAMPLES: Record<string, HomeData> = { ...HOME_SAMPLES, ...HOME_SAMPLES_TEAM };

function SampleHome({ sample }: { sample: string }) {
  const [tries, setTries] = React.useState(0);
  const { rows, onAction, onHandOver, dialogs, panels } = useHomeActions(false, () => {});
  const [view, setView] = React.useState<ProjectsView>("mine");
  let body: React.ReactNode;
  if (sample === "loading") body = <FactoryHomeLoading />;
  else if (sample === "error") {
    body = <FactoryHomeError key={tries} onRetry={() => setTries((n) => n + 1)} />;
  }
  else body = <FactoryHomeView data={sampleView(ALL_SAMPLES[sample], view)} projectsView={view} onProjectsView={setView} onAction={onAction} onHandOver={onHandOver} rows={rows} panels={panels} hideStart={() => Promise.resolve()} showStart={() => Promise.resolve()} />;
  return (
    <>
      {body}
      {dialogs}
      <Toaster />
    </>
  );
}

/** What the page shows at once after a change, until the daemon's own answer catches up. */
interface Overlay { items: Set<string>; projects: Set<string>; handedOver: Set<string>; until: number }

function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function LiveHome() {
  const [view, setView] = React.useState<ProjectsView>();   // unset: what the cell sends with no ?projects=
  const { data, error, reload } = usePagePolling(() => getHome(view), POLL_MS);
  const [readAt, setReadAt] = React.useState<Date>();
  const [overlay, setOverlay] = React.useState<Overlay | null>(null);
  const timer = React.useRef<ReturnType<typeof setTimeout>>();
  React.useEffect(() => { if (data) { setReadAt(new Date()); } }, [data]);
  // the daemon keeps its answer for a few seconds: once that has passed, the next read is the truth
  React.useEffect(() => {
    if (data && overlay && Date.now() >= overlay.until) setOverlay(null);
  }, [data, overlay]);
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const { rows, onAction, onHandOver, dialogs, panels } = useHomeActions(true, (done) => {
    if (done) {
      setOverlay((o) => ({
        items: new Set([...(o?.items ?? []), ...(done.item ? [done.item] : [])]),
        projects: new Set([...(o?.projects ?? []), ...(done.project ? [done.project] : [])]),
        handedOver: new Set([...(o?.handedOver ?? []), ...(done.handedOver ? [done.handedOver] : [])]),
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
      ...(data.you_are_waiting_on ? { you_are_waiting_on: data.you_are_waiting_on.filter((w) => !overlay.handedOver.has(w.project.id)) } : {}),
      projects: data.projects.filter((p) => !isProject(p) || !overlay.projects.has(p.id)),
      finished_count: data.finished_count + data.projects.filter((p) => isProject(p) && overlay.projects.has(p.id)).length,
    } : data;
    const notice = error ? (
      <Banner tone="warn" data-testid="stale-banner">
        Could not refresh. Showing what we had at {hhmm(readAt ?? new Date())}.
      </Banner>
    ) : undefined;
    body = <FactoryHomeView data={shown} onAction={onAction} onHandOver={onHandOver} rows={rows} panels={panels} notice={notice} onStartHidden={reload} onStartShown={reload}
                           projectsView={view} onProjectsView={(v) => { setView(v); reload(); }} />;
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
  if (sample && (sample === "loading" || sample === "error" || Object.prototype.hasOwnProperty.call(ALL_SAMPLES, sample))) {
    return <SampleHome key={sample} sample={sample} />;
  }
  return <LiveHome />;
}
