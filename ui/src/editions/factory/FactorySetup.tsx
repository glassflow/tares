import * as React from "react";
import { createPortal } from "react-dom";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { cloudLinkHere, useCloud } from "../../cloud";
import { CLOUD_BACK } from "../../pages/Security";
import { TerseSettings } from "../settings/AccessPanels";
import { GithubPanel } from "../settings/GithubPanel";
import { SlackCloudPanel, SlackSelfHostedPanels } from "../settings/SlackPanels";
import { Banner, Button, Skeleton } from "../../components/edition";
import { ERROR_BORDER } from "../../components/edition/Field";
import { Check, ChevronDown } from "../../components/icons";
import { Checkbox } from "../../components/ui/checkbox";
import { Input } from "../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { ENTERPRISE_URL, licenceWordsOn } from "../../licenceWords";
import { cn } from "../../lib/utils";
import { useSample } from "../sample";
import type { StartStep } from "../StartChecklist.api";
import { useHeaderSlots, useOwnMenu } from "../Shell";
import { usePagePolling } from "../usePagePolling";
import {
  clearProjectPick, keepDefault, makeDefault, removeConnection, saveIntegration, setupGithubRepos, setupRead, testIntegration,
  type SettingValue, type TestResult, type SetupApp, type SetupAvailable, type SetupConnected, type SetupData, type SetupField, type SetupForm,
  type SetupSlot, type SetupStep, type SetupTag,
} from "./FactorySetup.api";
import { isSample, sampleLoad } from "./setupSamples";
import { ChoicesForm, Prerequisites, TerminalSteps, YourCrew } from "./SetupParts";
import { OverviewView } from "./FactoryOverview";
import { saveCrewChoices } from "./FactoryHome.api";
import { HOME_SAMPLES } from "./homeSamples";
import type { HomeOnboarding } from "./home.types";

// Set up your factory (TR-872): the one place a factory is set up, and the place to come back to.
// A rail on the left: how the crew works and GitHub first, then the seven integration steps, then the
// commands to run in a terminal. For an integration step, what is connected, what can be connected and
// the form of the one being connected or changed; every string of those comes from GET
// /api/factory/setup, and one generic form renderer draws every integration's fields by kind, so a new
// integration needs no code here. The crew, GitHub and terminal steps read the onboarding and Start
// blocks of GET /api/home, the same ones the home's first-run panel draws. A save goes through the
// routes that already exist.

// The terminal step ticks as the machine reports, so the setup is read again while it is shown.
const POLL_TERMINAL_MS = 4000;
// Below 900 px the rail becomes a list under the title. The classes are written out whole, because Tailwind
// reads class names as literal text and cannot see one built from a variable.

const GENERIC_ERROR = "That did not work. Try again in a moment.";
// Words the page writes itself, where the cell gave none: the failed read has no answer to take them from.
const LOAD_ERROR_LEAD = "The setup could not be loaded.";
const LOAD_ERROR_CAUSE = "Tares did not answer, so nothing here is shown. Nothing was changed.";
const FALLBACK_TITLE = "Set up your factory";
const FALLBACK_EXIT = "Exit setup";
const TEST_NEEDS_FIELDS = "Fill in the required settings first.";

const SMALL_BTN = "h-8 px-3 text-[13px]";
// A button with the dark border of the main action (the mock's plain .btn), a red one for what removes,
// and the same colours as the text of an error. Hover keeps the border, or the secondary variant would reset it.
const DARK = "border-foreground hover:border-foreground";
const DANGER_BTN = "border-err-border text-[color:color-mix(in_srgb,var(--err)_78%,var(--ink))] hover:border-err-border";
const OK_TEXT = "text-[color:color-mix(in_srgb,var(--ok)_62%,var(--ink))]";
// Narrow, the buttons of a row share its width, as in the mock.
const ACTS = "flex flex-wrap items-center gap-2.5 max-[559px]:[&>*]:flex-auto";

// ---- tags ----

