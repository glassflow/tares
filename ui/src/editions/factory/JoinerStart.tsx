import * as React from "react";
import { useLocation } from "react-router-dom";

import { Banner, Button, EmptyState, ListRow, Skeleton } from "../../components/edition";
import { cn } from "@/lib/utils";
import { api, ApiError } from "../../api";
import type { CrewSettingField } from "../../types";
import { Slot } from "@/eeSlots";
import { useWho } from "../who";
import { CopyCommand, Tick } from "../StartChecklist";
import { hideStart, type StartStep } from "../StartChecklist.api";
import { saveCrewChoices } from "./FactoryHome.api";
import { factoryPeople } from "./FactoryPeople.api";
import { joinProject } from "./FactoryProject.api";
import { COLS, isTeam, LEFT, MUTED, PAGE, reason, RIGHT, SectionHead } from "./homeShared";
import { InlineIcon } from "./ProjectIcon";
import { ChoicesForm } from "./SetupParts";
import type { HomeData, HomeOnboarding, HomeProject, HomeProjectRow, HomeStart } from "./home.types";
import YourTeam from "./YourTeam";

// Home for a person joining a factory someone else set up (TR-1723): the team (YourTeam), what it already
// set, then the person's own three steps (the crew's choices, three commands on their machine, a project to join). Every
// step ticks from the cell. It stays while steps are open and the person has not hidden them; the usual
// Home is back after that. What waits for the person is passed in and drawn above the steps.

/** `joining` alone stays true for good, so the view also asks that the steps are open and not hidden. */
export const joinerShows = (d: HomeData): boolean => !!d.joiner?.joining && !!d.start?.show;

/** The crew settings a person can hold of their own. Clearing them keeps the person on the team's. */
const TEAM_KEYS: CrewSettingField[] = ["autonomy", "releaser", "builders_max", "challenger"];
const MACHINE_KEYS = ["install", "login", "crew"];
const SETTINGS_HASH = "#settings";
const HIDE_FAILED = "Could not hide the steps. Try again in a moment.";
const TEXT = "text-[13px] text-muted-foreground";

/** A person's name as the page may say it: a cell that never named its first person calls them "Local" or
 *  "You", which is no name. */
function named(name: string | null | undefined): string {
  const n = (name ?? "").trim();
  return ["local", "you"].includes(n.toLowerCase()) ? "" : n;
}

/** The name of the cell's first person: undefined while it is read, "" when there is none to say. */
function useOwnerName(): string | undefined {
  const [name, setName] = React.useState<string>();
  React.useEffect(() => {
    let current = true;
    factoryPeople()
      .then((d) => named(d.people.find((p) => p.id === "local")?.name))
      .catch(() => "")
      .then((n) => { if (current) setName(n); });
    return () => { current = false; };
  }, []);
  return name;
}

function Welcome({ onChanged }: { onChanged: () => void }) {
  const owner = useOwnerName();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(false);
  const hide = () => {
    setBusy(true);
    setError(false);
    // the button stays at work until the next read takes this view away
    hideStart().then(onChanged, () => { setBusy(false); setError(true); });
  };
  return (
    <Banner data-testid="joiner-welcome" action={
      <Button variant="quiet" className="px-1 underline" busy={busy} onClick={hide} data-testid="joiner-hide">Hide these steps</Button>
    }>
      <h1 className="m-0 text-[22px] md:text-[28px]">
        {owner === undefined ? (
          <span className="block py-1" data-testid="joiner-welcome-loading">
            <span className="sr-only">Welcome</span>
            <Skeleton width={280} height={22} className="max-w-full" />
          </span>
        ) : (
          <>Welcome to {owner ? <><span data-user-text>{owner}</span>&rsquo;s</> : <>your team&rsquo;s</>} factory</>
        )}
      </h1>
      <span className="mt-1 block text-muted-foreground">Three steps, and your crew works with the team.</span>
      {error && <span role="alert" className="mt-1 block text-err" data-testid="joiner-hide-error">{HIDE_FAILED}</span>}
    </Banner>
  );
}

function TeamStrip({ team }: { team: HomeStart["team"] }) {
  if (!team?.settings_text && !team?.github) return null;
  return (
    <section aria-label="Already set up by your team" className="min-w-0" data-testid="joiner-team">
      <SectionHead title="Already set up by your team" />
      <div className="grid gap-1 text-sm">
        {team.settings_text && <div data-testid="joiner-team-settings">{team.settings_text}</div>}
        {team.github && <div data-testid="joiner-team-github" data-user-text>{team.github}</div>}
      </div>
    </section>
  );
}

