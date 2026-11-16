import * as React from "react";
import { createPortal } from "react-dom";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { ERROR_BORDER } from "../../components/edition/Field";
import { Check } from "../../components/icons";
import { Checkbox } from "../../components/ui/checkbox";
import { Input } from "../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "../../lib/utils";
import { useSample } from "../sample";
import { useHeaderSlots, useOwnMenu } from "../Shell";
import { usePagePolling } from "../usePagePolling";
import {
  clearProjectPick, makeDefault, removeConnection, saveIntegration, setupRead,
  type SettingValue, type SetupApp, type SetupAvailable, type SetupConnected, type SetupData, type SetupField, type SetupForm,
  type SetupSlot, type SetupStep, type SetupTag,
} from "./FactorySetup.api";
import { isSample, sampleLoad } from "./setupSamples";

// Set up your factory (TR-872): seven steps, a rail on the left, and for the chosen step what is
// connected, what can be connected and the form of the one being connected or changed. Every string
// comes from GET /api/factory/setup; this file words only what happens after a click (a saved
// integration, a failed read). One generic form renderer draws every integration's fields by kind, so
// a new integration needs no code here. A save goes through the routes that already exist.

const POLL_APP_MS = 3000;
// Below 900 px the rail becomes a list under the title. The classes are written out whole, because Tailwind
// reads class names as literal text and cannot see one built from a variable.

const GENERIC_ERROR = "That did not work. Try again in a moment.";
// Words the page writes itself, where the cell gave none: the failed read has no answer to take them from.
const LOAD_ERROR_LEAD = "The setup could not be loaded.";
const LOAD_ERROR_CAUSE = "Tares did not answer, so nothing here is shown. Nothing was changed.";
const FALLBACK_TITLE = "Set up your factory";
const FALLBACK_EXIT = "Exit setup";

const SMALL_BTN = "h-8! px-3! text-[13px]!";
// A button with the dark border of the main action (the mock's plain .btn), a red one for what removes,
// and the same colours as the text of an error. Hover keeps the border, or the secondary variant would reset it.
const DARK = "border-foreground! hover:border-foreground!";
const DANGER_BTN = "border-[color:var(--danger-border)]! text-[color:color-mix(in_srgb,var(--err)_78%,var(--ink))]! hover:border-[color:var(--danger-border)]!";
const OK_TEXT = "text-[color:color-mix(in_srgb,var(--ok)_62%,var(--ink))]";
// Narrow, the buttons of a row share its width, as in the mock.
const ACTS = "flex flex-wrap items-center gap-2.5 max-[559px]:[&>*]:flex-auto";

// ---- tags ----

const TAG_TONE: Record<SetupTag["key"], string> = {
  default: "border-foreground bg-foreground text-background",
  built_in: "border-border bg-muted text-muted-foreground",
  key: "border-border bg-muted text-muted-foreground",
  app: cn("border-[color:var(--ok-border)] bg-[var(--ok-soft)]", OK_TEXT),
  licence_ended: "border-[color:var(--warn)] bg-[var(--warn-soft)] text-foreground",
  not_licensed: "border-[color:var(--warn)] bg-[var(--warn-soft)] text-foreground",
};

function Tag({ tone, children, ...props }: { tone: string; children: React.ReactNode } & React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span className={cn("inline-block whitespace-nowrap border px-1.5 py-px font-mono text-[11px] font-medium uppercase leading-4 tracking-[0.08em]", tone)} {...props}>
      {children}
    </span>
  );
}

// ---- the form: one renderer for every kind of field ----

type Values = Record<string, SettingValue>;

function initialValues(fields: SetupField[]): Values {
  const out: Values = {};
  for (const f of fields) {
    if (f.kind === "bool") out[f.name] = f.value === true;
    else if (f.kind === "mapping") out[f.name] = Object.fromEntries(f.rows.map((r) => [r.key, r.value]));
    else if (f.kind === "secret") out[f.name] = "";
    else out[f.name] = typeof f.value === "string" ? f.value : "";
  }
  return out;
}

const blank = (v: SettingValue | undefined) => typeof v === "string" && !v.trim();

/** The names of the fields that are required and empty. A secret already saved may stay blank. */
function missing(fields: SetupField[], values: Values): string[] {
  return fields.filter((f) => {
    if (!f.required) return false;
    const v = values[f.name];
    if (f.kind === "bool") return false;
    if (f.kind === "secret") return !f.saved && blank(v);
    if (f.kind === "mapping") return f.rows.some((r) => blank((v as Record<string, string>)?.[r.key]));
    return blank(v);
  }).map((f) => f.name);
}

