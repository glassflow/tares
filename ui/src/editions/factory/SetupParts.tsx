import * as React from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/edition";
import { Switch } from "@paper-kit/ui/switch";
import { cn } from "@/lib/utils";
import { CopyCommand, Tick } from "../StartChecklist";
import type { StartStep } from "../StartChecklist.api";
import type { HomeOnboarding, OnboardingMember } from "./home.types";

// The pieces of setting a factory up that /setup draws around its integration steps (TR-1312): the crew's
// choices, the four terminal commands, and the panel that fills in as the crew reports. They draw the
// onboarding block and the terminal steps GET /api/factory/setup carries; each step ticks from the cell,
// so the page only has to read again.

const PANEL = "min-w-0 border border-border bg-card [overflow-wrap:anywhere]";
const MUTED = "text-[13px] text-muted-foreground";
const SAVE_FAILED = "That was not saved. Try again in a moment.";
const MIN_BUILDERS = 1;
const MAX_BUILDERS = 20;

const MERGES = [
  { value: "L3-review", title: "You approve each merge", text: "The crew builds and reviews, then waits for your yes." },
  { value: "L4-ship", title: "The crew merges on its own", text: "Once the reviewer passes it and CI is green." },
] as const;

const MEMBER_WORDS: Record<OnboardingMember["state"], string> = {
  running: "running", quiet: "quiet", stopped: "stopped", starting: "starting", none: "not running",
};

function Choice({ checked, name, value, title, text, onChange, disabled }: {
  checked: boolean; name: string; value: string; title: string; text: string;
  onChange: () => void; disabled?: boolean;
}) {
  return (
    <label className={cn("flex cursor-pointer items-start gap-3 border bg-field px-4 py-3.5",
                         checked ? "border-foreground" : "border-border")}>
      <input type="radio" name={name} value={value} checked={checked} disabled={disabled} onChange={onChange}
             data-testid={`choice-${value}`} className="mt-1 size-4 flex-none accent-[var(--foreground)]" />
      <span className="grid gap-0.5">
        <span className="font-semibold">{title}</span>
        <span className={MUTED}>{text}</span>
      </span>
    </label>
  );
}

function StepMark({ n, done, current }: { n: number; done: boolean; current: boolean }) {
  if (done) return <Tick done />;
  return (
    <span aria-hidden data-testid="start-tick" data-done="false"
          className={cn("mt-0.5 grid size-5 flex-none place-items-center border-[1.5px] font-mono text-[11px] font-semibold",
                        current ? "border-foreground bg-foreground text-background" : "border-muted-foreground text-muted-foreground")}>
      {n}
    </span>
  );
}

function StepShell({ step, n, current, children, onChange }: {
  step: StartStep; n: number; current: boolean; children?: React.ReactNode; onChange?: () => void;
}) {
  return (
    <li data-testid="start-step" data-key={step.key} data-done={step.done} data-current={current}
        className={cn("flex min-w-0 gap-4 border bg-card px-[18px] py-4", current ? "border-foreground" : "border-border")}>
      <StepMark n={n} done={step.done} current={current} />
      <div className="grid min-w-0 flex-1 gap-3">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <h3 className={cn("m-0 text-base", step.done && "text-muted-foreground")}>{step.title}</h3>
            {!step.done && <div className={MUTED}>{step.gets}</div>}
            {step.done && step.detail && <div className="mt-0.5 line-clamp-3 text-[13px]" title={step.detail} data-user-text data-testid="step-detail">{step.detail}</div>}
          </div>
          {step.done && onChange && (
            <Button variant="quiet" className="px-1 underline" onClick={onChange} data-testid="step-change">Change</Button>
          )}
        </div>
        {children}
      </div>
    </li>
  );
}

export type SaveChoices = (c: { autonomy: string; builders_max: number; releaser: boolean }) => Promise<unknown>;

/** How the crew works: who merges, builders at once, releases. Saved together when the person confirms.
 *  A refused save reads as `errorText` gives it, else as the plain "not saved" line. */
