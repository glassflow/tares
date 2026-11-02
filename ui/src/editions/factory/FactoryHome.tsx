import * as React from "react";
import { Link } from "react-router-dom";

import {
  Banner, Button, EmptyState, ListRow, Panel, ProgressBar, Skeleton, SkeletonRows, StatusDot, Toaster, useToast,
  type Tone,
} from "../../components/edition";
import { cn } from "@/lib/utils";
import Home from "../../pages/Home";
import { useEdition } from "../edition";
import { useSample } from "../sample";
import type {
  HomeAction, HomeCrewMember, HomeCrewState, HomeData, HomeItem, HomeProject, HomeProjectState,
} from "./home.types";
import { HOME_SAMPLES } from "./homeSamples";

// The factory home: what waits for the person, the projects, the crew, the Claude plan and the
// connected tools. FactoryHomeView is pure: it draws the HomeData it is given. The default export
// shows a sample while the sample flag is on, and today's `/` otherwise (the live page is TR-732).

const SHOW_FIRST = 5;
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

function ActionButton({ action, item, onAction }: {
  action: HomeAction; item?: HomeItem; onAction?: (a: HomeAction, item?: HomeItem) => void;
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
  return (
    <Button variant={variant} className={cls} onClick={() => onAction?.(action, item)}>
      {action.label}
    </Button>
  );
}

function WaitingRow({ item, onAction }: { item: HomeItem; onAction?: (a: HomeAction, item?: HomeItem) => void }) {
  // Outline buttons first, the primary one last.
  const actions = [...item.actions].sort((a, b) => Number(a.primary) - Number(b.primary));
  return (
    <ListRow data-testid="waiting-row" className="justify-between gap-y-3">
      <div className="min-w-0 flex-[1_1_280px]">
        <div className={KIND}>{item.label}</div>
        <div className="my-0.5 font-bold" data-user-text>{item.title}</div>
        <div className="line-clamp-3" title={item.detail} data-user-text>{item.detail}</div>
        <div className={cn(MUTED, "mt-0.5")} data-user-text>{item.meta}</div>
      </div>
      {actions.length > 0 && (
        <div className="flex max-w-full flex-none flex-wrap gap-2">
          {actions.map((a) => <ActionButton key={a.label} action={a} item={item} onAction={onAction} />)}
        </div>
      )}
    </ListRow>
  );
}

function Waiting({ items, onAction }: { items: HomeItem[]; onAction?: (a: HomeAction, item?: HomeItem) => void }) {
  const [all, setAll] = React.useState(false);
  const shown = all ? items : items.slice(0, SHOW_FIRST);
  const more = items.length - shown.length;
  return (
    <section aria-label="Waiting for you" className="min-w-0">
      <SectionHead title="Waiting for you" aside={<span className={MUTED}>oldest first</span>} />
      <div className="border border-border bg-card">
        {items.length === 0 ? (
          <EmptyState title="Nothing waits for you." className="py-6" />
        ) : (
          <>
            {shown.map((it) => <WaitingRow key={it.id} item={it} onAction={onAction} />)}
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

export function FactoryHomeView({ data, onAction }: { data: HomeData; onAction?: (a: HomeAction, item?: HomeItem) => void }) {
  const { credentialName } = useEdition();
  const name = firstName(credentialName);
  return (
    <div className={PAGE} data-testid="factory-home">
      <TopBar name={name} headline={<p className="m-0! max-w-[640px] text-[15px] text-muted-foreground" data-testid="home-headline">{data.headline}</p>} />
      <div className={COLS}>
        <div className={LEFT}>
          <Waiting items={data.waiting_for_you} onAction={onAction} />
          <Projects projects={data.projects} finished={data.finished_count} />
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

export default function FactoryHome() {
  const sample = useSample();
  const { toast } = useToast();
  const [tries, setTries] = React.useState(0);
  const known = sample === "loading" || sample === "error" || (sample !== null && Object.prototype.hasOwnProperty.call(HOME_SAMPLES, sample));
  // No sample, or a name we do not know: today's `/`. The live page replaces this in TR-732.
  if (!sample || !known) return <Home />;

  const onAction = (a: HomeAction) => {
    if (a.do !== "copy") return;
    const text = typeof a.text === "string" ? a.text : "";
    navigator.clipboard.writeText(text).then(
      () => toast("Copied. Paste it into your Claude Code session."),
      () => toast("Could not copy. Try again in a moment."),
    );
  };

  let body: React.ReactNode;
  if (sample === "loading") body = <FactoryHomeLoading />;
  else if (sample === "error") body = <FactoryHomeError key={tries} onRetry={() => setTries((n) => n + 1)} />;
  else body = <FactoryHomeView data={HOME_SAMPLES[sample]} onAction={onAction} />;
  return (
    <>
      {body}
      <Toaster />
    </>
  );
}