/** What a save sends: text and secrets trimmed (a blank secret keeps the saved one), a real bool, a mapping. */
function settingsOf(fields: SetupField[], values: Values): Record<string, SettingValue> {
  const out: Record<string, SettingValue> = {};
  for (const f of fields) {
    const v = values[f.name];
    if (f.kind === "bool") out[f.name] = v === true;
    else if (f.kind === "mapping") out[f.name] = Object.fromEntries(Object.entries(v as Record<string, string>).map(([k, x]) => [k, x.trim()]));
    else out[f.name] = typeof v === "string" ? v.trim() : "";
  }
  return out;
}

const FIELD_BOX = "h-10! min-w-0";
const BAR = "shadow-[inset_3px_0_0_var(--err)]";

function FieldLabel({ f, htmlFor, as: As = "label" }: { f: SetupField; htmlFor?: string; as?: "label" | "legend" }) {
  return (
    <As {...(As === "label" ? { htmlFor } : {})} className="float-none flex flex-wrap items-baseline gap-x-2 p-0 text-sm font-semibold text-foreground">
      <span>{f.label}</span>
      <span className="text-xs font-normal text-muted-foreground" data-testid="field-requirement">{f.requirement}</span>
    </As>
  );
}

function SettingField({ f, value, error, onChange }: {
  f: SetupField;
  value: SettingValue;
  error: boolean;
  onChange: (v: SettingValue) => void;
}) {
  const base = React.useId();
  const id = `${base}-${f.name}`;
  const helpId = `${id}-help`;
  const errId = `${id}-error`;
  const describedBy = [f.help && helpId, f.kind === "secret" && f.saved && f.saved_note && `${id}-saved`, error && errId].filter(Boolean).join(" ") || undefined;
  const aria = { id, "aria-describedby": describedBy, "aria-invalid": error || undefined, "aria-required": f.required || undefined };
  const help = f.help ? <p id={helpId} className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{f.help}</p> : null;
  const err = error ? <p id={errId} className="m-0 text-[13px] font-semibold text-[color:var(--err)]" data-testid="field-error">{f.error}</p> : null;
  const box = cn(FIELD_BOX, error && cn(ERROR_BORDER, BAR));

  if (f.kind === "bool") {
    return (
      <div className="flex items-start gap-3" data-testid={`field-${f.name}`} data-kind={f.kind}>
        <Checkbox
          id={id}
          checked={value === true}
          onCheckedChange={(c) => onChange(c === true)}
          aria-describedby={describedBy}
          className="mt-0.5"
          data-testid={`input-${f.name}`}
        />
        <div className="grid min-w-0 gap-1">
          <label htmlFor={id} className="cursor-pointer text-sm font-semibold leading-5 text-foreground [overflow-wrap:anywhere]">{f.label}</label>
          {help}
        </div>
      </div>
    );
  }

  if (f.kind === "mapping") {
    const rows = (value as Record<string, string>) ?? {};
    return (
      <fieldset className="m-0 grid min-w-0 gap-2 border-0 p-0" data-testid={`field-${f.name}`} data-kind={f.kind} aria-describedby={describedBy}>
        <FieldLabel f={f} as="legend" />
        {help}
        <div className="grid gap-2">
          {f.rows.map((r) => (
            <div key={r.key} className="grid items-center gap-x-3 gap-y-1 min-[560px]:grid-cols-[140px_minmax(0,1fr)]" data-testid={`row-${r.key}`}>
              <span className="text-sm text-foreground" aria-hidden>{r.label}</span>
              <Input
                type="text"
                value={rows[r.key] ?? ""}
                onChange={(e) => onChange({ ...rows, [r.key]: e.target.value })}
                aria-label={r.aria_label}
                aria-invalid={error && blank(rows[r.key]) ? true : undefined}
                className={cn(FIELD_BOX, error && blank(rows[r.key]) && cn(ERROR_BORDER, BAR))}
                autoComplete="off"
                spellCheck={false}
                data-testid={`input-${f.name}-${r.key}`}
              />
            </div>
          ))}
        </div>
        {err}
      </fieldset>
    );
  }

  let control: React.ReactNode;
  if (f.kind === "select") {
    control = (
      <Select value={typeof value === "string" ? value : ""} onValueChange={(v) => onChange(v)}>
        <SelectTrigger {...aria} className={box} data-testid={`input-${f.name}`}>
          <SelectValue placeholder={f.placeholder || undefined} />
        </SelectTrigger>
        <SelectContent>
          {f.options.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}
        </SelectContent>
      </Select>
    );
  } else {
    const secret = f.kind === "secret";
    // A saved secret says it is saved once, in the note under the box; the placeholder would repeat it.
    const said = secret && f.saved && !!f.saved_note;
    control = (
      <Input
        {...aria}
        type={secret ? "password" : "text"}
        inputMode={f.kind === "url" ? "url" : undefined}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={said ? undefined : f.placeholder || undefined}
        autoComplete={secret ? "new-password" : "off"}
        autoCapitalize="off"
        spellCheck={false}
        className={box}
        data-testid={`input-${f.name}`}
      />
    );
  }

  return (
    <div className="grid min-w-0 gap-1.5" data-testid={`field-${f.name}`} data-kind={f.kind}>
      <FieldLabel f={f} htmlFor={id} />
      {control}
      {f.kind === "secret" && f.saved && f.saved_note && (
        <p id={`${id}-saved`} className="m-0 flex items-center gap-1.5 text-[13px] text-muted-foreground" data-testid="field-saved">
          <Check className="size-3.5 flex-none text-[color:var(--ok)]" aria-hidden />
          <span>{f.saved_note}</span>
        </p>
      )}
      {help}
      {err}
    </div>
  );
}