const TAG_TONE: Record<SetupTag["key"], string> = {
  default: "border-foreground bg-foreground text-background",
  built_in: "border-border bg-muted text-muted-foreground",
  key: "border-border bg-muted text-muted-foreground",
  app: cn("border-ok-border bg-ok-soft", OK_TEXT),
  licence_ended: "border-warn bg-warn-soft text-foreground",
  not_licensed: "border-warn bg-warn-soft text-foreground",
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

const FIELD_BOX = "h-10 min-w-0";
const BAR = "shadow-[inset_3px_0_0_var(--err)]";

function FieldLabel({ f, htmlFor, as: As = "label" }: { f: SetupField; htmlFor?: string; as?: "label" | "legend" }) {
  return (
    <As {...(As === "label" ? { htmlFor } : {})} className="float-none flex flex-wrap items-baseline gap-x-2 p-0 text-sm font-semibold text-foreground">
      <span>{f.label}</span>
      <span className="text-xs font-normal text-muted-foreground" data-testid="field-requirement">{f.requirement}</span>
    </As>
  );
}

/** The repos a "github_repo" field offers. `repos` is null until a list arrives, and a list stays while the next
 *  one loads (a credential typed in the form is waited on briefly, so each key does not ask again). `note` is
 *  the cell's sentence when GitHub listed nothing. */
function useRepoList(on: boolean, via: "app" | "key", credential: string) {
  const [state, setState] = React.useState<{ repos: string[] | null; loading: boolean; note: string }>(
    { repos: null, loading: on, note: "" });
  const cred = credential.trim();
  const first = React.useRef(true);
  React.useEffect(() => {
    if (!on) { setState({ repos: null, loading: false, note: "" }); return; }
    let live = true;
    const wait = first.current ? 0 : 400;
    first.current = false;
    const t = window.setTimeout(() => {
      setState((s) => ({ ...s, loading: true }));
      setupGithubRepos(via, cred)
        .then((r) => { if (live) setState({ repos: r.repos.length ? r.repos : null, loading: false, note: r.note }); })
        .catch(() => { if (live) setState({ repos: null, loading: false, note: "" }); });
    }, wait);
    return () => { live = false; window.clearTimeout(t); };
  }, [on, via, cred]);
  return state;
}

/** owner/name of a typed repository, lower case, for matching against the list: a pasted GitHub address works. */
const repoKey = (v: string) => v.trim().toLowerCase().replace(/^https?:\/\/[^/]+\//, "").replace(/\.git$/, "").replace(/\/+$/, "");
const SHOWN_REPOS = 50;

/** A text box that also offers the listed repos: typing narrows the list, and anything can still be typed. */
function RepoCombo({ aria, value, onChange, repos, placeholder, more, className }: {
  aria: Record<string, unknown> & { id: string };
  value: string;
  onChange: (v: string) => void;
  repos: string[] | null;
  placeholder: string;
  more: string;
  className: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [hi, setHi] = React.useState(-1);
  const listId = `${aria.id}-list`;
  const key = repoKey(value);
  const all = repos ?? [];
  // A value that is one of the repos (or empty) shows the whole list; anything else narrows it.
  const matches = !key || all.some((r) => r.toLowerCase() === key) ? all : all.filter((r) => r.toLowerCase().includes(key));
  const shown = matches.slice(0, SHOWN_REPOS);
  const listed = all.length > 0;
  const expanded = open && shown.length > 0;
  const pick = (r: string) => { onChange(r); setOpen(false); setHi(-1); };
  const onKey = (e: React.KeyboardEvent) => {
    if (!listed) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setHi((h) => Math.min(h + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
    else if (e.key === "Enter" && expanded && hi >= 0 && shown[hi]) { e.preventDefault(); pick(shown[hi]); }
    else if (e.key === "Escape" && expanded) { e.preventDefault(); setOpen(false); }
  };
  return (
    <div className="relative min-w-0">
      <Input
        {...aria}
        type="text"
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true); setHi(-1); }}
        onClick={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKey}
        placeholder={placeholder || undefined}
        autoComplete="off"
        autoCapitalize="off"
        spellCheck={false}
        role={listed ? "combobox" : undefined}
        aria-autocomplete={listed ? "list" : undefined}
        aria-expanded={listed ? expanded : undefined}
        aria-controls={listed ? listId : undefined}
        aria-activedescendant={expanded && hi >= 0 ? `${listId}-${hi}` : undefined}
        className={cn(className, listed && "pr-10")}
        data-testid="input-repo"
        data-listed={listed ? String(all.length) : undefined}
      />
      {listed && (
        <button type="button" tabIndex={-1} aria-hidden onMouseDown={(e) => { e.preventDefault(); setOpen((o) => !o); }}
          className="absolute inset-y-0 right-0 flex w-10 items-center justify-center border-0 bg-transparent p-0 text-muted-foreground"
          data-testid="repo-toggle">
          <ChevronDown className="size-4" aria-hidden />
        </button>
      )}
      {expanded && (
        <ul id={listId} role="listbox" aria-label="Repositories" data-testid="repo-list"
          className="absolute inset-x-0 top-full z-30 m-0 mt-1 max-h-72 list-none overflow-y-auto border border-border bg-popover p-1 shadow-[0_6px_18px_var(--sheet-shadow)]">
          {shown.map((r, i) => (
            <li key={r} id={`${listId}-${i}`} role="option" aria-selected={r.toLowerCase() === key}
              onMouseDown={(e) => { e.preventDefault(); pick(r); }} onMouseEnter={() => setHi(i)}
              className={cn("flex cursor-pointer items-center justify-between gap-2 px-2.5 py-1.5 text-sm [overflow-wrap:anywhere]",
                i === hi && "bg-row-hover", r.toLowerCase() === key && "font-semibold")}
              data-testid="repo-option">
              <span className="min-w-0">{r}</span>
              {r.toLowerCase() === key && <Check className="size-4 flex-none" aria-hidden />}
            </li>
          ))}
          {matches.length > shown.length && (
            <li role="presentation" className="px-2.5 py-1.5 text-[13px] text-muted-foreground" data-testid="repo-more">
              {more.replace("{n}", String(matches.length - shown.length))}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

function SettingField({ f, value, error, onChange, via = "key", credential = "" }: {
  f: SetupField;
  value: SettingValue;
  error: boolean;
  onChange: (v: SettingValue) => void;
  via?: "app" | "key";
  credential?: string;
}) {
  const picking = f.picker === "github_repo";
  const list = useRepoList(picking, via, credential);
  const base = React.useId();
  const id = `${base}-${f.name}`;
  const helpId = `${id}-help`;
  const errId = `${id}-error`;
  const describedBy = [f.help && helpId, f.kind === "secret" && f.saved && f.saved_note && `${id}-saved`, picking && `${id}-status`, error && errId].filter(Boolean).join(" ") || undefined;
  const aria = { id, "aria-describedby": describedBy, "aria-invalid": error || undefined, "aria-required": f.required || undefined };
  const helpText = picking && list.repos ? f.picker_help || f.help : f.help;
  const help = helpText ? <p id={helpId} className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{helpText}</p> : null;
  // The list's own line: loading while the first list is asked for, then the cell's sentence when GitHub listed nothing.
  const status = !picking ? "" : list.loading && !list.repos ? f.picker_loading || "" : !list.repos ? list.note : "";
  const err = error ? <p id={errId} className="m-0 text-[13px] font-semibold text-err" data-testid="field-error">{f.error}</p> : null;
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
  if (picking) {
    control = (
      <RepoCombo aria={aria} value={typeof value === "string" ? value : ""} onChange={(v) => onChange(v)} repos={list.repos}
        placeholder={list.repos ? f.picker_placeholder || f.placeholder : f.placeholder} more={f.picker_more || ""} className={box} />
    );
  } else if (f.kind === "select") {
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
          <Check className="size-3.5 flex-none text-ok" aria-hidden />
          <span>{f.saved_note}</span>
        </p>
      )}
      {help}
      {picking && (
        <p id={`${id}-status`} className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere] empty:hidden" role="status"
          aria-live="polite" data-testid="repo-status" data-words-exempt={list.note && status === list.note ? "" : undefined}>{status}</p>
      )}
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
      <h2 className="m-0 text-sm font-semibold">What happens next</h2>
      <ol className="m-0 grid list-decimal gap-1.5 pl-5 text-sm text-foreground">
        {app.steps.map((s) => <li key={s} className="[overflow-wrap:anywhere]">{s}</li>)}
      </ol>
      <p className="m-0 text-[13px] text-foreground [overflow-wrap:anywhere]">{app.note}</p>
    </div>
  );
}

function FormView({ slot, stepName, form, app, name, title, onBack, onSaved }: {
  slot: SetupSlot;
  stepName: string;
  form: SetupForm;
  app: SetupApp | null;
  name: string;
  title: string;
  onBack: () => void;
  onSaved: (text: string) => void;
}) {
  const [via, setVia] = React.useState<"app" | "key">(app ? "app" : "key");
  const [values, setValues] = React.useState<Values>(() => initialValues(form.fields));
  const [bad, setBad] = React.useState<string[]>([]);
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState("");
  const [test, setTest] = React.useState<TestResult | null>(null);
  const [testing, setTesting] = React.useState(false);
  const formRef = React.useRef<HTMLFormElement>(null);

  const appMode = !!app && via === "app";
  const done = appMode && app.stage === "done";
  const fields = appMode ? (done && app.repo_step ? appFields(form.fields) : []) : form.fields;
  // The app's page opens in this window and its connect page brings the person back to this step, with the
  // address it was opened from (cloudLinkHere); the step opens this form again, with the app done, to save it.
  const open = () => {
    if (app) window.location.assign(cloudLinkHere(app.url));
  };

  // A test answers for the settings it ran with. Each run takes a number; a change to a field or to the way of
  // connecting takes the next one, so a late answer for older settings is dropped, not shown.
  const testRun = React.useRef(0);
  const forgetTest = () => { testRun.current += 1; setTest(null); setTesting(false); };

  const change = (n: string, v: SettingValue) => {
    setValues((s) => ({ ...s, [n]: v }));
    setBad((b) => b.filter((x) => x !== n));
    forgetTest();
  };

  // Test connection: the settings above go to the service and nothing is saved. A blank secret tests the saved
  // one. The answer is the service's own sentence; a refusal from Tares shows as a failed test too. A result
  // belongs to the settings it ran with, so a change to a field, or to the way of connecting, clears it.
  const runTest = async () => {
    if (testing) return;
    const empty = missing(fields, values);
    setBad(empty);
    setProblem("");
    if (empty.length) {
      setTest({ ok: false, message: TEST_NEEDS_FIELDS });
      return;
    }
    const run = ++testRun.current;
    setTesting(true);
    setTest(null);
    try {
      const r = await testIntegration(slot, name, settingsOf(fields, values));
      if (run === testRun.current) setTest({ ok: r.ok, message: r.message });
    } catch (err) {
      if (run === testRun.current) setTest({ ok: false, message: err instanceof ApiError || err instanceof Error ? err.message || GENERIC_ERROR : GENERIC_ERROR });
    } finally {
      if (run === testRun.current) setTesting(false);
    }
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
  const choosing = appMode && !done;

  return (
    <div className="mx-auto grid w-full max-w-[640px] min-w-0 gap-5" data-testid="setup-form" data-mode={form.mode}>
      <div className="grid gap-1">
        <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-x-1.5 text-[13px] text-muted-foreground">
          <button
            type="button"
            onClick={onBack}
            className="inline border-0 bg-transparent p-0 text-[13px] font-normal text-muted-foreground underline underline-offset-[3px] hover:text-foreground cursor-pointer"
            data-testid="form-back"
          >
            {stepName}
          </button>
          <span aria-hidden>/</span>
          <span aria-current="page" className="[overflow-wrap:anywhere]" data-testid="form-crumb">{form.title}</span>
        </nav>
        <h1 className="m-0 text-[22px] min-[900px]:text-[26px] [overflow-wrap:anywhere]" data-testid="page-title" tabIndex={-1}>{form.title}</h1>
        <p className="m-0 mt-1 text-[15px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="form-lead">{form.lead}</p>
      </div>

      <form ref={formRef} onSubmit={submit} noValidate className="grid min-w-0 gap-[18px] border border-border bg-card p-4 min-[560px]:p-5" data-testid="form-panel">
        {app && !appMode && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" data-testid="app-switch">
            <Button variant="secondary" className={SMALL_BTN} onClick={() => { setVia("app"); forgetTest(); }} data-testid="app-use-app">{app.use_app_label}</Button>
          </div>
        )}
        {choosing && <AppSteps app={app} />}
        {fields.map((f) => (
          <SettingField key={f.name} f={f} value={values[f.name]} error={bad.includes(f.name)} onChange={(v) => change(f.name, v)}
            via={appMode ? "app" : "key"} credential={typeof values.credential === "string" ? values.credential : ""} />
        ))}
        {done && app.done && (
          <div className="border-t border-border pt-4">
            <div
              className={cn("border border-ok-border bg-ok-soft px-3 py-2 text-[13px] [overflow-wrap:anywhere]", OK_TEXT)}
              role="status"
              data-testid="app-done"
            >
              {app.done}
            </div>
          </div>
        )}
        {hasForm && !appMode && (
          <div className="grid border-t border-border pt-4" data-testid="test-box">
            <div role="status" data-testid="test-status">
              {test && (
              <div
                className={cn(
                  "mb-2 border px-3 py-2 text-[13px] [overflow-wrap:anywhere]",
                  test.ok
                    ? cn("border-ok-border bg-ok-soft", OK_TEXT)
                    : "border-err-border bg-err-soft font-semibold text-[color:color-mix(in_srgb,var(--err)_78%,var(--ink))]",
                )}
                data-testid="test-line"
                data-ok={test.ok}
              >
                {test.ok ? "Passed." : "Failed."} <span data-user-text>{test.message}</span>
              </div>
              )}
            </div>
            {!test && (
              <p className="m-0 mb-2 text-[13px] text-muted-foreground" data-testid="test-help">
                {form.test_hint}
              </p>
            )}
            <div>
              <Button variant="secondary" className={cn(SMALL_BTN, DARK)} busy={testing} disabled={busy} onClick={runTest} data-testid="test-connection">Test connection</Button>
            </div>
          </div>
        )}
        {problem && <Banner tone="error" data-testid="form-error"><span data-user-text>{problem}</span></Banner>}
        <div className={ACTS}>
          {/* once the app is done, saving connects it for this step: the app alone only lets the cell in */}
          {choosing ? (
            <Button onClick={open} data-testid="app-open">{app.button}</Button>
          ) : (
            <Button type="submit" busy={busy} data-testid="form-save">{form.save_label}</Button>
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
  onAction: (key: "edit" | "make_default" | "keep") => void;
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
            ) : a.key === "keep" ? (
              <Button key={a.key} variant="secondary" className={cn(SMALL_BTN, DARK)} disabled={busy} onClick={() => onAction("keep")} data-testid={`action-${a.key}`}>{a.label}</Button>
            ) : a.key === "make_default" ? (
              <Button key={a.key} variant="secondary" className={SMALL_BTN} disabled={busy} onClick={() => onAction("make_default")} data-testid={`action-${a.key}`}>{a.label}</Button>
            ) : (
              <Button ref={removeRef} key={a.key} variant="secondary" className={cn(SMALL_BTN, DANGER_BTN)} disabled={busy} onClick={onAsk} data-testid={`action-${a.key}`} aria-expanded={asking}>{a.label}</Button>
            ))}
          </div>
        )}
      </div>
      {asking && c.confirm && (
        <div role="alertdialog" aria-labelledby={`ask-${slot}-${c.id}`} className="grid gap-2 border border-err-border bg-err-soft p-3" data-testid="confirm">
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
  useCloud();   // renders again when /health lands: only a self-hosted cell may name the enterprise licence
  const aboutLicence = !a.allowed && licenceWordsOn();
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border px-4 py-3.5 last:border-b-0" data-testid={`available-${a.id}`}>
      <div className="min-w-0 flex-[1_1_320px]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold [overflow-wrap:anywhere]">{a.title}</span>
          {a.enterprise && <Tag tone={TAG_TONE.built_in} data-testid="tag-enterprise">Enterprise</Tag>}
        </div>
        <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{a.description}</div>
        {!a.allowed && (
          <div className="mt-1 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="row-reason">
            <Tag tone={TAG_TONE.not_licensed} data-testid="tag-not_licensed">Not licensed</Tag>{a.reason ? <> {a.reason}</> : null}
          </div>
        )}
        {aboutLicence && (
          <a href={ENTERPRISE_URL} target="_blank" rel="noreferrer" data-testid="row-licence-link"
             className="mt-1 inline-block text-[13px] text-foreground underline decoration-border underline-offset-[3px]">About the enterprise licence</a>
        )}
      </div>
      <div className="flex flex-none items-center gap-2">
        <Button variant="secondary" className={cn(SMALL_BTN, DARK)} disabled={!a.allowed || !a.form} onClick={onConnect} data-testid="action-connect">{a.button}</Button>
      </div>
    </li>
  );
}

function SectionHead({ children, ...props }: { children: React.ReactNode } & React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className="m-0 mb-2 text-base" {...props}>{children}</h2>;
}

/** The step after this one in the rail, for the footer's button; null on the last. */
interface NextStep { key: StepKey; name: string }

function StepFooter({ hint, next, onGo, button = true }: {
  hint: string; next: NextStep | null; onGo: (k: StepKey) => void; button?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border pt-4" data-testid="step-footer">
      <p className="m-0 min-w-0 flex-[1_1_280px] text-[13px] text-muted-foreground">{hint}</p>
      {!button ? null : next ? (
        <Button onClick={() => onGo(next.key)} data-testid="step-next">Next: {next.name}</Button>
      ) : (
        <Button asChild data-testid="step-next"><Link to="/">Finish setup</Link></Button>
      )}
    </div>
  );
}

function StepView({ step, footer = true, nextStep, busy, asking, notice, problem, onGo, onConnect, onEdit, onMakeDefault, onKeep, onAsk, onAnswer, onClearNotice }: {
  step: SetupStep;
  footer?: boolean;
  nextStep: NextStep | null;
  busy: boolean;
  asking: string | null;
  notice: string;
  problem: string;
  onGo: (key: StepKey) => void;
  onConnect: (id: string) => void;
  onEdit: (id: string) => void;
  onMakeDefault: (id: string) => void;
  onKeep: (id: string) => void;
  onAsk: (id: string | null) => void;
  onAnswer: (c: SetupConnected, yes: boolean) => void;
  onClearNotice: () => void;
}) {
  return (
    <div className="grid min-w-0 max-w-[880px] gap-6" data-testid="step-view" data-slot={step.slot}>
      <div className="grid gap-2">
        <h1 className="m-0 text-[22px] min-[900px]:text-[26px] [overflow-wrap:anywhere]" data-testid="page-title" tabIndex={-1}>{step.name}</h1>
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
        <ul className="m-0 list-none border border-border bg-card p-0">
          {step.connected.map((c) => (
            <ConnectedRow
              key={c.id}
              c={c}
              slot={step.slot}
              busy={busy}
              asking={asking === c.id}
              onAsk={() => onAsk(c.id)}
              onAnswer={(yes) => onAnswer(c, yes)}
              onAction={(k) => (k === "edit" ? onEdit(c.id) : k === "keep" ? onKeep(c.id) : onMakeDefault(c.id))}
            />
          ))}
        </ul>
      </section>

      {/* nothing more to connect for this step: the section is left out, not shown empty */}
      {step.available.length > 0 && (
        <section aria-labelledby="available-head" data-testid="available">
          <SectionHead id="available-head">{step.available_heading}</SectionHead>
          <ul className="m-0 list-none border border-border bg-card p-0">
            {step.available.map((a) => <AvailableRow key={a.id} a={a} onConnect={() => onConnect(a.id)} />)}
          </ul>
        </section>
      )}

      {step.note && <p className="m-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="step-note">{step.note}</p>}

      {footer && <StepFooter hint={step.footer.hint} next={nextStep} onGo={onGo} />}
    </div>
  );
}

// ---- the steps around the integrations: the crew, GitHub, the terminal ----

function StepHead({ name, why }: { name: string; why: string }) {
  return (
    <div className="grid gap-2">
      <h1 className="m-0 text-[22px] min-[900px]:text-[26px] [overflow-wrap:anywhere]" data-testid="page-title" tabIndex={-1}>{name}</h1>
      <p className="m-0 max-w-[760px] text-[15px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="page-sub">{why}</p>
    </div>
  );
}

const CREW_WHY = "The crew reads these when it starts. Change them here at any time.";
const GITHUB_WHY = "The crew opens its pull requests there, and Tares follows each one through review and CI.";
const TERMINAL_WHY = "Your crew is a set of Claude Code sessions on your own machine. Type these commands there; each step ticks itself when it is done.";

function choicesLine(o: HomeOnboarding): string {
  const c = o.choices;
  if (!c.chosen) return "Not set yet";
  const n = c.builders_max ?? 0;
  return `${c.autonomy === "L4-ship" ? "Crew merges" : "You approve merges"}, ${n} builder${n === 1 ? "" : "s"}`;
}

/** The form's own button saves and moves on, so the footer has no second one. */
function CrewView({ o, next, onGo, onSaved }: { o: HomeOnboarding; next: NextStep | null; onGo: (k: StepKey) => void; onSaved: () => void }) {
  return (
    <div className="grid min-w-0 max-w-[880px] gap-6" data-testid="step-view" data-slot="crew">
      <StepHead name="How your crew works" why={CREW_WHY} />
      <ChoicesForm o={o} save={saveCrewChoices}
                   onSaved={() => { onSaved(); if (next) onGo(next.key); }} />
      <StepFooter hint="Who merges, how many builders work at once, and whether the crew may release." next={next} onGo={onGo} button={false} />
    </div>
  );
}

/** Code: GitHub, as Settings managed it (the credentials, the app, its repositories), with its own
 *  connect links that come back to this page. Setup has a Next button under it; Integrations does not. */
function GithubView({ name, next, onGo, footer }: { name: string; next: NextStep | null; onGo: (k: StepKey) => void; footer: boolean }) {
  const cloud = useCloud();
  return (
    <div className="grid min-w-0 max-w-[880px] gap-6" data-testid="step-view" data-slot="github">
      <StepHead name={name} why={GITHUB_WHY} />
      <TerseSettings.Provider value={true}><GithubPanel cloud={cloud} title="Accounts" /></TerseSettings.Provider>
      {footer && <StepFooter hint="" next={next} onGo={onGo} />}
    </div>
  );
}

const NOTIFICATIONS_WHY = "Where the crew asks you its questions and sends the morning status.";

/** Notifications: Slack, connected the way this cell connects it (the Tares Cloud app, or a bot token). */
function NotificationsView() {
  const cloud = useCloud();
  const url = cloud.health?.slack_connect_url;
  return (
    <div className="grid min-w-0 max-w-[880px] gap-6" data-testid="step-view" data-slot="notifications">
      <StepHead name="Notifications" why={NOTIFICATIONS_WHY} />
      <TerseSettings.Provider value={true}>
        {url
          ? <SlackCloudPanel cloud={cloud} connectUrl={url} />
          : <SlackSelfHostedPanels />}
      </TerseSettings.Provider>
    </div>
  );
}

function TerminalView({ o, steps, onGo }: { o: HomeOnboarding; steps: StartStep[]; onGo: (k: StepKey) => void }) {
  const current = steps.find((s) => !s.done)?.key;
  return (
    <div className="grid min-w-0 max-w-[1100px] gap-6" data-testid="step-view" data-slot="terminal">
      <StepHead name="In your terminal" why={TERMINAL_WHY} />
      {/* above both columns, so the crew panel starts level with the first step */}
      <Prerequisites />
      <div className="-mt-3 flex flex-wrap items-start gap-6">
        <div className="grid min-w-0 flex-[999_1_480px] gap-3">
          <TerminalSteps steps={steps} o={o} current={current} showDone />
        </div>
        <YourCrew o={o} />
      </div>
      <StepFooter hint="" next={null} onGo={onGo} />
    </div>
  );
}

// ---- the rail ----

type StepKey = SetupSlot | "overview" | "crew" | "github" | "notifications" | "terminal";

/** One row of the rail: an integration step, or one of the steps around them. `info` is the page
 *  before the steps (how the factory works): no number, no tick, not counted in the progress. */
interface RailItem { key: StepKey; name: string; line: string; set: boolean; info?: boolean }

/** The rail's number of each counted step, by key (the info page has none). */
function stepNumbers(items: RailItem[]): Record<string, number> {
  const out: Record<string, number> = {};
  let n = 0;
  for (const i of items) if (!i.info) out[i.key] = ++n;
  return out;
}

function StepButton({ s, n, current, onPick, id, plain }: { s: RailItem; n: number; current: boolean; onPick: () => void; id: string; plain: boolean }) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={current ? "step" : undefined}
      className={cn(
        "flex w-full items-start gap-3 border-0 border-b border-border bg-transparent px-3 py-3 text-left font-normal text-foreground cursor-pointer hover:bg-row-hover last:border-b-0",
        current && "bg-row-hover shadow-[inset_2px_0_0_var(--accent)]",
      )}
      data-testid={`${id}-${s.key}`}
      data-state={s.set ? "set" : "not_set"}
    >
      {!plain && !s.info && (
        <span
          className={cn("mt-0.5 inline-flex size-[18px] flex-none items-center justify-center border", s.set ? "border-foreground bg-foreground text-background" : "border-input")}
          aria-hidden
        >
          {s.set && <Check className="size-3" />}
        </span>
      )}
      <span className="grid min-w-0 gap-0.5">
        <span className="text-sm font-semibold [overflow-wrap:anywhere]">{plain || s.info ? s.name : `${n}. ${s.name}`}</span>
        {s.line && <span className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="step-line">{s.line}</span>}
      </span>
    </button>
  );
}

/** The list on the left. Setup numbers its steps, ticks them and says how far it is; Integrations
 *  (`plain`) lists its sections only. */
function Rail({ items, current, progress, onPick, plain = false }: {
  items: RailItem[]; current: StepKey; progress: SetupData["progress"]; onPick: (k: StepKey) => void; plain?: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  const at = Math.max(0, items.findIndex((s) => s.key === current));
  const cur = items[at];
  const { set, total, text } = progress;
  const pick = (k: StepKey) => { setOpen(false); onPick(k); };
  const numbers = stepNumbers(items);
  // The wide rail and the narrow list are both in the page, one hidden by width; the narrow ones
  // have their own test ids so a test finds the one that is shown.
  const list = (id: string) => (
    <div className="border border-border bg-card">
      {items.map((s) => <StepButton key={s.key} id={id} s={s} n={numbers[s.key] ?? 0} plain={plain} current={s.key === current} onPick={() => pick(s.key)} />)}
    </div>
  );
  return (
    <>
      <nav aria-label={plain ? "Integrations" : "Steps"} className={cn("hidden self-start sticky top-6 gap-3", "min-[900px]:grid")} data-testid="rail">
        {!plain && <div className="grid gap-2">
          <h2 className="m-0 text-sm font-semibold" data-testid="progress-text">{text}</h2>
          <div className="h-1 bg-wash-strong" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={set} aria-label={text}>
            <div className="h-full bg-foreground" style={{ width: `${total ? (set / total) * 100 : 0}%` }} />
          </div>
        </div>}
        {list("step")}
      </nav>
      <details open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)} className={cn("border border-border bg-card", "min-[900px]:hidden")} data-testid="rail-narrow">
        <summary className="flex cursor-pointer items-center justify-between gap-3 px-3 py-3 text-sm font-semibold">
          <span>{plain || cur.info ? cur.name : `Step ${numbers[cur.key]} of ${total}: ${cur.name}`}</span>
          {!plain && <span className="text-[13px] font-normal text-muted-foreground">{`${set} set`}</span>}
        </summary>
        <div className="border-t border-border">{list("nstep")}</div>
      </details>
    </>
  );
}

// ---- the page ----

function AroundLoading() {
  return (
    <div className="grid max-w-[880px] gap-4 self-start" role="status" aria-busy="true" aria-label="Loading" data-testid="setup-loading">
      <Skeleton width="30%" height={26} />
      <Skeleton width="70%" height={14} />
      <Skeleton height={96} />
    </div>
  );
}

function Loading() {
  return (
    <div className="grid gap-6 min-[900px]:grid-cols-[272px_minmax(0,1fr)]" role="status" aria-busy="true" aria-label="Loading" data-testid="setup-loading">
      <div className="grid gap-3 self-start">
        <Skeleton width="60%" height={14} />
        <Skeleton height={4} />
        {Array.from({ length: 10 }, (_, i) => <Skeleton key={i} height={44} />)}
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

const AROUND: StepKey[] = ["overview", "crew", "github", "notifications", "terminal"];

/** "setup" walks every step once, with Next buttons and a count; "integrations" is the same sections
 *  grouped by what they do for the crew, to come back to at any time. */
type Mode = "setup" | "integrations";

function railItems(mode: Mode, data: SetupData): RailItem[] {
  const o = data.onboarding ?? null;
  const terminal = data.terminal ?? [];
  const slots = data.steps.map((st): RailItem => ({
    key: st.slot, name: st.name, line: mode === "setup" ? st.line : st.line.replace(/ \(not set yet\)$/, ""), set: st.set,
  }));
  if (mode === "integrations") {
    const tool = (k: "github" | "slack") => data.tools?.[k];
    const runtime = slots.filter((x) => x.key === "runtime");
    return [
      { key: "github", name: "Code", line: tool("github")?.detail ?? "", set: !!tool("github")?.connected },
      ...slots.filter((x) => x.key !== "runtime"),
      { key: "notifications", name: "Notifications", line: tool("slack")?.detail ?? "", set: !!tool("slack")?.connected },
      ...runtime,
    ];
  }
  return [
    { key: "overview", name: "How the factory works", line: "", set: false, info: true },
    { key: "crew", name: "How your crew works", line: o ? choicesLine(o) : "", set: !!o?.choices.chosen },
    { key: "github", name: "GitHub", line: o ? (o.github.connected ? o.github.text || "Connected" : "Not connected") : "", set: !!o?.github.connected },
    ...slots,
    { key: "terminal", name: "In your terminal", set: terminal.length > 0 && terminal.every((t) => t.done),
      line: terminal.length ? `${terminal.filter((t) => t.done).length} of ${terminal.length} done` : "" },
  ];
}

function Setup({ data, reload, first, mode = "setup" }: { data: SetupData; reload: () => void; first: string | null; mode?: Mode }) {
  const [params, setParams] = useSearchParams();
  const o = data.onboarding ?? null;
  const terminal = data.terminal ?? [];
  const setupMode = mode === "setup";
  // A Tares Cloud connect page comes back with ?cloud= (and ?cloud_detail=): said once, then dropped
  // from the address so a reload does not say it again.
  const [cloudBack] = React.useState(() => {
    const m = CLOUD_BACK[params.get("cloud") ?? ""];
    return m ? { ...m, detail: params.get("cloud_detail") ?? "" } : null;
  });
  React.useEffect(() => {
    if (!params.has("cloud") && !params.has("cloud_detail")) return;
    setParams((p) => { const q = new URLSearchParams(p); q.delete("cloud"); q.delete("cloud_detail"); return q; }, { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const items = railItems(mode, data);
  // Until the crew's choices are saved (step 1), setup opens on how the factory works; after that, on step 1.
  // The progress count is no guide: a new cell already has its built-in steps set.
  const [key, setKey] = React.useState<StepKey>(() => (items.some((i) => i.key === first) ? first as StepKey
    : items[0].info && o?.choices.chosen ? items[1].key : items[0].key));
  const around = AROUND.includes(key);
  const slot: SetupSlot = around ? data.steps[0].slot : key as SetupSlot;
  const at = items.findIndex((i) => i.key === key);
  const nextStep: NextStep | null = at >= 0 && at < items.length - 1 ? { key: items[at + 1].key, name: items[at + 1].name } : null;
  // Integrations is not a walk: no step is "set" by a click there, so no Use button and no Next
  const shownStep = (st: SetupStep): SetupStep => (setupMode ? st
    : { ...st, connected: st.connected.map((c) => ({ ...c, actions: c.actions.filter((a) => a.key !== "keep") })) });
  // the terminal step ticks as the machine reports: read the home again while it is shown
  React.useEffect(() => {
    if (key !== "terminal") return undefined;
    const t = setInterval(reload, POLL_TERMINAL_MS);
    return () => clearInterval(t);
  }, [key, reload]);
  const [view, setView] = React.useState<OpenForm | null>(null);
  const [asking, setAsking] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState("");
  const [problem, setProblem] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const headRef = React.useRef<HTMLDivElement>(null);
  const moved = React.useRef(false);
  const refocus = React.useRef(false);

  const step = data.steps.find((s) => s.slot === slot) ?? data.steps[0];

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
  }, [key, showForm]);

  // after a removal or a new default the row that had the focus is gone or changed, so the title takes it
  React.useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    titleOf()?.focus();
  }, [data]);

  // the step is kept in the address, so a connect page that sends the person back lands on it
  const go = (k: StepKey) => {
    setKey(k); setView(null); setAsking(null); setNotice(""); setProblem("");
    setParams((p) => { const q = new URLSearchParams(p); q.set("step", k); return q; }, { replace: true });
    window.scrollTo?.({ top: 0 });
  };

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

  const keepOf = async (id: string) => {
    setBusy(true); setProblem(""); setNotice("");
    try {
      await keepDefault(slot, id);
      setNotice(`${step.connected.find((c) => c.id === id)?.title ?? id} is set for ${step.name}.`);
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

  // Back from an app's connect page (?cloud=linear-connected on Tickets): its form opens again, now with the
  // app done, so one click saves it as this step's integration. Once only, and only if it is still to save.
  const reopened = React.useRef(false);
  React.useEffect(() => {
    if (reopened.current || !cloudBack?.tab || around) return;
    reopened.current = true;
    const row = step.available.find((a) => a.id === cloudBack.tab && a.app?.stage === "done");
    if (row) setView({ id: row.id, mode: "connect", title: row.title });
  }, [cloudBack, around, step]);

  return (
    <div className={cn("mx-auto grid w-full max-w-[1280px] min-w-0 gap-6", setupMode && "px-4 py-6 min-[900px]:px-10")} data-testid={setupMode ? "setup" : "integrations"}>
      {setupMode && <HeaderBits title={data.title} exit={data.exit_label} />}
      <div className={cn("grid min-w-0 gap-6", !showForm && "min-[900px]:grid-cols-[272px_minmax(0,1fr)] min-[900px]:gap-8")}>
        {!showForm && <Rail items={items} current={key} progress={data.progress} onPick={go} plain={!setupMode} />}
        <div ref={headRef} className="grid min-w-0 content-start gap-4">
          {cloudBack && !showForm && (
            <Banner tone={cloudBack.kind === "error" ? "error" : "note"} data-testid="cloud-back">
              <span data-user-text>{cloudBack.text}{cloudBack.detail ? ` ${cloudBack.detail}` : ""}</span>
            </Banner>
          )}
          {key === "overview" ? <OverviewView data={data} numbers={stepNumbers(items)} next={nextStep} onGo={(k) => go(k as StepKey)} />
          : key === "github" ? <GithubView name={setupMode ? "GitHub" : "Code"} next={nextStep} onGo={go} footer={setupMode} />
          : key === "notifications" ? <NotificationsView />
          : around ? (
            !o ? <AroundLoading />
              : key === "crew" ? <CrewView o={o} next={nextStep} onGo={go} onSaved={reload} />
              : <TerminalView o={o} steps={terminal} onGo={go} />
          ) : showForm && view && form ? (
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
            />
          ) : (
            <StepView
              step={shownStep(step)}
              footer={setupMode}
              nextStep={nextStep}
              busy={busy}
              asking={asking}
              notice={notice}
              problem={problem}
              onGo={go}
              onConnect={(id) => openForm(id, "connect")}
              onEdit={(id) => openForm(id, "edit")}
              onMakeDefault={makeDefaultOf}
              onKeep={keepOf}
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

/** A sample has no crew of its own: it borrows the home sample of a new cell for the steps around. */
const withSampleAround = (d: SetupData): SetupData => ({
  ...d,
  onboarding: d.onboarding ?? HOME_SAMPLES.startNothing.onboarding,
  terminal: d.terminal ?? (HOME_SAMPLES.startNothing.start?.steps ?? []).filter((s) => s.where === "terminal"),
});

/** Integrations: the same sections as setup, grouped by what they do, in the console's own frame. */
export function FactoryIntegrations() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params] = useSearchParams();
  const { data, error, reload } = usePagePolling<SetupData>(() => (sample ? sampleLoad(sample).then(withSampleAround) : setupRead()), 0);
  if (data) return <Setup data={data} reload={reload} first={params.get("step")} mode="integrations" />;
  return error ? (
    <div role="alert" className="border border-err-border bg-err-soft px-3.5 py-2.5 text-[13px] text-foreground" data-testid="integrations-error">
      <b>The integrations could not be loaded.</b> Nothing was changed.
      <Button variant="secondary" className={cn(SMALL_BTN, DARK, "ml-2 align-middle")} onClick={reload}>Try again</Button>
    </div>
  ) : <Loading />;
}

export default function FactorySetup() {
  useOwnMenu();
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params] = useSearchParams();
  const { data, error, reload } = usePagePolling<SetupData>(() => (sample ? sampleLoad(sample).then(withSampleAround) : setupRead()), 0);

  if (data) return <Setup data={data} reload={reload} first={params.get("step")} />;
  return (
    <div className="mx-auto grid w-full max-w-[1280px] min-w-0 gap-6 px-4 py-6 min-[900px]:px-10" data-testid="setup">
      <HeaderBits title={FALLBACK_TITLE} exit={FALLBACK_EXIT} />
      {error ? (
        <div className="grid max-w-[880px] gap-4">
          <h1 className="m-0 text-[22px] min-[900px]:text-[26px]" data-testid="page-title">{FALLBACK_TITLE}</h1>
          <div role="alert" className="border border-err-border bg-err-soft px-3.5 py-2.5 text-[13px] text-foreground" data-testid="setup-error">
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
