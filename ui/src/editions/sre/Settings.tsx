import * as React from "react";
import { Link, useLocation } from "react-router-dom";

import { ApiError } from "../../api";
import { licenceWordsOn, useCloud } from "../../cloud";
import { Banner, Button, Dialog, Skeleton, StatusLabel, Toaster, useToast, type Tone } from "../../components/edition";
import { Input } from "../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "../../lib/utils";
import { SettingsRefused, useSettingsRefused } from "../AdminOnly";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import { copyText } from "../copyText";
import {
  saveSreSettings, sreSettings,
  type CreatedKey, type Field, type FieldGroup, type FieldGroupName, type FieldValue, type KeyRow, type Option,
  type SreSettings, type ToolRow,
} from "./Settings.api";
import { isSettingsSample, sampleLoad, sampleSettings, SAMPLE_PARTIAL, type SampleName } from "./sample/settings";

// AI SRE Settings: seven groups on one page, from one read (GET /api/sre/settings). Watching,
// Investigating and Where notes go each save on their own (PUT /api/sre/settings/{group}) and a save
// that stops partway says which part was saved. What a person is editing lives in `edits`, next to
// a copy of the group as it was when they began, so the 30 second refresh never moves a group under
// their hands. Someone who may only read sees plain text: no inputs and no buttons. The cell words
// the values and the sentences; this page keeps the labels of its own fields. A key's secret is shown
// once, held in memory only, and never stored.

const POLL_MS = 30_000;
const NONE = "__none__"; // a select cannot hold the empty string
const NOT_SAVED = "The settings were not saved. Try again in a moment.";
const RULE_LIMIT = 5;

type Values = Record<string, string | string[]>;
interface Edit<G> { base: G; values: Values }
type Edits = { [K in FieldGroupName]?: Edit<SreSettings[K]> };

// The words of each field, per group. Everything else (values, spent so far, when a budget starts
// again, the tools, the keys) comes from the cell.
interface Copy { label: string; sub?: string; help?: string }
const BUDGET_RESETS = 'A monthly budget starts again on the 1st at 00:00 UTC. "Never" counts everything it has ever spent.';
const COPY: Record<FieldGroupName, Record<string, Copy>> = {
  watching: {
    system_name: { label: "What we call what is watched", sub: "Shown in the home headline", help: 'Leave empty to use "your services".' },
    every: { label: "How often it checks" },
    model: { label: "Model" },
    budget_usd: { label: "Budget", sub: "Dollars the watcher may spend" },
    budget_period: { label: "Budget resets", help: BUDGET_RESETS },
    rule: { label: "The rule for what is unusual", sub: "What it compares" },
    hit_rate: { label: "Was it worth raising?", sub: "From the answers people gave" },
  },
  investigating: {
    model: { label: "Model" },
    budget_usd: { label: "Budget", sub: "Dollars the investigator may spend" },
    budget_period: { label: "Budget resets", help: BUDGET_RESETS },
    cooldown: { label: "Waits before looking at the same thing again" },
  },
  notes: {
    slack_channel: { label: "Slack channel for notes", sub: "Findings and incidents are posted here" },
    watch_channel: { label: "When nobody is watching, say so in" },
  },
};
const PERIOD_LABEL: Record<string, string> = { month: "Each month, on the 1st", lifetime: "Never resets" };
const NOT_ENOUGH = 'Not enough answers yet. People answer "Was it worth raising?" on each finding.';

const NAV = [
  { id: "watching", label: "Watching" }, { id: "investigating", label: "Investigating" },
  { id: "notes", label: "Where notes go" }, { id: "tools", label: "Tools" },
  { id: "people", label: "People and keys" }, { id: "edition", label: "Edition" }, { id: "advanced", label: "Advanced" },
];

const SUBTITLE: Record<string, string> = {
  watching: "The watcher looks at your services and raises what is unusual",
  investigating: "The investigator digs into what the watcher raised",
  tools: "What the watcher and investigator can look at",
  people: "Keys let outside tools and people reach this cell",
  edition: "Which set of screens this cell shows",
  advanced: "Everything else, as before",
};

// ---- small helpers ----------------------------------------------------------------------------------

const asString = (v: FieldValue | undefined): string => (v === null || v === undefined ? "" : Array.isArray(v) ? v.join("\u0001") : String(v));
const sameValue = (a: string | string[] | FieldValue | undefined, b: string | string[] | FieldValue | undefined) => asString(a) === asString(b);
const lowerFirst = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);
const enc = (v: string) => (v === "" ? NONE : v);
const dec = (v: string) => (v === NONE ? "" : v);
const fieldOf = (g: FieldGroup, key: string) => g.fields.find((f) => f.key === key);