/** The fields an app connection still asks for: no key and no address, since the app holds the access. */
const appFields = (fields: SetupField[]) => fields.filter((f) => f.kind !== "secret" && f.kind !== "url" && f.name !== "credential");

/** What happens when the app is used: the steps and the note, in a box. */
function AppSteps({ app }: { app: SetupApp }) {
  return (
    <div className="grid gap-2.5 border border-border bg-background p-4" data-testid="app-block">
      <h2 className="m-0! text-sm! font-semibold!">What happens next</h2>
      <ol className="m-0 grid list-decimal gap-1.5 pl-5 text-sm text-foreground">
        {app.steps.map((s) => <li key={s} className="[overflow-wrap:anywhere]">{s}</li>)}
      </ol>
      <p className="m-0 text-[13px] text-foreground [overflow-wrap:anywhere]">{app.note}</p>
    </div>
  );
}

/** While the app window is open the page shows only this: one box that says what it waits for. */
function AppWaiting({ service }: { service: string }) {
  return (
    <div className="border border-border px-3 py-2 text-[13px] [overflow-wrap:anywhere]" role="status" data-testid="app-waiting">
      <span className="mr-2 inline-block size-3 animate-spin rounded-full border-2 border-border border-t-foreground align-[-2px] motion-reduce:animate-none" aria-hidden />
      <b>Waiting for {service}.</b> Finish signing in on the {service} page that opened. This page carries on by itself once you approve the app.
    </div>
  );
}