export function ChoicesForm({ o, save, onSaved, saveLabel = "Save and continue", errorText }: {
  o: HomeOnboarding; save: SaveChoices; onSaved: () => void; saveLabel?: string;
  errorText?: (e: unknown) => string | undefined;
}) {
  const c = o.choices;
  const [autonomy, setAutonomy] = React.useState<string>(c.autonomy ?? c.defaults.autonomy);
  const [builders, setBuilders] = React.useState<number>(c.builders_max ?? c.defaults.builders_max);
  const [releases, setReleases] = React.useState<boolean>(c.releaser ?? c.defaults.releaser);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  // Letting the crew merge on its own is a bigger step than the default: it is asked about, in
  // plain words, before it can be saved. Cancel goes back to the person's own approval.
  const [asking, setAsking] = React.useState(false);
  const pick = (v: string) => {
    setAutonomy(v);
    setError(null);
    setAsking(v === "L4-ship" && c.autonomy !== "L4-ship");
  };
  const go = () => {
    setBusy(true);
    setError(null);
    save({ autonomy, builders_max: builders, releaser: releases }).then(
      () => { setBusy(false); setAsking(false); onSaved(); },
      (e: unknown) => { setBusy(false); setError(errorText?.(e) ?? SAVE_FAILED); },
    );
  };
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!asking) go();
  };
  return (
    <form onSubmit={submit} className="grid gap-4" data-testid="choices-form">
      <fieldset className="m-0 grid gap-2 border-0 p-0">
        <legend className="mb-2 p-0 text-sm font-semibold">Who merges a finished pull request</legend>
        {MERGES.map((m) => (
          <Choice key={m.value} name="merge" value={m.value} title={m.title} text={m.text} checked={autonomy === m.value}
                  onChange={() => pick(m.value)} />
        ))}
      </fieldset>
      <div className="flex flex-wrap gap-4">
        <div className="flex min-w-0 flex-[1_1_240px] items-center justify-between gap-4 border border-border bg-field px-4 py-3.5">
          <span className="grid gap-0.5">
            <span id="onb-builders" className="font-semibold">Builders at once</span>
            <span className={MUTED}>One per milestone and repo</span>
          </span>
          <span role="group" aria-labelledby="onb-builders" className="flex items-center border border-border">
            <button type="button" aria-label="One builder fewer" disabled={builders <= MIN_BUILDERS} data-testid="builders-less"
                    onClick={() => setBuilders(builders - 1)}
                    className="size-9 border-0 bg-transparent p-0 text-lg text-foreground cursor-pointer disabled:cursor-not-allowed">{"−"}</button>
            <b className="min-w-9 text-center" data-testid="builders-count" aria-live="polite">{builders}</b>
            <button type="button" aria-label="One builder more" disabled={builders >= MAX_BUILDERS} data-testid="builders-more"
                    onClick={() => setBuilders(builders + 1)}
                    className="size-9 border-0 bg-transparent p-0 text-lg text-foreground cursor-pointer disabled:cursor-not-allowed">+</button>
          </span>
        </div>
        <div className="flex min-w-0 flex-[1_1_240px] items-center justify-between gap-4 border border-border bg-field px-4 py-3.5">
          <span className="grid gap-0.5">
            <span className="font-semibold">Releases</span>
            <span className={MUTED}>{releases ? "The crew may ship a finished milestone when you say so." : "Off: merged work waits for you to ship it."}</span>
          </span>
          <Switch aria-label="Releases" checked={releases} onCheckedChange={setReleases} data-testid="toggle-releases" />
        </div>
      </div>
      {error && <div role="alert" data-testid="step-error" className="text-[13px] text-err [overflow-wrap:anywhere]">{error}</div>}
      {asking ? (
        <div role="group" aria-labelledby="onb-merge-ask" data-testid="merge-confirm-box"
             className="grid gap-3 border border-foreground bg-field px-4 py-3.5">
          <div className="grid gap-0.5">
            <span id="onb-merge-ask" className="font-semibold">Let the crew merge on its own?</span>
            <span className={MUTED}>
              Once the reviewer passes a pull request and CI is green, the crew merges it without asking you.
              You can change this here at any time.
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" busy={busy} onClick={go} data-testid="merge-confirm">Yes, merge on its own</Button>
            <Button type="button" variant="quiet" disabled={busy} onClick={() => pick("L3-review")} data-testid="merge-cancel">
              Keep my approval
            </Button>
          </div>
        </div>
      ) : (
        <div>
          <Button type="submit" busy={busy} data-testid="choices-save">{saveLabel}</Button>
        </div>
      )}
    </form>
  );
}

function MemberLine({ name, who, m }: { name: string; who: string; m: OnboardingMember }) {
  const on = m.state === "running";
  return (
    <li className="grid gap-0.5 border-t border-border px-[18px] py-3.5 first:border-t-0" data-testid={`crew-${who}`} data-state={m.state}>
      <div className="flex items-center justify-between gap-3">
        <span className="font-semibold">{name}</span>
        <span className="flex items-center gap-2 text-muted-foreground">
          <span aria-hidden className={cn("size-2 flex-none", on ? "bg-ok" : "border-[1.5px] border-muted-foreground")} />
          <span data-testid={`crew-${who}-state`}>{MEMBER_WORDS[m.state]}</span>
        </span>
      </div>
      <span className="text-xs text-muted-foreground">{who === "orchestrator" ? "The one session you talk to" : "Reviews every pull request to a fixed bar"}</span>
    </li>
  );
}