/** What the person typed, as the cell takes it. */
function toApi(key: string, v: string | string[]): FieldValue {
  if (Array.isArray(v)) return v;
  if (key === "budget_usd") {
    const t = v.trim();
    if (t === "") return null;
    return Number.isFinite(Number(t)) ? Number(t) : t;
  }
  return v;
}

/** The one line under a budget: what is spent and, for a monthly budget, when it starts again. */
function spentLine(g: FieldGroup, f: Field): string {
  const spent = f.spent_words ?? "";
  const resets = fieldOf(g, "budget_period")?.resets_words;
  return resets && !/starts again/i.test(spent) ? `${spent}, ${lowerFirst(resets)}` : spent;
}

const wordsOf = (g: FieldGroup, f: Field, v: string | string[]): string => {
  if (f.options) return f.options.find((o) => o.value === v)?.label ?? f.value_words ?? "";
  return Array.isArray(v) ? v.join(", ") : String(v);
};

// ---- layout pieces -----------------------------------------------------------------------------------

function Sec({ id, title, sub, children }: { id: string; title: string; sub?: string; children: React.ReactNode }) {
  return (
    <section id={id} data-testid={`group-${id}`} className="min-w-0 scroll-mt-20 border border-border bg-card">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border px-4 py-3.5 md:px-[18px]">
        <h2 className="m-0! text-[17px]!">{title}</h2>
        {sub && <span className="text-[13px] text-muted-foreground">{sub}</span>}
      </div>
      {children}
    </section>
  );
}

const ROW = "grid min-w-0 gap-x-6 gap-y-1.5 border-b border-border px-4 py-3.5 last:border-b-0 md:grid-cols-[240px_minmax(0,1fr)] md:px-[18px]";