function FormView({ slot, stepName, form, app, name, title, onBack, onSaved, onPoll }: {
  slot: SetupSlot;
  stepName: string;
  form: SetupForm;
  app: SetupApp | null;
  name: string;
  title: string;
  onBack: () => void;
  onSaved: (text: string) => void;
  onPoll: () => void;
}) {
  const [via, setVia] = React.useState<"app" | "key">(app ? "app" : "key");
  const [waiting, setWaiting] = React.useState(false);
  const [values, setValues] = React.useState<Values>(() => initialValues(form.fields));
  const [bad, setBad] = React.useState<string[]>([]);
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState("");
  const formRef = React.useRef<HTMLFormElement>(null);

  const appMode = !!app && via === "app";
  const done = appMode && app.stage === "done";
  const fields = appMode ? (done && app.repo_step ? appFields(form.fields) : []) : form.fields;
  const waitingNow = waiting && appMode && app.stage === "start";

  // While the app window is open, read the page again every few seconds: the connection shows up on its own.
  // The reader is held in a ref, so the timer is not built again each time the page draws; a hidden tab
  // makes no calls, and reads once as soon as it is visible again.
  const pollRef = React.useRef(onPoll);
  pollRef.current = onPoll;
  React.useEffect(() => {
    if (!waitingNow) return undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    const stop = () => { if (timer !== undefined) { clearInterval(timer); timer = undefined; } };
    const start = () => { stop(); timer = setInterval(() => pollRef.current(), POLL_APP_MS); };
    const onVisible = () => {
      if (document.hidden) { stop(); return; }
      pollRef.current();
      start();
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisible);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisible); };
  }, [waitingNow]);

  const open = () => {
    if (app) window.open(app.url, "_blank", "noopener");
    setWaiting(true);
  };

  const change = (n: string, v: SettingValue) => {
    setValues((s) => ({ ...s, [n]: v }));
    setBad((b) => b.filter((x) => x !== n));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const empty = missing(fields, values);
    setBad(empty);
    setProblem("");
    if (empty.length) {
      const first = formRef.current?.querySelector<HTMLElement>(`[data-testid="field-${empty[0]}"] input, [data-testid="field-${empty[0]}"] button`);
      first?.focus();
      return;
    }
    setBusy(true);
    try {
      await saveIntegration(slot, name, settingsOf(fields, values), form.save_as_default);
      onSaved(form.mode === "edit" ? `${title} integration settings are saved.` : `${title} is connected.`);
    } catch (err) {
      setProblem(err instanceof ApiError || err instanceof Error ? err.message || GENERIC_ERROR : GENERIC_ERROR);
      setBusy(false);
    }
  };

  const hasForm = fields.length > 0;
  const choosing = appMode && !done && !waitingNow;

  return (
    <div className="mx-auto grid w-full max-w-[640px] min-w-0 gap-5" data-testid="setup-form" data-mode={form.mode}>
      <div className="grid gap-1">
        <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-x-1.5 text-[13px] text-muted-foreground">
          <button
            type="button"
            onClick={onBack}
            className="inline! border-0! bg-transparent! p-0! text-[13px]! font-normal! text-muted-foreground! underline! underline-offset-[3px] hover:text-foreground! cursor-pointer"
            data-testid="form-back"
          >
            {stepName}
          </button>
          <span aria-hidden>/</span>
          <span aria-current="page" className="[overflow-wrap:anywhere]" data-testid="form-crumb">{form.title}</span>
        </nav>
        <h1 className="m-0! text-[22px]! min-[900px]:text-[26px]! [overflow-wrap:anywhere]" data-testid="page-title" tabIndex={-1}>{form.title}</h1>
        <p className="m-0 mt-1 text-[15px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="form-lead">{form.lead}</p>
      </div>

      <form ref={formRef} onSubmit={submit} noValidate className="grid min-w-0 gap-[18px] border border-border bg-card p-4 min-[560px]:p-5" data-testid="form-panel">
        {app && !appMode && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" data-testid="app-switch">
            <Button variant="secondary" className={SMALL_BTN} onClick={() => setVia("app")} data-testid="app-use-app">{app.use_app_label}</Button>
          </div>
        )}
        {choosing && <AppSteps app={app} />}
        {waitingNow && <AppWaiting service={app.service} />}
        {fields.map((f) => (
          <SettingField key={f.name} f={f} value={values[f.name]} error={bad.includes(f.name)} onChange={(v) => change(f.name, v)} />
        ))}
        {done && app.done && (
          <div className="border-t border-border pt-4">
            <div
              className={cn("border border-[color:var(--ok-border)] bg-[var(--ok-soft)] px-3 py-2 text-[13px] [overflow-wrap:anywhere]", OK_TEXT)}
              role="status"
              data-testid="app-done"
            >
              {app.done}
            </div>
          </div>
        )}
        {problem && <Banner tone="error" data-testid="form-error"><span data-user-text>{problem}</span></Banner>}
        <div className={ACTS}>
          {waitingNow ? (
            <Button variant="secondary" className={DARK} onClick={open} data-testid="app-open-again">Open {app.service} again</Button>
          ) : choosing ? (
            <Button onClick={open} data-testid="app-open">{app.button}</Button>
          ) : hasForm || !appMode ? (
            <Button type="submit" busy={busy} data-testid="form-save">{form.save_label}</Button>
          ) : (
            <Button type="button" onClick={onBack} data-testid="form-done">{stepName}</Button>
          )}
          <Button variant="secondary" className={DARK} onClick={onBack} data-testid="form-cancel">Cancel</Button>
        </div>
        {choosing && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-4 text-[13px] text-muted-foreground">
            <span>Cannot use the app?</span>
            <Button variant="secondary" className={SMALL_BTN} onClick={() => setVia("key")} data-testid="app-use-key">{app.use_key_label}</Button>
          </div>
        )}
      </form>
    </div>
  );
}

// ---- the step: what is connected, what can be connected ----