export function YourCrew({ o }: { o: HomeOnboarding }) {
  const c = o.crew;
  const b = c.builders;
  return (
    <aside aria-label="Your crew" className={cn(PANEL, "flex-[1_1_280px]")} data-testid="your-crew">
      <div className="border-b border-border px-[18px] py-3.5">
        <h2 className="m-0 text-[15px]">Your crew</h2>
        <div className="text-xs text-muted-foreground">Fills in as your machine reports</div>
      </div>
      <ul className="m-0 list-none p-0">
        <li className="flex items-center justify-between gap-3 px-[18px] py-3.5" data-testid="crew-machine">
          <span className="shrink-0 font-semibold">Your machine</span>
          <span className="flex min-w-0 items-center gap-2 text-muted-foreground">
            <span aria-hidden className={cn("size-2 flex-none", c.machine ? "bg-ok" : "border-[1.5px] border-muted-foreground")} />
            <span data-user-text data-testid="crew-machine-state" className="min-w-0 break-all">{c.machine || "Not signed in"}</span>
          </span>
        </li>
        <MemberLine name="Orchestrator" who="orchestrator" m={c.orchestrator} />
        <MemberLine name="Reviewer" who="reviewer" m={c.reviewer} />
        <li className="grid gap-0.5 border-t border-border px-[18px] py-3.5" data-testid="crew-builders">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold">Builders</span>
            <span className="text-muted-foreground" data-testid="crew-builders-state">{b.live > 0 ? `${b.live} working` : "None yet"}</span>
          </div>
          <span className="text-xs text-muted-foreground">Up to {b.max} at once, once a plan is agreed</span>
        </li>
      </ul>
      {c.up && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border px-[18px] py-3 text-[13px]" data-testid="crew-links">
          <Link to="/settings#crew" className="text-foreground underline underline-offset-2">Change in Settings</Link>
          <Link to="/crew" className="text-foreground underline underline-offset-2">Open the Crew page</Link>
        </div>
      )}
    </aside>
  );
}

function TerminalStep({ s, n, current, o, showDone }: {
  s: StartStep; n: number; current: boolean; o: HomeOnboarding; showDone: boolean;
}) {
  const command = s.command ?? s.actions.find((a): a is { label: string; copy: string } => "copy" in a)?.copy;
  const coming = s.key === "crew" && !s.done && (o.crew.orchestrator.state !== "none" || o.crew.reviewer.state !== "none");
  return (
    <StepShell step={s} n={n} current={current}>
      {(!s.done || showDone) && command && <div><CopyCommand command={command} /></div>}
      {coming && (
        <div className="grid gap-1 text-[13px]" data-testid="crew-coming" role="status">
          <span>Orchestrator is {MEMBER_WORDS[o.crew.orchestrator.state]}</span>
          <span>Reviewer is {MEMBER_WORDS[o.crew.reviewer.state]}</span>
        </div>
      )}
      {s.key === "crew" && !s.done && (
        <div className={MUTED} data-testid="crew-doctor">
          Nothing after a minute? Try <code className="font-mono">tares factory doctor</code>.
        </div>
      )}
    </StepShell>
  );
}

/** "Then, in your terminal": what you need, then the four commands, each ticking from the cell. A done
 *  step keeps its command one click away (`showDone`), so /setup can always show how to run the crew. */
/** What the terminal steps need on the machine, said once above them. */
export function Prerequisites() {
  return (
    <p className="m-0 text-[13px] text-muted-foreground" data-testid="prerequisites">
      You need, signed in: Claude Code on a plan with background sessions, and gh with write access to your repos. Also uv, git, jq and curl.
    </p>
  );
}

export function TerminalSteps({ steps, o, start = 1, current, showDone = false }: {
  steps: StartStep[]; o: HomeOnboarding; start?: number; current?: string; showDone?: boolean;
}) {
  return (
    <section aria-label="In your terminal" className="grid gap-3" data-testid="terminal-panel">
      <ol start={start} className="m-0 grid list-none gap-3 p-0">
        {steps.map((s, i) => (
          <TerminalStep key={s.key} s={s} n={start + i} current={s.key === current} o={o} showDone={showDone} />
        ))}
      </ol>
    </section>
  );
}