function Step({ id, title, n, of, done, children, innerRef }: {
  id: "settings" | "machine" | "project"; title: string; n: number; of: number; done: boolean; children: React.ReactNode;
  innerRef?: React.Ref<HTMLElement>;
}) {
  return (
    <section ref={innerRef} aria-label={title} className="min-w-0 [overflow-wrap:anywhere]"
             data-testid="joiner-step" data-key={id} data-done={done}>
      <SectionHead title={title} aside={<span className={MUTED}>Step {n} of {of}</span>} />
      {children}
    </section>
  );
}

/** A tick and what it is about. */
function Ticked({ done, children }: { done: boolean; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 items-start gap-4">
      <Tick done={done} />
      <div className="grid min-w-0 flex-1 gap-3">{children}</div>
    </div>
  );
}

/** What a step gets while it is open, and what the cell saw once it is done. */
function Said({ step }: { step: StartStep }) {
  if (!step.done) return <div className={TEXT}>{step.gets}</div>;
  return step.detail ? <div className="text-[13px]" data-user-text>{step.detail}</div> : <div className={TEXT}>Done.</div>;
}

/** The cell's own sentence when it refuses a choice (looser than the team's, or not the person's to set). */
const refusal = (e: unknown): string | undefined =>
  e instanceof ApiError && (e.status === 400 || e.status === 403) ? e.message : undefined;

function SettingsStep({ step, n, of, onboarding, onChanged }: {
  step: StartStep; n: number; of: number; onboarding?: HomeOnboarding; onChanged: () => void;
}) {
  const { hash, key } = useLocation();
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string>();
  const box = React.useRef<HTMLElement>(null);
  // `/start#settings` lands here, on a first load or while the page is already open
  React.useEffect(() => {
    if (hash !== SETTINGS_HASH) return;
    setOpen(true);
    box.current?.scrollIntoView({ block: "start" });
  }, [hash, key]);
  const show = (next: boolean) => { setError(undefined); setOpen(next); };
  const changed = () => { show(false); onChanged(); };
  const takeTeam = () => {
    setBusy(true);
    setError(undefined);
    api.setCrewSettings({ clear: TEAM_KEYS }).then(
      () => { setBusy(false); changed(); },
      (e: unknown) => { setBusy(false); setError(reason(e)); },
    );
  };
  return (
    <Step id="settings" title={step.title} n={n} of={of} done={step.done} innerRef={box}>
      <Ticked done={step.done}>
        <Said step={step} />
        <div className="flex flex-wrap items-center gap-2">
          {(!step.done || open) && (
            // the open form's Save is then the step's main button
            <Button variant={open ? "secondary" : "primary"} busy={busy} onClick={takeTeam} data-testid="joiner-use-team">
              Use the team&rsquo;s choices
            </Button>
          )}
          {open ? (
            <Button variant="quiet" className="px-1 underline" onClick={() => show(false)} data-testid="joiner-close">Close</Button>
          ) : step.done ? (
            <Button variant="quiet" className="px-1 underline" onClick={() => show(true)} data-testid="joiner-change">Change</Button>
          ) : onboarding && (
            <Button variant="secondary" onClick={() => show(true)} data-testid="joiner-own">Choose my own</Button>
          )}
        </div>
        {error && <div role="alert" className="text-[13px] text-err" data-testid="joiner-settings-error">{error}</div>}
        {open && onboarding && (
          <ChoicesForm o={onboarding} save={saveCrewChoices} saveLabel="Save my choices" errorText={refusal} onSaved={changed} />
        )}
      </Ticked>
    </Step>
  );
}

function MachineStep({ steps, n, of }: { steps: StartStep[]; n: number; of: number }) {
  const done = steps.every((s) => s.done);
  return (
    <Step id="machine" title="Set up your machine" n={n} of={of} done={done}>
      {!done && <p className={cn(TEXT, "m-0 mb-3")}>Type these in a terminal on your machine. Each one ticks here once it is done.</p>}
      <ul className="m-0 list-none p-0">
        {steps.map((s) => {
          const command = s.command ?? s.actions.flatMap((a) => ("copy" in a ? [a.copy] : []))[0];
          return (
            <li key={s.key} className="border-t border-border py-3.5 first:border-t-0 first:pt-0"
                data-testid="joiner-command" data-key={s.key} data-done={s.done}>
              <Ticked done={s.done}>
                <div>
                  <div className={cn("font-semibold", s.done && "text-muted-foreground")}>{s.title}</div>
                  <Said step={s} />
                </div>
                {command && <div><CopyCommand command={command} /></div>}
              </Ticked>
            </li>
          );
        })}
      </ul>
    </Step>
  );
}

const askLead = (lead: string) => (lead ? <>Ask <span data-user-text>{lead}</span></> : "Ask its lead");