function ConnectedRow({ c, slot, busy, asking, onAsk, onAnswer, onAction }: {
  c: SetupConnected;
  slot: SetupSlot;
  busy: boolean;
  asking: boolean;
  onAsk: () => void;
  onAnswer: (yes: boolean) => void;
  onAction: (key: "edit" | "make_default") => void;
}) {
  const keepRef = React.useRef<HTMLButtonElement>(null);
  const removeRef = React.useRef<HTMLButtonElement>(null);
  React.useEffect(() => { if (asking) keepRef.current?.focus(); }, [asking]);
  // The answer No closes the question, and the focus goes back to the button that opened it.
  const keep = () => { onAnswer(false); removeRef.current?.focus(); };
  return (
    <li className="grid gap-3 border-b border-border px-4 py-3.5 last:border-b-0" data-testid={`connected-${c.id}`} data-slot={slot}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-[1_1_320px]">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold [overflow-wrap:anywhere]" data-testid="row-title">{c.title}</span>
            {c.tags.map((t) => <Tag key={t.key} tone={TAG_TONE[t.key]} data-testid={`tag-${t.key}`}>{t.text}</Tag>)}
          </div>
          <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{c.description}</div>
          {c.use && <div className="mt-1 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="row-use" data-user-text>{c.use}</div>}
          {c.blocked && <div className="mt-1 text-[13px] text-muted-foreground" data-testid="row-blocked">{c.blocked.text}</div>}
        </div>
        {c.actions.length > 0 && (
          <div className="flex max-w-full flex-wrap items-center gap-2">
            {c.actions.map((a) => a.key === "edit" ? (
              <Button key={a.key} variant="secondary" className={cn(SMALL_BTN, DARK)} disabled={busy} onClick={() => onAction("edit")} data-testid={`action-${a.key}`}>{a.label}</Button>
            ) : a.key === "make_default" ? (
              <Button key={a.key} variant="secondary" className={SMALL_BTN} disabled={busy} onClick={() => onAction("make_default")} data-testid={`action-${a.key}`}>{a.label}</Button>
            ) : (
              <Button ref={removeRef} key={a.key} variant="secondary" className={cn(SMALL_BTN, DANGER_BTN)} disabled={busy} onClick={onAsk} data-testid={`action-${a.key}`} aria-expanded={asking}>{a.label}</Button>
            ))}
          </div>
        )}
      </div>
      {asking && c.confirm && (
        <div role="alertdialog" aria-labelledby={`ask-${slot}-${c.id}`} className="grid gap-2 border border-[color:var(--danger-border)] bg-[var(--err-soft)] p-3" data-testid="confirm">
          <div id={`ask-${slot}-${c.id}`} className="text-sm font-semibold">{c.confirm.title}</div>
          <p className="m-0 text-[13px] [overflow-wrap:anywhere]" data-user-text>{c.confirm.text}</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" className={cn(SMALL_BTN, DANGER_BTN)} busy={busy} onClick={() => onAnswer(true)} data-testid="confirm-yes">{c.confirm.yes}</Button>
            <Button ref={keepRef} variant="secondary" className={cn(SMALL_BTN, DARK)} disabled={busy} onClick={keep} data-testid="confirm-no">{c.confirm.no}</Button>
          </div>
        </div>
      )}
    </li>
  );
}

function AvailableRow({ a, onConnect }: { a: SetupAvailable; onConnect: () => void }) {
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border px-4 py-3.5 last:border-b-0" data-testid={`available-${a.id}`}>
      <div className="min-w-0 flex-[1_1_320px]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold [overflow-wrap:anywhere]">{a.title}</span>
          {a.enterprise && <Tag tone={TAG_TONE.built_in} data-testid="tag-enterprise">Enterprise</Tag>}
          {!a.allowed && <Tag tone={TAG_TONE.not_licensed} data-testid="tag-not_licensed">Not licensed</Tag>}
        </div>
        <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{a.description}</div>
        {!a.allowed && a.reason && <div className="mt-1 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="row-reason">{a.reason}</div>}
      </div>
      <div className="flex flex-none items-center gap-2">
        <Button variant="secondary" className={cn(SMALL_BTN, DARK)} disabled={!a.allowed || !a.form} onClick={onConnect} data-testid="action-connect">{a.button}</Button>
      </div>
    </li>
  );
}

function SectionHead({ children, ...props }: { children: React.ReactNode } & React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className="m-0! mb-2 font-mono text-xs! font-medium! uppercase tracking-[0.08em]! text-muted-foreground" {...props}>{children}</h2>;
}

function StepView({ step, nextStep, busy, asking, notice, problem, onGo, onConnect, onEdit, onMakeDefault, onAsk, onAnswer, onClearNotice }: {
  step: SetupStep;
  nextStep: SetupSlot | null;
  busy: boolean;
  asking: string | null;
  notice: string;
  problem: string;
  onGo: (slot: SetupSlot) => void;
  onConnect: (id: string) => void;
  onEdit: (id: string) => void;
  onMakeDefault: (id: string) => void;
  onAsk: (id: string | null) => void;
  onAnswer: (c: SetupConnected, yes: boolean) => void;
  onClearNotice: () => void;
}) {
  return (
    <div className="grid min-w-0 max-w-[880px] gap-6" data-testid="step-view" data-slot={step.slot}>
      <div className="grid gap-2">
        <h1 className="m-0! text-[22px]! min-[900px]:text-[26px]! [overflow-wrap:anywhere]" data-testid="page-title" tabIndex={-1}>{step.name}</h1>
        <p className="m-0 max-w-[760px] text-[15px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="page-sub">{step.why}</p>
      </div>

      {step.banner && <Banner tone="warn" data-testid="step-banner"><span data-user-text>{step.banner.text}</span></Banner>}
      {notice && (
        <Banner tone="note" data-testid="flash" action={<Button variant="quiet" className={SMALL_BTN} onClick={onClearNotice}>Dismiss</Button>}>
          <span data-user-text>{notice}</span>
        </Banner>
      )}
      {problem && <Banner tone="error" data-testid="step-error"><span data-user-text>{problem}</span></Banner>}

      <section aria-labelledby="connected-head" data-testid="connected">
        <SectionHead id="connected-head">{step.connected_heading}</SectionHead>
        <ul className="m-0! list-none! border border-border bg-card p-0!">
          {step.connected.map((c) => (
            <ConnectedRow
              key={c.id}
              c={c}
              slot={step.slot}
              busy={busy}
              asking={asking === c.id}
              onAsk={() => onAsk(c.id)}
              onAnswer={(yes) => onAnswer(c, yes)}
              onAction={(k) => (k === "edit" ? onEdit(c.id) : onMakeDefault(c.id))}
            />
          ))}
        </ul>
      </section>

      <section aria-labelledby="available-head" data-testid="available">
        <SectionHead id="available-head">{step.available_heading}</SectionHead>
        {step.available.length > 0 ? (
          <ul className="m-0! list-none! border border-border bg-card p-0!">
            {step.available.map((a) => <AvailableRow key={a.id} a={a} onConnect={() => onConnect(a.id)} />)}
          </ul>
        ) : (
          <p className="m-0 text-sm text-muted-foreground" data-testid="available-empty">{step.available_empty}</p>
        )}
      </section>

      {step.note && <p className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="step-note">{step.note}</p>}

      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border pt-4" data-testid="step-footer">
        <p className="m-0 min-w-0 flex-[1_1_280px] text-[13px] text-muted-foreground">{step.footer.hint}</p>
        {nextStep ? (
          <Button onClick={() => onGo(nextStep)} data-testid="step-next">{step.footer.next}</Button>
        ) : (
          <Button asChild data-testid="step-next"><Link to="/">{step.footer.next}</Link></Button>
        )}
      </div>
    </div>
  );
}

// ---- the rail ----

function StepButton({ s, current, onPick, id }: { s: SetupStep; current: boolean; onPick: () => void; id: string }) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={current ? "step" : undefined}
      className={cn(
        "flex! w-full items-start gap-3 border-0! border-b! border-border! bg-transparent! px-3! py-3! text-left font-normal! text-foreground! cursor-pointer hover:bg-[var(--row-hover)]! last:border-b-0!",
        current && "bg-[var(--row-hover)]! shadow-[inset_2px_0_0_var(--accent)]",
      )}
      data-testid={`${id}-${s.slot}`}
      data-state={s.state}
    >
      <span
        className={cn("mt-0.5 inline-flex size-[18px] flex-none items-center justify-center border", s.set ? "border-foreground bg-foreground text-background" : "border-input")}
        aria-hidden
      >
        {s.set && <Check className="size-3" />}
      </span>
      <span className="grid min-w-0 gap-0.5">
        <span className="text-sm font-semibold [overflow-wrap:anywhere]">{`${s.number}. ${s.name}`}</span>
        <span className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="step-line">{s.line}</span>
      </span>
    </button>
  );
}