function Row({ label, sub, htmlFor, testid, children }: { label: string; sub?: string; htmlFor?: string; testid?: string; children: React.ReactNode }) {
  return (
    <div className={ROW} data-testid={testid}>
      <div className="min-w-0">
        <label htmlFor={htmlFor} className="block text-[13px] font-bold text-foreground">{label}</label>
        {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
      </div>
      <div className="grid min-w-0 content-start gap-1.5">{children}</div>
    </div>
  );
}

const Help = ({ children, ...p }: React.HTMLAttributes<HTMLParagraphElement>) =>
  <p className="m-0 max-w-[640px] text-[13px] text-muted-foreground" {...p}>{children}</p>;

function SaveBar({ group, dirty, busy, message, onSave }: { group: string; dirty: boolean; busy: boolean; message?: string; onSave: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 border-t border-border bg-[var(--wash)] px-4 py-3 md:px-[18px]">
      <span className="min-w-0 text-[13px] text-muted-foreground" data-testid={`save-msg-${group}`} role="status">
        {message ?? (dirty ? "You have changes that are not saved." : "")}
      </span>
      <Button busy={busy} disabled={!dirty} onClick={onSave} data-testid={`save-${group}`}>Save</Button>
    </div>
  );
}

function Pick({ id, value, options, onChange, testid, label, wide }: {
  id: string; value: string; options: Option[]; onChange: (v: string) => void; testid: string; label: string; wide?: boolean;
}) {
  const all = options.some((o) => o.value === value) ? options : [{ value, label: value }, ...options];
  return (
    <Select value={enc(value)} onValueChange={(v) => onChange(dec(v))}>
      <SelectTrigger id={id} aria-label={label} data-testid={testid} className={cn("max-w-[380px]", wide && "max-w-[560px]")}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {all.map((o) => <SelectItem key={enc(o.value)} value={enc(o.value)}>{o.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}

// ---- a field group -------------------------------------------------------------------------------------

interface GroupProps {
  name: FieldGroupName;
  group: FieldGroup;
  can: boolean;
  edit?: Edit<FieldGroup>;
  set: (key: string, value: string | string[]) => void;
  busy: boolean;
  message?: string;
  onSave: () => void;
  readOnlyNote?: boolean;
  children?: React.ReactNode;
}

function RuleRow({ g, rule, text, editable, value, set }: {
  g: FieldGroup; rule: Field; text?: Field; editable: boolean; value: string[]; set: (v: string[]) => void;
}) {
  const [adding, setAdding] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const limit = Number(/Up to (\d+)/.exec(rule.help)?.[1]) || RULE_LIMIT;
  const add = () => {
    const t = draft.trim();
    if (t && !value.includes(t) && value.length < limit) set([...value, t]);
    setDraft("");
    setAdding(false);
  };
  const c = COPY.watching.rule;
  return (
    <Row label={c.label} sub={c.sub} testid="field-watching-rule">
      <div className="flex flex-wrap items-center gap-2" data-testid="rule-chips">
        {value.length === 0 && <span className="text-sm text-muted-foreground">Everything together</span>}
        {value.map((l) => (
          <span key={l} data-testid="rule-chip" className="inline-flex items-center gap-1.5 border border-border bg-background px-2.5 py-1 text-[13px]">
            <span data-user-text>{l}</span>
            {editable && (
              <button type="button" aria-label={`Remove ${l}`} data-testid="rule-chip-remove" onClick={() => set(value.filter((x) => x !== l))}
                      className="border-0! bg-transparent! p-0! text-muted-foreground! hover:text-foreground!">&times;</button>
            )}
          </span>
        ))}
        {editable && value.length < limit && !adding && (
          <button type="button" data-testid="rule-add" onClick={() => setAdding(true)}
                  className="border border-dashed border-border! bg-transparent! px-2.5! py-1! text-[13px]! font-normal! text-muted-foreground!">+ Add</button>
        )}
        {editable && adding && (
          <Input autoFocus aria-label="A label to compare by" placeholder="Label" value={draft} data-testid="rule-add-input"
                 onChange={(e) => setDraft(e.target.value)} onBlur={add}
                 onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } if (e.key === "Escape") { setDraft(""); setAdding(false); } }}
                 className="h-8 w-40" />
        )}
      </div>
      {text?.text && (
        <div data-words-exempt data-testid="rule-text" className="max-w-[640px] whitespace-pre-line border border-border bg-[var(--wash)] px-3.5 py-2.5 text-sm [overflow-wrap:anywhere]">
          {text.text}
        </div>
      )}
      {text && (
        <Help>
          The wording of the rule is set in the watcher&apos;s setup, under Advanced.{" "}
          {text.link && <Link to={text.link.value} data-testid="rule-open" className="text-foreground! underline!">Open it</Link>}
        </Help>
      )}
    </Row>
  );
}

function FieldRow({ name, g, f, editable, value, set }: {
  name: FieldGroupName; g: FieldGroup; f: Field; editable: boolean; value: string | string[]; set: (v: string | string[]) => void;
}) {
  const c = COPY[name][f.key] ?? { label: f.label };
  const id = `f-${name}-${f.key}`;
  const test = `${name}-${f.key}`;
  const str = Array.isArray(value) ? "" : value;
  let help = c.help;
  let body: React.ReactNode;

  if (f.key === "hit_rate") {
    body = <div className="text-sm" data-testid="hit-rate">{/^0 answers/.test(f.value_words ?? "") ? NOT_ENOUGH : f.value_words}</div>;
  } else if (!editable) {
    const words = f.key === "budget_period" ? PERIOD_LABEL[str] ?? f.value_words : (f.value_words || wordsOf(g, f, value));
    body = <div className="text-sm [overflow-wrap:anywhere]" data-testid={`value-${test}`}>{words}</div>;
  } else if (f.options) {
    const options = f.key === "budget_period" ? f.options.map((o) => ({ ...o, label: PERIOD_LABEL[o.value] ?? o.label })) : f.options;
    body = <Pick id={id} value={str} options={options} label={c.label} testid={`input-${test}`} onChange={set} />;
  } else if (f.key === "slack_channel") {
    // no channel list to choose from: Slack is not connected, or did not answer
    body = <div className="text-sm [overflow-wrap:anywhere]" data-testid={`value-${test}`}>{f.value_words}</div>;
    help = f.help;
  } else {
    const budget = f.key === "budget_usd";
    body = (
      <Input id={id} value={str} data-testid={`input-${test}`} onChange={(e) => set(e.target.value)} aria-label={c.label}
             inputMode={budget ? "decimal" : undefined} autoComplete="off"
             className={cn("max-w-[380px]", f.key === "system_name" && "max-w-[560px]")} />
    );
    if (f.key === "watch_channel") help = f.help;
  }
  if (f.key === "budget_usd") help = spentLine(g, f) || undefined;
  if (f.key === "budget_period" && !editable) help = undefined;
  if (f.key === "system_name" && !editable) help = undefined;

  return (
    <Row label={c.label} sub={c.sub} htmlFor={editable ? id : undefined} testid={`field-${test}`}>
      {body}
      {help && <Help data-testid={`help-${test}`}>{help}</Help>}
    </Row>
  );
}

function FieldGroupSection({ name, group, can, edit, set, busy, message, onSave, readOnlyNote, children }: GroupProps) {
  const editable = can && group.editable;
  const valueOf = (f: Field): string | string[] => edit?.values[f.key] ?? (Array.isArray(f.value) ? f.value : asString(f.value));
  const rule = fieldOf(group, "rule");
  const text = fieldOf(group, "rule_text");
  const dirty = !!edit && Object.keys(edit.values).length > 0;
  const title = name === "notes" ? "Where notes go" : group.title;
  return (
    <Sec id={name} title={title} sub={SUBTITLE[name]}>
      {readOnlyNote && (
        <div className="border-b border-border bg-[var(--wash)] px-4 py-2.5 text-[13px] text-muted-foreground md:px-[18px]" data-testid="readonly-note">
          You can read these settings but not change them. Ask someone with admin access.
        </div>
      )}
      {group.empty_words && (
        <p className="m-0 border-b border-border px-4 py-3.5 text-sm text-muted-foreground md:px-[18px]" data-testid={`empty-${name}`}>{group.empty_words}</p>
      )}
      {group.fields.map((f) => {
        if (f.key === "rule_text") return null;
        if (f.key === "rule") {
          return <RuleRow key={f.key} g={group} rule={rule!} text={text} editable={editable}
                          value={(valueOf(f) as string[]) ?? []} set={(v) => set("rule", v)} />;
        }
        return <FieldRow key={f.key} name={name} g={group} f={f} editable={editable} value={valueOf(f)} set={(v) => set(f.key, v)} />;
      })}
      {children}
      {editable && group.fields.some((f) => !f.read_only && f.key !== "rule_text") && (
        <SaveBar group={name} dirty={dirty} busy={busy} message={message} onSave={onSave} />
      )}
    </Sec>
  );
}

// ---- tools, people, edition, advanced ------------------------------------------------------------------

function Tag({ r }: { r: ToolRow }) {
  const tone: Tone = r.ok ? "ready" : r.state === "off" ? "quiet" : "bad";
  const text = r.ok ? "Working" : r.state === "off" ? "Not connected" : "Needs attention";
  return <StatusLabel tone={tone} data-testid="tool-state" className="whitespace-nowrap">{text}</StatusLabel>;
}

function ToolsSection({ tools, can }: { tools: SreSettings["tools"]; can: boolean }) {
  const empty = !!tools.empty_words;
  return (
    <Sec id="tools" title={tools.title} sub={SUBTITLE.tools}>
      {empty ? (
        <div className="border-b border-border px-4 py-6 text-center md:px-[18px]" data-testid="tools-empty">
          <b className="block text-[15px]">Nothing connected yet</b>
          <span className="text-sm text-muted-foreground">Add what the watcher should look at: logs, metrics, alerts, deploys.</span>
        </div>
      ) : (
        <div className="max-h-[330px] overflow-y-auto" data-testid="tools-scroll">
          {tools.rows.map((r) => (
            <div key={r.name} data-testid="tool-row" data-ok={r.ok}
                 className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 border-b border-border px-4 py-3 last:border-b-0 md:grid-cols-[220px_minmax(0,1fr)_auto] md:px-[18px]">
              <div className="min-w-0 [overflow-wrap:anywhere]">
                <span className="block text-[13px] font-bold" data-testid="tool-kind">{r.kind}</span>
                {r.name !== r.kind && <span className="block text-xs text-muted-foreground" data-user-text data-testid="tool-name">{r.name}</span>}
              </div>
              <div className="order-last col-span-2 min-w-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere] md:order-none md:col-span-1" data-testid="tool-words">
                {(r.ok ? r.state_words : r.problem_words || r.state_words) ?? ""}
              </div>
              <Tag r={r} />
            </div>
          ))}
        </div>
      )}
      {can && (
        <div className="flex flex-wrap items-center gap-2 bg-[var(--wash)] px-4 py-3 md:px-[18px]" data-testid="tools-add">
          <span className="mr-1 text-[13px] text-muted-foreground">Add a tool</span>
          {tools.add.map((a) => (
            <Button key={a.label} asChild variant="secondary" className="h-8 px-2.5 text-[13px]">
              <Link to={a.value}>{a.label}</Link>
            </Button>
          ))}
        </div>
      )}
    </Sec>
  );
}

function KeyLine({ k, can, busy, onRevoke }: { k: KeyRow; can: boolean; busy: boolean; onRevoke: (k: KeyRow) => void }) {
  return (
    <div data-testid="key-row" className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 border-b border-border px-4 py-3 last:border-b-0 md:grid-cols-[220px_minmax(0,1fr)_auto] md:px-[18px]">
      <b className="min-w-0 text-[13px] [overflow-wrap:anywhere]" data-user-text data-testid="key-name">{k.name}</b>
      <span className="order-last col-span-2 text-[13px] text-muted-foreground md:order-none md:col-span-1" data-testid="key-words">
        {k.scopes_words}. {k.last_used_words}
      </span>
      {can && k.can_revoke && k.id ? (
        <Button variant="secondary" busy={busy} className="h-8 px-2.5 text-[13px]" data-testid="key-revoke" onClick={() => onRevoke(k)}>Revoke</Button>
      ) : <span />}
    </div>
  );
}

function PeopleSection({ people, can, created, onCreate, onRevoke, onDismiss, notice, working }: {
  people: SreSettings["people"]; can: boolean; created?: CreatedKey;
  onCreate: (name: string, scope: string) => Promise<boolean>; onRevoke: (k: KeyRow) => void; onDismiss: () => void;
  notice?: string; working: string | null;
}) {
  const cloud = useCloud();
  const [form, setForm] = React.useState(false);
  const [name, setName] = React.useState("");
  const [scope, setScope] = React.useState("read");
  const [problem, setProblem] = React.useState("");
  const { toast } = useToast();
  const editable = can && people.editable;
  const submit = async () => {
    if (!name.trim()) { setProblem("Give the key a name."); return; }
    setProblem("");
    if (await onCreate(name.trim(), scope)) { setForm(false); setName(""); setScope("read"); }
  };
  const copy = async () => {
    try { await copyText(created!.secret); toast("Key copied"); } catch { toast("Could not copy. Select the key and copy it by hand."); }
  };
  return (
    <Sec id="people" title={people.title} sub={SUBTITLE.people}>
      {created && (
        <div className="grid gap-2 border-b border-border bg-muted px-4 py-3.5 md:px-[18px]" data-testid="created-key" role="status">
          <div className="text-sm">
            <b>Key created: <span data-user-text>{created.name}</span>.</b> {created.scopes_words}. Copy it now, it is not shown again.
          </div>
          <code data-words-exempt data-testid="created-key-secret" className="block max-w-full overflow-x-auto border border-border bg-background px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">{created.secret}</code>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={copy} data-testid="created-key-copy">Copy key</Button>
            <Button variant="quiet" onClick={onDismiss} data-testid="created-key-done">I have copied it</Button>
          </div>
        </div>
      )}
      {notice && <div className="border-b border-border px-4 py-2.5 text-[13px] text-[color:var(--err)] md:px-[18px]" role="alert" data-testid="people-notice">{notice}</div>}
      {people.keys.length === 0 ? (
        <p className="m-0 border-b border-border px-4 py-3.5 text-sm text-muted-foreground md:px-[18px]" data-testid="keys-empty">{people.empty_words}</p>
      ) : (
        <div className="max-h-[330px] overflow-y-auto" data-testid="keys-scroll">
          {people.keys.map((k) => <KeyLine key={k.id ?? k.name} k={k} can={editable} busy={working === k.id} onRevoke={onRevoke} />)}
        </div>
      )}
      {(editable || people.workspace) && (
        <div className="grid gap-3 bg-[var(--wash)] px-4 py-3 md:px-[18px]">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            {editable && !form && <Button variant="secondary" className="h-8 px-2.5 text-[13px]" data-testid="key-create-open" onClick={() => setForm(true)}>Create a key</Button>}
            {people.workspace && (
              <span className="text-[13px] text-muted-foreground" data-testid="workspace-note">
                <Link to={people.workspace.link.value} className="text-foreground! underline!">{people.workspace.words}</Link> opens the control plane
              </span>
            )}
          </div>
          {editable && form && (
            <form className="grid items-end gap-3 md:grid-cols-[minmax(0,260px)_minmax(0,200px)_auto]" data-testid="key-form"
                  onSubmit={(e) => { e.preventDefault(); void submit(); }}>
              <div className="grid gap-1">
                <label htmlFor="key-name" className="text-xs font-semibold">Name</label>
                <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} data-testid="key-name-input" placeholder="Alex laptop" autoComplete="off" />
              </div>
              <div className="grid gap-1">
                <label htmlFor="key-scope" className="text-xs font-semibold">What it can do</label>
                <Pick id="key-scope" value={scope} options={people.scope_options.map((o) => ({ ...o, label: o.label.charAt(0).toUpperCase() + o.label.slice(1) }))}
                      onChange={setScope} testid="key-scope" label="What the key can do" />
              </div>
              <div className="flex gap-2">
                <Button type="submit" busy={working === "create"} data-testid="key-create">Create</Button>
                <Button variant="quiet" onClick={() => { setForm(false); setProblem(""); }}>Cancel</Button>
              </div>
              {problem && <p className="m-0 text-[13px] text-[color:var(--err)] md:col-span-3" role="alert" data-testid="key-problem">{problem}</p>}
            </form>
          )}
        </div>
      )}
      {licenceWordsOn() && <div className="border-t border-border px-4 py-3 text-[13px] text-muted-foreground md:px-[18px]" data-testid="licence-link-row">
        <Link to="/settings/advanced?tab=licence" className="text-foreground! underline!" data-testid="licence-link">Enterprise licence</Link>: see it, paste a key or remove one.
      </div>}
    </Sec>
  );
}

function EditionSection({ edition, can, onSwitch }: { edition: SreSettings["edition"]; can: boolean; onSwitch: (to: Option) => void }) {
  const other = edition.options.find((o) => o.value !== edition.value);
  const canSwitch = can && edition.can_switch && !!other;
  return (
    <Sec id="edition" title={edition.title} sub={SUBTITLE.edition}>
      <Row label="This cell shows" testid="field-edition">
        <div className="flex flex-wrap items-center gap-3">
          <b className="text-sm" data-testid="edition-current">{edition.label}</b>
          {canSwitch && other && <Button variant="secondary" className="h-8 px-2.5 text-[13px]" data-testid="edition-switch" onClick={() => onSwitch(other)}>Switch to {other.label}</Button>}
        </div>
        <Help data-testid="edition-help">{canSwitch ? edition.warning : "Only an admin can switch."}</Help>
      </Row>
    </Sec>
  );
}

function AdvancedSection({ advanced }: { advanced: SreSettings["advanced"] }) {
  return (
    <Sec id="advanced" title={advanced.title} sub={SUBTITLE.advanced}>
      <div className="grid md:grid-cols-2">
        {advanced.links.map((l) => (
          <Link key={l.value} to={l.value} data-testid="advanced-link"
                className="block min-w-0 border-b border-border px-4 py-3 no-underline! hover:bg-[var(--row-hover)] md:px-[18px] md:odd:border-r">
            <span className="block text-[13px] font-bold text-foreground">{l.label}</span>
            <span className="block text-[13px] text-muted-foreground">{l.help}</span>
          </Link>
        ))}
      </div>
      <p className="m-0 px-4 py-3 text-[13px] text-muted-foreground md:px-[18px]" data-testid="advanced-note">{advanced.note}</p>
    </Sec>
  );
}

// ---- the page ---------------------------------------------------------------------------------------------

function Head({ sub }: { sub?: boolean }) {
  return (
    <div>
      <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Settings</h1>
      {sub && <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Change how the watcher and investigator work. Each group saves on its own.</p>}
    </div>
  );
}

function Loading() {
  return (
    <div className="grid gap-6" data-testid="settings-loading" role="status" aria-busy="true" aria-label="Loading">
      {[0, 1, 2].map((i) => (
        <div key={i} className="border border-border bg-card">
          <div className="border-b border-border px-4 py-3.5"><Skeleton width="30%" height={14} /></div>
          {[0, 1, 2].map((j) => (
            <div key={j} className="grid gap-3 border-b border-border px-4 py-4 md:grid-cols-[240px_1fr]">
              <Skeleton width="60%" height={10} /><Skeleton width="45%" height={10} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

const asSample = (n: string | null): SampleName | null => (isSettingsSample(n) ? n : null);

/** What a sample does in place of the cell: a save changes the copy on this page and nothing else. */
function applyLocal(s: SreSettings, group: FieldGroupName, values: Values): SreSettings {
  const g = s[group];
  const fields = g.fields.map((f) => {
    if (!(f.key in values)) return f;
    const v = values[f.key];
    const label = f.options?.find((o) => o.value === v)?.label;
    return { ...f, value: v as FieldValue, value_words: label ?? (Array.isArray(v) ? "Compares by " + v.join(", ") : String(v)) };
  });
  return { ...s, [group]: { ...g, fields } };
}

export default function SettingsGate() {
  const refused = useSettingsRefused(asSample(useSample()) !== null);
  return refused ? <SettingsRefused testId="sre-settings-refused" /> : <Settings />;
}

function Settings() {
  const named = useSample();
  const sample = asSample(named);
  const { toast } = useToast();
  const { data, error, reload } = usePagePolling<SreSettings>(() => (sample ? sampleLoad(sample) : sreSettings()), POLL_MS);

  const [served, setServed] = React.useState<SreSettings>();
  React.useEffect(() => { if (data) setServed(data); }, [data]);

  // an address like /settings#watching opens at that group once the page has loaded
  const { hash } = useLocation();
  const scrolled = React.useRef("");
  React.useEffect(() => {
    if (!served || !hash || scrolled.current === hash) return;
    scrolled.current = hash;
    document.getElementById(hash.slice(1))?.scrollIntoView();
  }, [served, hash]);

  const [edits, setEdits] = React.useState<Edits>({});
  const [saving, setSaving] = React.useState<FieldGroupName | null>(null);
  const [messages, setMessages] = React.useState<Partial<Record<FieldGroupName, string>>>({});
  const [failure, setFailure] = React.useState<{ group: FieldGroupName; text: string } | null>(null);
  const [created, setCreated] = React.useState<CreatedKey>();
  const [peopleNotice, setPeopleNotice] = React.useState("");
  const [working, setWorking] = React.useState<string | null>(null);
  const [switching, setSwitching] = React.useState<Option | null>(null);
  const [switchBusy, setSwitchBusy] = React.useState(false);
  const [switchProblem, setSwitchProblem] = React.useState("");

  // a sample can open already saved, partly saved or asking about the switch
  const seeded = React.useRef(false);
  React.useEffect(() => {
    if (!served || seeded.current || !sample) return;
    seeded.current = true;
    if (sample === "saved") { setMessages({ watching: "Saved just now" }); toast("Watching saved"); }
    if (sample === "partial") {
      setEdits({ watching: { base: served.watching, values: { budget_usd: "2o" } } });
      setFailure({ group: "watching", text: SAMPLE_PARTIAL });
    }
    if (sample === "switch") setSwitching(served.edition.options.find((o) => o.value !== served.edition.value) ?? null);
  }, [served, sample, toast]);

  const setValue = (group: FieldGroupName, key: string, value: string | string[]) => {
    if (!served) return;
    setMessages((m) => ({ ...m, [group]: undefined }));
    setEdits((prev) => {
      const cur = (prev[group] as Edit<FieldGroup> | undefined) ?? { base: served[group] as FieldGroup, values: {} };
      const values: Values = { ...cur.values, [key]: value };
      const field = fieldOf(cur.base, key);
      if (field && sameValue(value, field.value)) delete values[key];
      return { ...prev, [group]: Object.keys(values).length ? { base: cur.base, values } : undefined } as Edits;
    });
  };

  const clearEdit = (group: FieldGroupName) => setEdits((p) => ({ ...p, [group]: undefined }));

  const save = async (group: FieldGroupName) => {
    const edit = edits[group] as Edit<FieldGroup> | undefined;
    if (!edit || !served || saving) return;
    setSaving(group);
    setFailure(null);
    const body: Record<string, FieldValue> = {};
    for (const [k, v] of Object.entries(edit.values)) body[k] = toApi(k, v);
    try {
      let next: SreSettings;
      if (sample) {
        const bad = Object.entries(edit.values).find(([k, v]) => k === "budget_usd" && typeof v === "string" && v.trim() !== "" && !Number.isFinite(Number(v)));
        if (bad) throw new ApiError(SAMPLE_PARTIAL, 400);
        next = applyLocal(served, group, edit.values);
      } else {
        next = await saveSreSettings(group, body);
      }
      setServed(next);
      clearEdit(group);
      setMessages((m) => ({ ...m, [group]: "Saved just now" }));
      toast(`${group === "notes" ? "Where notes go" : served[group].title} saved`);
    } catch (err) {
      const text = err instanceof ApiError ? err.message : NOT_SAVED;
      setFailure({ group, text });
      // what was saved before the stop is saved: show it, and keep only what is still to fix
      if (!sample) {
        try {
          const now = await sreSettings();
          setServed(now);
          const keep: Values = {};
          for (const [k, v] of Object.entries(edit.values)) {
            const f = fieldOf(now[group], k);
            if (!f || !sameValue(v, f.value)) keep[k] = v;
          }
          setEdits((p) => ({ ...p, [group]: Object.keys(keep).length ? { base: now[group], values: keep } : undefined }) as Edits);
        } catch { /* the next refresh brings it */ }
      }
    } finally {
      setSaving(null);
    }
  };

  const createKey = async (name: string, scope: string): Promise<boolean> => {
    if (!served) return false;
    setWorking("create");
    setPeopleNotice("");
    try {
      if (sample) {
        const label = served.people.scope_options.find((o) => o.value === scope)?.label ?? scope;
        const key: CreatedKey = { id: "sample-new", name, scopes_words: "Can " + label, secret: "sample-secret-not-a-real-key" };
        setCreated(key);
        setServed({ ...served, people: { ...served.people, keys: [...served.people.keys, { name, scopes_words: key.scopes_words, last_used_words: "Never used", id: key.id, can_revoke: true }] } });
      } else {
        const { created_key, ...rest } = await saveSreSettings("people", { create: { name, scopes: [scope] } });
        setServed(rest);
        setCreated(created_key);
      }
      return true;
    } catch (err) {
      setPeopleNotice(err instanceof ApiError ? err.message : "The key was not created. Try again in a moment.");
      return false;
    } finally {
      setWorking(null);
    }
  };

  const revoke = async (k: KeyRow) => {
    if (!served || !k.id) return;
    setWorking(k.id);
    setPeopleNotice("");
    try {
      if (sample) setServed({ ...served, people: { ...served.people, keys: served.people.keys.filter((x) => x.id !== k.id) } });
      else setServed(await saveSreSettings("people", { revoke: k.id }));
    } catch (err) {
      setPeopleNotice(err instanceof ApiError ? err.message : "The key was not revoked. Try again in a moment.");
    } finally {
      setWorking(null);
    }
  };

  const doSwitch = async () => {
    if (!switching) return;
    setSwitchBusy(true);
    setSwitchProblem("");
    try {
      if (sample) { setSwitching(null); toast("This is a sample, so nothing changed."); return; }
      await saveSreSettings("edition", { value: switching.value, confirm: true });
      window.location.assign("/"); // the whole console changes: load it again
    } catch (err) {
      setSwitchProblem(err instanceof ApiError ? err.message : "The edition was not changed. Try again in a moment.");
    } finally {
      setSwitchBusy(false);
    }
  };

  const banner = error && (
    <Banner tone="error" data-testid="settings-error"
            action={<Button variant="secondary" onClick={reload} data-testid="settings-retry">Try now</Button>}>
      <b>Settings did not load.</b> <span className="text-foreground">Tares could not read them just now. It will try again in 30 seconds.</span>
    </Banner>
  );

  if (!served) {
    return (
      <div className="grid gap-6" data-testid="sre-settings">
        <Head />
        {banner || <Loading />}
      </div>
    );
  }

  const s = served;
  const can = s.can_edit;
  const view = (name: FieldGroupName): FieldGroup => ((edits[name] as Edit<FieldGroup> | undefined)?.base ?? s[name]) as FieldGroup;
  const groupProps = (name: FieldGroupName) => ({
    name, can, group: view(name), edit: edits[name] as Edit<FieldGroup> | undefined,
    set: (k: string, v: string | string[]) => setValue(name, k, v),
    busy: saving === name, message: messages[name], onSave: () => void save(name),
  });
  const failedTitle = failure && (failure.group === "notes" ? "Where notes go" : s[failure.group].title);
  const partial = failure && /^Saved /.test(failure.text);

  return (
    <div className="grid gap-6" data-testid="sre-settings">
      <Head sub />
      {banner}
      {failure && (
        <Banner tone="warn" data-testid="partial-banner" role="alert"
                action={<Button variant="quiet" onClick={() => setFailure(null)}>Dismiss</Button>}>
          <b>{partial ? `Part of ${failedTitle} was saved.` : `${failedTitle} was not saved.`}</b> {failure.text} Fix it and save again.
        </Banner>
      )}
      <nav aria-label="Settings groups" className="flex flex-wrap gap-2" data-testid="settings-nav">
        {NAV.map((n) => (
          <a key={n.id} href={`#${n.id}`} data-testid={`nav-${n.id}`}
             className="border border-border bg-card px-3 py-1.5 text-[13px] text-foreground! no-underline! hover:bg-muted">{n.label}</a>
        ))}
      </nav>

      <FieldGroupSection {...groupProps("watching")} readOnlyNote={!can} />
      <FieldGroupSection {...groupProps("investigating")} />
      <FieldGroupSection {...groupProps("notes")} />
      <ToolsSection tools={s.tools} can={can} />
      <PeopleSection people={s.people} can={can} created={created} onCreate={createKey} onRevoke={(k) => void revoke(k)}
                     onDismiss={() => setCreated(undefined)} notice={peopleNotice} working={working} />
      <EditionSection edition={s.edition} can={can} onSwitch={(o) => { setSwitchProblem(""); setSwitching(o); }} />
      <AdvancedSection advanced={s.advanced} />

      <Dialog open={switching !== null} onOpenChange={(o) => { if (!o && !switchBusy) setSwitching(null); }} data-testid="edition-dialog"
              title={`Switch to ${switching?.label ?? ""}?`} description={s.edition.warning}
              footer={
                <>
                  <Button variant="secondary" disabled={switchBusy} onClick={() => setSwitching(null)} data-testid="edition-cancel">Cancel</Button>
                  <Button busy={switchBusy} onClick={() => void doSwitch()} data-testid="edition-confirm">Switch</Button>
                </>
              }>
        {switchProblem && <p className="m-0 text-[13px] text-[color:var(--err)]" role="alert" data-testid="edition-problem">{switchProblem}</p>}
      </Dialog>
      <Toaster />
    </div>
  );
}