function ProjectStep({ step, n, of, rows, onChanged }: {
  step: StartStep; n: number; of: number;
  /** The team's projects, a private one the person is not on as a stub. */
  rows: HomeProjectRow[]; onChanged: () => void;
}) {
  const { personId } = useWho();
  const [busy, setBusy] = React.useState<string>();
  const [joined, setJoined] = React.useState<string[]>([]);
  const [errors, setErrors] = React.useState<Record<string, string | undefined>>({});
  const join = (id: string) => {
    setBusy(id);
    setErrors((all) => ({ ...all, [id]: undefined }));
    joinProject(id).then(
      () => { setBusy(undefined); setJoined((all) => [...all, id]); onChanged(); },
      (e: unknown) => { setBusy(undefined); setErrors((all) => ({ ...all, [id]: reason(e) })); },
    );
  };
  const onIt = (p: HomeProject) => joined.includes(p.id)
    || (!!personId && (p.lead?.id === personId || !!p.members?.some((m) => m?.id === personId)));
  const row = (p: HomeProjectRow, i: number) => {
    if ("hidden" in p) {
      return (
        <ListRow key={`hidden:${i}`} className="justify-between px-0" data-testid="joiner-hidden-project">
          <span className="font-semibold">A private project</span>
          <span className={TEXT} data-testid="joiner-ask">{askLead(named(p.lead?.name))}</span>
        </ListRow>
      );
    }
    const lead = named(p.lead?.name);
    return (
      <ListRow key={p.id} className="justify-between px-0" data-testid="joiner-project" data-id={p.id}>
        <span className="grid min-w-0 flex-[1_1_240px] gap-0.5">
          <span className="font-semibold" data-user-text><InlineIcon icon={p.icon} />{p.name}</span>
          {p.goal && <span className={cn(MUTED, "line-clamp-2")} data-user-text>{p.goal}</span>}
          {lead && <span className={MUTED}>Led by <span data-user-text>{lead}</span></span>}
        </span>
        {onIt(p) ? (
          <span className={TEXT} data-testid="joiner-member">You are on it</span>
        ) : p.scope === "private" ? (
          <span className={TEXT} data-testid="joiner-ask">{askLead(lead)}</span>
        ) : (
          <Button variant="secondary" className="h-8 text-[13px]" busy={busy === p.id} disabled={!!busy && busy !== p.id}
                  onClick={() => join(p.id)} aria-label={`Join this project: ${p.name}`} data-testid="joiner-join">
            Join this project
          </Button>
        )}
        {errors[p.id] && <span role="alert" className="basis-full text-[13px] text-err" data-testid="joiner-join-error">{errors[p.id]}</span>}
      </ListRow>
    );
  };
  return (
    <Step id="project" title={step.title} n={n} of={of} done={step.done}>
      <Ticked done={step.done}>
        <Said step={step} />
        {rows.length === 0 ? (
          <EmptyState data-testid="joiner-no-projects">No team project yet. One shows here once someone on the team starts it.</EmptyState>
        ) : (
          <div>{rows.map(row)}</div>
        )}
      </Ticked>
    </Step>
  );
}

export default function JoinerStart({ data, onChanged, notice, waiting }: {
  /** The home as its Team list answers it (`?projects=team`). */
  data: HomeData;
  /** Called after every change the cell took, so the page reads again. */
  onChanged: () => void;
  notice?: React.ReactNode;
  /** The waiting section with its buttons, when something waits for the person. */
  waiting?: React.ReactNode;
}) {
  const steps = data.start?.steps ?? [];
  const step = (key: string) => steps.find((s) => s.key === key);
  const settings = step("settings");
  const project = step("project");
  const machine = MACHINE_KEYS.flatMap((k) => step(k) ?? []);
  const of = [settings, machine.length > 0, project].filter(Boolean).length;
  let n = 0;
  return (
    <div className={PAGE} data-testid="joiner-start">
      <Welcome onChanged={onChanged} />
      {notice}
      <div className={COLS}>
        <div className={LEFT}>
          <YourTeam team={data.joiner?.team} now={Date.parse(data.now)} />
          <TeamStrip team={data.start?.team} />
          {waiting}
          {settings && <SettingsStep step={settings} n={++n} of={of} onboarding={data.onboarding} onChanged={onChanged} />}
          {machine.length > 0 && <MachineStep steps={machine} n={++n} of={of} />}
          {project && <ProjectStep step={project} n={++n} of={of} rows={data.projects} onChanged={onChanged} />}
        </div>
        {isTeam(data) && (
          <aside className={RIGHT}>
            <Slot name="home.teamCrews" crews={data.crews ?? []} quiet={data.crews_quiet ?? []} />
          </aside>
        )}
      </div>
    </div>
  );
}