function Rail({ data, slot, onPick }: { data: SetupData; slot: SetupSlot; onPick: (s: SetupSlot) => void }) {
  const [open, setOpen] = React.useState(false);
  const cur = data.steps.find((s) => s.slot === slot) ?? data.steps[0];
  const { set, total } = data.progress;
  const pick = (s: SetupSlot) => { setOpen(false); onPick(s); };
  // The wide rail and the narrow list are both in the page, one hidden by width; the narrow ones
  // have their own test ids so a test finds the one that is shown.
  const list = (id: string) => (
    <div className="border border-border bg-card">
      {data.steps.map((s) => <StepButton key={s.slot} id={id} s={s} current={s.slot === slot} onPick={() => pick(s.slot)} />)}
    </div>
  );
  return (
    <>
      <nav aria-label="Steps" className={cn("hidden self-start sticky top-6 gap-3", "min-[900px]:grid")} data-testid="rail">
        <div className="grid gap-2">
          <h2 className="m-0! text-sm! font-semibold" data-testid="progress-text">{data.progress.text}</h2>
          <div className="h-1 bg-[var(--wash-2)]" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={set} aria-label={data.progress.text}>
            <div className="h-full bg-foreground" style={{ width: `${total ? (set / total) * 100 : 0}%` }} />
          </div>
        </div>
        {list("step")}
      </nav>
      <details open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)} className={cn("border border-border bg-card", "min-[900px]:hidden")} data-testid="rail-narrow">
        <summary className="flex cursor-pointer items-center justify-between gap-3 px-3 py-3 text-sm font-semibold">
          <span>{`Step ${cur.number} of ${total}: ${cur.name}`}</span>
          <span className="text-[13px] font-normal text-muted-foreground">{`${set} set`}</span>
        </summary>
        <div className="border-t border-border">{list("nstep")}</div>
      </details>
    </>
  );
}

// ---- the page ----

function Loading() {
  return (
    <div className="grid gap-6 min-[900px]:grid-cols-[272px_minmax(0,1fr)]" role="status" aria-busy="true" aria-label="Loading" data-testid="setup-loading">
      <div className="grid gap-3 self-start">
        <Skeleton width="60%" height={14} />
        <Skeleton height={4} />
        {Array.from({ length: 7 }, (_, i) => <Skeleton key={i} height={44} />)}
      </div>
      <div className="grid max-w-[880px] gap-4 self-start">
        <Skeleton width="30%" height={26} />
        <Skeleton width="70%" height={14} />
        <Skeleton height={96} />
        <Skeleton height={96} />
      </div>
    </div>
  );
}

/** The page's words in the console header: its name after the cell name (not on a phone) and the way out. */
function HeaderBits({ title, exit }: { title: string; exit: string }) {
  const { crumb, actions } = useHeaderSlots();
  return (
    <>
      {crumb && createPortal(
        <span className="hidden min-w-0 items-center gap-2.5 text-[13px] text-muted-foreground md:flex">
          <span aria-hidden>/</span>
          <span className="truncate" data-testid="setup-title">{title}</span>
        </span>,
        crumb,
      )}
      {actions && createPortal(
        <Button asChild variant="secondary" className={cn(SMALL_BTN, "shrink-0")} data-testid="setup-exit"><Link to="/">{exit}</Link></Button>,
        actions,
      )}
    </>
  );
}

interface OpenForm { id: string; mode: "connect" | "edit"; title: string }

function Setup({ data, reload, first }: { data: SetupData; reload: () => void; first: SetupSlot | null }) {
  const [slot, setSlot] = React.useState<SetupSlot>(() => (data.steps.some((s) => s.slot === first) ? first! : data.steps[0].slot));
  const [view, setView] = React.useState<OpenForm | null>(null);
  const [asking, setAsking] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState("");
  const [problem, setProblem] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const headRef = React.useRef<HTMLDivElement>(null);
  const moved = React.useRef(false);
  const refocus = React.useRef(false);

  const step = data.steps.find((s) => s.slot === slot) ?? data.steps[0];
  const idx = data.steps.indexOf(step);
  const nextStep = idx < data.steps.length - 1 ? data.steps[idx + 1].slot : null;

  // what the form is for, read from the current answer: a connection that appeared closes it
  const item = view?.mode === "edit" ? step.connected.find((c) => c.id === view.id) : view ? step.available.find((a) => a.id === view.id) : undefined;
  const form = view && item ? (item as SetupConnected | SetupAvailable).form : null;
  const showForm = !!view && !!form && !!item;

  // The row the form is for left the answer: the app brought the connection in while the form was open, or
  // the connection is gone. The form closes and the step says what happened, and the form does not
  // come back on its own later.
  React.useEffect(() => {
    if (!view || item) return;
    const connected = step.connected.some((c) => c.id === view.id);
    setNotice(view.mode === "connect" && connected ? `${view.title} is connected.` : view.mode === "edit" ? `${view.title} is no longer connected.` : "");
    setView(null);
  }, [view, item, step]);

  const titleOf = () => headRef.current?.querySelector<HTMLElement>("[data-testid=page-title]");

  // a move to another step or into a form puts the focus on the new title, so a keyboard or screen reader follows
  React.useEffect(() => {
    if (!moved.current) { moved.current = true; return; }
    titleOf()?.focus();
  }, [slot, showForm]);

  // after a removal or a new default the row that had the focus is gone or changed, so the title takes it
  React.useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    titleOf()?.focus();
  }, [data]);

  const go = (s: SetupSlot) => { setSlot(s); setView(null); setAsking(null); setNotice(""); setProblem(""); window.scrollTo?.({ top: 0 }); };

  const fail = (e: unknown) => setProblem(e instanceof Error && e.message ? e.message : GENERIC_ERROR);

  const makeDefaultOf = async (id: string) => {
    setBusy(true); setProblem(""); setNotice("");
    try {
      await makeDefault(slot, id);
      setNotice(`${step.connected.find((c) => c.id === id)?.title ?? id} is now the default for ${step.name}.`);
      refocus.current = true;
      reload();
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const answer = async (c: SetupConnected, yes: boolean) => {
    if (!yes) { setAsking(null); return; }
    setBusy(true); setProblem(""); setNotice("");
    try {
      // a project that picks it moves to the default first: the connection cannot go while a project uses it
      for (const p of c.picked_by) await clearProjectPick(p.uid, slot);
      await removeConnection(slot, c.id);
      setNotice(`${c.title} is ${c.via === "app" ? "disconnected" : "removed"}.`);
      setAsking(null);
      refocus.current = true;
      reload();
    } catch (e) { fail(e); reload(); } finally { setBusy(false); }
  };

  const saved = (text: string) => { setView(null); setNotice(text); setProblem(""); reload(); };

  const openForm = (id: string, mode: OpenForm["mode"]) => {
    const row = mode === "edit" ? step.connected.find((c) => c.id === id) : step.available.find((a) => a.id === id);
    setNotice(""); setProblem(""); setView({ id, mode, title: row?.title ?? id });
  };

  return (
    <div className="mx-auto grid w-full max-w-[1280px] min-w-0 gap-6 px-4 py-6 min-[900px]:px-10" data-testid="setup">
      <HeaderBits title={data.title} exit={data.exit_label} />
      <div className={cn("grid min-w-0 gap-6", !showForm && "min-[900px]:grid-cols-[272px_minmax(0,1fr)] min-[900px]:gap-8")}>
        {!showForm && <Rail data={data} slot={step.slot} onPick={go} />}
        <div ref={headRef} className="min-w-0">
          {showForm && view && form ? (
            <FormView
              key={`${slot}:${view.id}:${view.mode}`}
              slot={slot}
              stepName={step.name}
              form={form}
              app={view.mode === "connect" ? (item as SetupAvailable).app : null}
              name={view.id}
              title={item!.title}
              onBack={() => setView(null)}
              onSaved={saved}
              onPoll={reload}
            />
          ) : (
            <StepView
              step={step}
              nextStep={nextStep}
              busy={busy}
              asking={asking}
              notice={notice}
              problem={problem}
              onGo={go}
              onConnect={(id) => openForm(id, "connect")}
              onEdit={(id) => openForm(id, "edit")}
              onMakeDefault={makeDefaultOf}
              onAsk={setAsking}
              onAnswer={answer}
              onClearNotice={() => setNotice("")}
            />
          )}
        </div>
      </div>
    </div>
  );
}

export default function FactorySetup() {
  useOwnMenu();
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params] = useSearchParams();
  const { data, error, reload } = usePagePolling<SetupData>(() => (sample ? sampleLoad(sample) : setupRead()), 0);

  if (data) return <Setup data={data} reload={reload} first={(params.get("step") as SetupSlot | null) ?? null} />;
  return (
    <div className="mx-auto grid w-full max-w-[1280px] min-w-0 gap-6 px-4 py-6 min-[900px]:px-10" data-testid="setup">
      <HeaderBits title={FALLBACK_TITLE} exit={FALLBACK_EXIT} />
      {error ? (
        <div className="grid max-w-[880px] gap-4">
          <h1 className="m-0! text-[22px]! min-[900px]:text-[26px]!" data-testid="page-title">{FALLBACK_TITLE}</h1>
          <div role="alert" className="border border-[color:var(--danger-border)] bg-[var(--err-soft)] px-3.5 py-2.5 text-[13px] text-foreground" data-testid="setup-error">
            <p className="m-0 [overflow-wrap:anywhere]">
              <b>{LOAD_ERROR_LEAD}</b> {LOAD_ERROR_CAUSE}
              <Button variant="secondary" className={cn(SMALL_BTN, DARK, "ml-2 align-middle")} onClick={reload} data-testid="setup-retry">Try again</Button>
            </p>
          </div>
        </div>
      ) : (
        <Loading />
      )}
    </div>
  );
}
