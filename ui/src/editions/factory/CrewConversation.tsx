import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";
import { ChevronDown, ChevronRight, Pencil, Search, Terminal } from "../../components/icons";
import type { ConvoItem, ConvoTool } from "./FactoryCrew.api";
import { changeOf, DiffText, JsonText, looksLikeDiff } from "./codeColours";

// "Show what it did" on the Crew page as a conversation (TR-1177). What the member said is on the left,
// what it was sent is on the right with who sent it, and each tool call is a card: its kind, its name, a
// one-line summary, how long it took and when; the input and the output open below it, each with Copy.
// A failed call has a red edge and a "Failed" mark. Three or more calls in a row with no words between
// them fold into one card. Session events are dividers. Oldest first, opening at the newest and
// following new lines unless the reader has scrolled up. Every string from the cell is drawn as text.
//
// The most common calls have a body built for them (TR-1317, VIEWS below): Bash shows its description,
// its command and its output with the exit status, TodoWrite its plan as a checklist, Agent its brief and
// report as markdown, Edit and Write their change. Each has a Formatted / Raw switch, and its Copy copies
// the call's input as it came. An input the list cut short does not parse, and a view can say no (null):
// either way the card shows the plain Input and Output blocks, as every other tool does.

const GROUP_AT = 3;
const NEAR_BOTTOM = 48;
const MUTED = "text-xs text-muted-foreground";
const DARK = "bg-[#1e0f00] text-[#efeddb]";

type Row =
  | { kind: "item"; item: ConvoItem; key: string }
  | { kind: "group"; tools: ConvoTool[]; key: string };

/** Runs of three or more tool calls with nothing said between them become one group. */
export function rowsOf(items: ConvoItem[]): Row[] {
  const out: Row[] = [];
  let run: ConvoTool[] = [];
  const flush = () => {
    if (run.length >= GROUP_AT) out.push({ kind: "group", tools: run, key: `g-${run[0].id}` });
    else run.forEach((t) => out.push({ kind: "item", item: t, key: `t-${t.id}` }));
    run = [];
  };
  items.forEach((it, i) => {
    if (it.kind === "tool") { run.push(it); return; }
    flush();
    out.push({ kind: "item", item: it, key: `${it.kind}-${i}-${it.at ?? ""}` });
  });
  flush();
  return out;
}

function hm(at: string | null | undefined): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 5);
}

export function took(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

const FILE_TOOLS = new Set(["Read", "Write", "NotebookRead"]);
const EDIT_TOOLS = new Set(["Edit", "MultiEdit", "NotebookEdit"]);
const SEARCH_TOOLS = new Set(["Grep", "Glob", "WebSearch", "WebFetch", "ToolSearch"]);

function ToolIcon({ name }: { name: string }) {
  const cls = "size-3.5";
  if (name === "Bash" || name === "BashOutput" || name === "KillShell") return <Terminal className={cls} />;
  if (EDIT_TOOLS.has(name)) return <Pencil className={cls} />;
  if (SEARCH_TOOLS.has(name)) return <Search className={cls} />;
  if (FILE_TOOLS.has(name)) {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
        <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6" />
      </svg>
    );
  }
  if (name === "SendMessage" || name.endsWith("send_message")) {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
        <path d="M21 3 10 14" /><path d="m21 3-7 18-4-7-7-4z" />
      </svg>
    );
  }
  if (name === "TodoWrite") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
        <path d="M9 6h11M9 12h11M9 18h11" /><path d="m3 6 1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2" />
      </svg>
    );
  }
  if (name === "Agent" || name === "Task") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
        <rect x="4" y="8" width="16" height="12" /><path d="M12 4v4M9 13v2M15 13v2" />
      </svg>
    );
  }
  if (name.startsWith("mcp__")) {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
        <path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0zM12 16v5" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cls} aria-hidden>
      <path d="M12 2 2 7l10 5 10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
    </svg>
  );
}

/** A tool's name as people read it: an MCP tool by its own name, without the server prefix. */
function toolName(name: string): string {
  if (!name.startsWith("mcp__")) return name;
  const parts = name.split("__");
  return parts[parts.length - 1] || name;
}

/** A message as markdown: bold, lists, headings, code and links. Raw HTML in the text stays text. */
function Prose({ text }: { text: string }) {
  return (
    <div className="md min-w-0 [overflow-wrap:anywhere] [&_pre]:max-w-full [&_pre]:text-xs">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function Copy({ text }: { text: string }) {
  const [done, setDone] = React.useState(false);
  return (
    <button type="button" className="h-auto! min-h-0! border border-border bg-card px-1.5! py-0.5! text-[11px]! leading-tight! text-foreground"
            onClick={() => {
              const w = navigator.clipboard?.writeText(text);
              if (w) w.then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }).catch(() => undefined);
            }}
            data-testid="tool-copy">
      {done ? "Copied" : "Copy"}
    </button>
  );
}

type Look = "json" | "diff" | "plain";
const LABEL = "font-mono text-[11px] uppercase tracking-wide text-muted-foreground";
const PRE = cn("m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words border border-border px-3 py-2 font-mono text-xs leading-normal", DARK);

/** One labelled part of a card's body; `right` holds its controls (the switch, Copy). */
function Section({ label, right, children, testid }: { label: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; testid?: string }) {
  return (
    <div className="min-w-0 px-3 pb-3" data-testid={testid}>
      <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
        <span className={cn(LABEL, "flex min-w-0 items-center gap-2")}>{label}</span>
        {right && <span className="flex shrink-0 items-center gap-1.5">{right}</span>}
      </div>
      {children}
    </div>
  );
}

function Block({ label, text, testid, look, copy, extra }: { label: React.ReactNode; text: string; testid: string; look: Look; copy?: string; extra?: React.ReactNode }) {
  return (
    <Section label={label} right={<>{extra}<Copy text={copy ?? text} /></>}>
      <pre data-words-exempt data-testid={testid} data-look={look} className={PRE}>
        {!text ? " " : look === "json" ? <JsonText text={text} /> : look === "diff" ? <DiffText text={text} /> : text}
      </pre>
    </Section>
  );
}

/** How a call ended, as a small coloured mark: green for done, red for failed. */
function Mark({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 font-mono text-[11px] normal-case tracking-normal",
      ok ? "text-[color:var(--ok)]" : "text-[color:var(--err)]")} data-testid="tool-mark" data-ok={ok ? "true" : "false"}>
      <span aria-hidden className="inline-block size-1.5 bg-current" />{children}
    </span>
  );
}

/** Formatted or Raw: the view built for the tool, or the call's input and output as they came. */
function Switch({ raw, setRaw }: { raw: boolean; setRaw: (raw: boolean) => void }) {
  const btn = (on: boolean) => cn("h-auto! min-h-0! border-0! px-2! py-0.5! font-mono text-[11px]! leading-tight!",
    on ? "bg-foreground! text-background!" : "bg-card! text-muted-foreground!");
  return (
    <span className="inline-flex border border-border" role="group" aria-label="How to show the call" data-testid="tool-switch">
      <button type="button" className={btn(!raw)} aria-pressed={!raw} onClick={() => setRaw(false)} data-testid="tool-formatted">Formatted</button>
      <button type="button" className={btn(raw)} aria-pressed={raw} onClick={() => setRaw(true)} data-testid="tool-raw">Raw</button>
    </span>
  );
}

/** The call's input as an object, or null when it is not one (cut short by the list, or not JSON). */
export function parseInput(text: string): Record<string, unknown> | null {
  try {
    const o: unknown = JSON.parse(text);
    return o && typeof o === "object" && !Array.isArray(o) ? (o as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

/** A Bash result's exit status and the output without the "Exit code N" line Claude Code puts first. A
 *  failed call with no such line (a blocked command) has no code; one that did not fail exited 0. */
export function exitOf(output: string, failed: boolean): { code: number | null; text: string } {
  const m = /^Exit code (\d+)\n?/.exec(output);
  if (m) return { code: Number(m[1]), text: output.slice(m[0].length) };
  return { code: failed ? null : 0, text: output };
}

interface ViewArgs {
  t: ConvoTool; input: Record<string, unknown>;
  /** The output, whole once it loaded; `loading` while the rest of a cut one is on its way. */
  output: string | null; loading: boolean; missed: boolean;
  /** The Formatted / Raw switch, for the view to put on its first label row. */
  toggle: React.ReactNode;
}

interface ToolView {
  /** The name on the header, when it says more than the tool's own. */
  title?: string;
  /** The header's summary is words (a description, a plan), not code. */
  prose?: (input: Record<string, unknown>) => boolean;
  /** A short tag after the summary, such as a sub-agent's type. */
  tag?: (input: Record<string, unknown>) => string;
  /** The body, or null to show the plain Input and Output blocks. */
  body: (a: ViewArgs) => React.ReactNode | null;
}

const rest = (loading: boolean, label: string) => (loading ? `${label} (loading the rest...)` : label);

/** The output part every view ends with, as the plain card shows it. */
function OutputPart({ output, loading, missed, label = "Output", mark, text }: {
  output: string | null; loading: boolean; missed: boolean; label?: string; mark?: React.ReactNode; text?: string;
}) {
  if (output === null) return <p className="m-0 px-3 pb-3 text-xs text-muted-foreground" data-testid="tool-no-result">No result yet.</p>;
  const shown = text ?? output;
  return (
    <>
      <Block label={<>{rest(loading, label)}{mark}</>} text={shown} copy={output} testid="tool-output"
             look={looksLikeDiff(shown) ? "diff" : "plain"} />
      {missed && <p className="m-0 px-3 pb-3 text-xs text-[color:var(--err)]" data-testid="tool-output-missed">The rest of the output did not load.</p>}
    </>
  );
}

function BashView({ t, input, output, loading, missed, toggle }: ViewArgs) {
  const command = typeof input.command === "string" ? input.command : "";
  if (!command.trim()) return null;
  const description = str(input.description);
  const ended = output === null ? null : exitOf(output, t.failed);
  const background = input.run_in_background === true && !t.failed;
  const mark = !ended ? null : background ? <Mark ok>in the background</Mark>
    : ended.code === null ? <Mark ok={false}>failed</Mark> : <Mark ok={ended.code === 0}>exit {ended.code}</Mark>;
  return (
    <>
      {description && (
        <Section label="Description" right={toggle}>
          <p className="m-0 text-sm [overflow-wrap:anywhere]" data-words-exempt data-testid="tool-description">{description}</p>
        </Section>
      )}
      <Section label="Command" right={<>{!description && toggle}<Copy text={t.input} /></>}>
        <pre data-words-exempt data-testid="tool-command" className={PRE}>
          <span aria-hidden className="select-none text-[color:var(--kit-accent)]">$ </span>{command}
        </pre>
      </Section>
      <OutputPart output={output} loading={loading} missed={missed} mark={mark} text={ended?.text} />
    </>
  );
}

interface Step { content: string; status: string; activeForm: string }

function steps(input: Record<string, unknown>): Step[] | null {
  if (!Array.isArray(input.todos) || input.todos.length === 0) return null;
  const out: Step[] = [];
  for (const s of input.todos) {
    if (!s || typeof s !== "object") return null;
    const r = s as Record<string, unknown>;
    if (typeof r.content !== "string") return null;
    out.push({ content: r.content, status: str(r.status), activeForm: str(r.activeForm) });
  }
  return out;
}

function PlanView({ t, input, output, loading, missed, toggle }: ViewArgs) {
  const plan = steps(input);
  if (!plan) return null;
  const done = plan.filter((s) => s.status === "completed").length;
  return (
    <>
      <Section label="Plan" right={<>{toggle}<Copy text={t.input} /></>} testid="tool-plan">
        <div className="mb-2 h-1 border border-border bg-muted" role="img" aria-label={`${done} of ${plan.length} done`}>
          <i className="block h-full bg-[var(--ok)]" style={{ width: `${Math.round((done / plan.length) * 100)}%` }} />
        </div>
        <ul className="m-0 grid list-none gap-1.5 p-0 text-sm" data-words-exempt>
          {plan.map((s, i) => {
            const isDone = s.status === "completed";
            const now = s.status === "in_progress";
            return (
              <li key={i} className={cn("grid grid-cols-[18px_minmax(0,1fr)] items-start gap-2 [overflow-wrap:anywhere]",
                isDone && "text-muted-foreground line-through", now && "font-semibold")} data-testid="plan-step" data-status={s.status || "pending"}>
                <span aria-hidden className={cn("mt-[3px] inline-block size-3.5 border-[1.5px]",
                  isDone ? "border-[var(--ok)] bg-[var(--ok)]" : now ? "border-[var(--kit-accent)] bg-[var(--accent-soft)]" : "border-muted-foreground")} />
                <span>{s.content}{now && <small className="ml-1.5 font-mono text-[10px] font-normal uppercase tracking-wide text-muted-foreground">now</small>}</span>
              </li>
            );
          })}
        </ul>
      </Section>
      {t.failed && <OutputPart output={output} loading={loading} missed={missed} mark={<Mark ok={false}>failed</Mark>} />}
    </>
  );
}

function AgentView({ t, input, output, loading, missed, toggle }: ViewArgs) {
  const brief = str(input.prompt);
  if (!brief) return null;
  const mark = output === null ? null : t.failed ? <Mark ok={false}>failed</Mark> : <Mark ok>done</Mark>;
  return (
    <>
      <Section label="Brief" right={<>{toggle}<Copy text={t.input} /></>} testid="tool-brief">
        <div className="text-sm" data-words-exempt><Prose text={brief} /></div>
      </Section>
      {output === null ? (
        <p className="m-0 px-3 pb-3 text-xs text-muted-foreground" data-testid="tool-no-result">No report yet.</p>
      ) : (
        <Section label={<>{rest(loading, "Report")}{mark}</>} right={<Copy text={output} />} testid="tool-report">
          <div className="max-h-96 overflow-auto border-l-2 border-border pl-3 text-sm" data-words-exempt><Prose text={output} /></div>
          {missed && <p className="m-0 mt-2 text-xs text-[color:var(--err)]" data-testid="tool-output-missed">The rest of the report did not load.</p>}
        </Section>
      )}
    </>
  );
}

function ChangeView({ t, output, loading, missed, toggle }: ViewArgs) {
  const change = changeOf(t.name, t.input);
  if (!change) return null;
  return (
    <>
      <Block label={<>Change{change.file && <> to <span className="normal-case" data-words-exempt>{change.file}</span></>}</>}
             text={change.diff} copy={t.input} testid="tool-input" look="diff" extra={toggle} />
      <OutputPart output={output} loading={loading} missed={missed} />
    </>
  );
}

const AGENT: ToolView = { prose: (i) => !!str(i.description), tag: (i) => str(i.subagent_type), body: AgentView };

/** The tools with a body of their own; every other tool shows its Input and Output blocks. */
const VIEWS: Record<string, ToolView> = {
  Bash: { prose: (i) => !!str(i.description), body: BashView },
  TodoWrite: { title: "Plan", prose: () => true, body: PlanView },
  Agent: AGENT,
  Task: AGENT,
  Edit: { body: ChangeView },
  MultiEdit: { body: ChangeView },
  Write: { body: ChangeView },
};

function ToolCard({ t, open, onToggle, loadFull }: {
  t: ConvoTool; open: boolean; onToggle: () => void; loadFull: (id: string) => Promise<string>;
}) {
  const [full, setFull] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [missed, setMissed] = React.useState(false);   // one try: a fetch that failed is not repeated
  React.useEffect(() => {
    if (open && t.more && full === null && !loading && !missed) {
      setLoading(true);
      loadFull(t.id).then(setFull).catch(() => setMissed(true)).finally(() => setLoading(false));
    }
  }, [open, t.more, t.id, full, loading, missed, loadFull]);
  const output = full ?? t.output;
  const [raw, setRaw] = React.useState(false);
  const view = VIEWS[t.name];
  const input = React.useMemo(() => (view ? parseInput(t.input) : null), [view, t.input]);
  const shaped = view && input ? view : null;
  const custom = open && shaped && input
    ? shaped.body({ t, input, output, loading, missed, toggle: <Switch raw={raw} setRaw={setRaw} /> })
    : null;
  const tag = shaped && input && shaped.tag ? shaped.tag(input) : "";
  const prose = !!(shaped && input && shaped.prose?.(input));
  const meta = [took(t.ms), hm(t.at)].filter(Boolean).join(" · ");
  return (
    <div className={cn("border bg-card", t.failed ? "border-[var(--err)]" : "border-border")} data-testid="tool-card"
         data-failed={t.failed ? "true" : "false"} data-tool={t.name}>
      <button type="button" className="block w-full min-w-0 px-2.5 py-2 text-left" aria-expanded={open}
              onClick={onToggle} data-testid="tool-head">
        <span className="flex w-full min-w-0 items-center gap-2">
        <span className={cn("inline-flex size-6 shrink-0 items-center justify-center border",
          t.failed ? "border-[var(--err)] text-[color:var(--err)]" : "border-border text-muted-foreground")}>
          <ToolIcon name={t.name} />
        </span>
        <span className="shrink-0 font-mono text-[13px] font-semibold" data-words-exempt>{shaped?.title ?? toolName(t.name)}</span>
        <span className={cn("min-w-0 flex-1 truncate", prose ? "text-[13px] text-foreground" : "font-mono text-xs text-muted-foreground")}
              data-words-exempt data-testid="tool-summary">{t.summary}</span>
        {tag && <span className="shrink-0 border border-border px-1.5 font-mono text-[10.5px]" data-words-exempt data-testid="tool-tag">{tag}</span>}
        {t.failed && (
          <span className="shrink-0 border border-[var(--err)] px-1.5 font-mono text-[10px] uppercase text-[color:var(--err)]"
                data-testid="tool-failed">Failed</span>
        )}
        {t.output === null && <span className={cn(MUTED, "shrink-0")}>no answer yet</span>}
        {meta && <span className="hidden shrink-0 font-mono text-[11px] text-muted-foreground sm:inline">{meta}</span>}
        {open ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
        </span>
      </button>
      {open && (
        <div className="border-t border-border pt-2" data-testid="tool-body" data-shaped={custom ? "true" : "false"}>
          {custom && !raw ? custom : (
            <>
              <Block label="Input" text={t.input} testid="tool-input" look="json"
                     extra={custom ? <Switch raw={raw} setRaw={setRaw} /> : undefined} />
              <OutputPart output={output} loading={loading} missed={missed} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Group({ tools, openAll, opened, toggle, loadFull }: {
  tools: ConvoTool[]; openAll: boolean; opened: Set<string>; toggle: (id: string) => void; loadFull: (id: string) => Promise<string>;
}) {
  const [open, setOpen] = React.useState(false);
  const names = [...new Set(tools.map((t) => toolName(t.name)))];
  const failed = tools.filter((t) => t.failed).length;
  const span = [hm(tools[0].at), hm(tools[tools.length - 1].at)].filter(Boolean);
  // a card the reader opened keeps its group open when a refresh folds it into one
  const shown = open || openAll || tools.some((t) => opened.has(t.id));
  return (
    <div className={cn("border bg-card", failed ? "border-[var(--err)]" : "border-border")} data-testid="tool-group">
      <button type="button" className="block w-full min-w-0 px-2.5 py-2 text-left" aria-expanded={shown}
              onClick={() => setOpen(!open)} data-testid="group-head">
        <span className="flex w-full min-w-0 items-center gap-2">
        <span className="inline-flex size-6 shrink-0 items-center justify-center border border-border text-muted-foreground">
          <ToolIcon name="" />
        </span>
        <span className="shrink-0 font-mono text-[13px] font-semibold">Ran {tools.length} tools</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" data-words-exempt>{names.join(", ")}</span>
        {failed > 0 && <span className="shrink-0 border border-[var(--err)] px-1.5 font-mono text-[10px] uppercase text-[color:var(--err)]">{failed} failed</span>}
        {span.length > 0 && <span className="hidden shrink-0 font-mono text-[11px] text-muted-foreground sm:inline">{[...new Set(span)].join("-")}</span>}
        {shown ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
        </span>
      </button>
      {shown && (
        <div className="grid gap-1.5 border-t border-border p-2">
          {tools.map((t) => <ToolCard key={t.id} t={t} open={openAll || opened.has(t.id)} onToggle={() => toggle(t.id)} loadFull={loadFull} />)}
        </div>
      )}
    </div>
  );
}

export function Conversation({ items, who, earlier, onEarlier, loadFull }: {
  items: ConvoItem[];
  /** The member's name, over what it said. */
  who: string;
  /** Show the "Show earlier" button. */
  earlier: boolean;
  onEarlier: () => Promise<void>;
  loadFull: (id: string) => Promise<string>;
}) {
  const box = React.useRef<HTMLDivElement>(null);
  const [follow, setFollow] = React.useState(true);
  const [openAll, setOpenAll] = React.useState(false);
  const [opened, setOpened] = React.useState<Set<string>>(new Set());
  const [loadingEarlier, setLoadingEarlier] = React.useState(false);
  const rows = React.useMemo(() => rowsOf(items), [items]);
  const last = items.length ? `${items.length}-${items[items.length - 1].at}` : "";

  React.useLayoutEffect(() => {
    const el = box.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [last, follow]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM);
  };
  const toggle = (id: string) => setOpened((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const earlierClick = async () => {
    const el = box.current;
    const fromBottom = el ? el.scrollHeight - el.scrollTop : 0;
    setLoadingEarlier(true);
    try { await onEarlier(); } finally { setLoadingEarlier(false); }
    requestAnimationFrame(() => { if (el) el.scrollTop = el.scrollHeight - fromBottom; });
  };

  return (
    <div className="mt-2 w-full min-w-0 max-w-full overflow-hidden border border-border bg-background" data-testid="member-conversation">
      <div ref={box} onScroll={onScroll} className="max-h-[560px] min-w-0 overflow-y-auto overflow-x-hidden px-3 py-3" data-testid="conversation-scroll">
        {earlier && (
          <div className="mb-3 text-center">
            <button type="button" className="linklike text-[13px]" onClick={() => void earlierClick()} disabled={loadingEarlier}
                    data-testid="conversation-earlier">
              {loadingEarlier ? "Loading..." : "Show earlier"}
            </button>
          </div>
        )}
        {rows.length === 0 && <p className="m-0 text-[13px] text-muted-foreground">Nothing yet in this session.</p>}
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
          {rows.map((r) => {
            if (r.kind === "group") {
              return <div key={r.key} className="ml-4 min-w-0"><Group tools={r.tools} openAll={openAll} opened={opened} toggle={toggle} loadFull={loadFull} /></div>;
            }
            const it = r.item;
            if (it.kind === "tool") {
              return <div key={r.key} className="ml-4 min-w-0"><ToolCard t={it} open={openAll || opened.has(it.id)} onToggle={() => toggle(it.id)} loadFull={loadFull} /></div>;
            }
            if (it.kind === "divider") {
              return (
                <div key={r.key} className="flex items-center gap-3 text-[11px] text-muted-foreground" data-testid="conversation-divider">
                  <span className="h-px flex-1 bg-border" />
                  <span className="font-mono">{it.text}{hm(it.at) ? ` · ${hm(it.at)}` : ""}</span>
                  <span className="h-px flex-1 bg-border" />
                </div>
              );
            }
            if (it.kind === "said") {
              return (
                <div key={r.key} className="max-w-[88%]" data-testid="bubble-said">
                  <div className="min-w-0">
                    <div className="mb-1 font-mono text-[11px] text-muted-foreground"><span data-user-text>{who}</span>{hm(it.at) ? ` · ${hm(it.at)}` : ""}</div>
                    <div className="border border-border bg-card px-3 py-2 text-sm" data-user-text><Prose text={it.text} /></div>
                  </div>
                </div>
              );
            }
            if (it.quiet) {
              return (
                <div key={r.key} className="ml-auto max-w-[80%] text-right" data-testid="bubble-factory">
                  <div className="mb-0.5 font-mono text-[11px] text-muted-foreground">{it.from}{hm(it.at) ? ` · ${hm(it.at)}` : ""}</div>
                  <div className="inline-block border border-dashed border-border bg-muted px-2.5 py-1.5 text-left text-xs text-muted-foreground" data-user-text>
                    <Prose text={it.text} />
                  </div>
                </div>
              );
            }
            return (
              <div key={r.key} className="ml-auto flex max-w-[88%] justify-end" data-testid="bubble-asked">
                <div className="min-w-0 text-right">
                  <div className="mb-1 font-mono text-[11px] text-muted-foreground"><span data-user-text>{it.from}</span>{hm(it.at) ? ` · ${hm(it.at)}` : ""}</div>
                  <div className="inline-block max-w-full border border-[color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[var(--accent-soft)] px-3 py-2 text-left text-sm text-foreground dark:bg-[color-mix(in_srgb,var(--accent)_20%,transparent)]" data-user-text><Prose text={it.text} /></div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-1.5">
        {follow ? (
          <span className="flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground" data-testid="conversation-following">
            <span aria-hidden className="inline-block size-1.5 bg-[var(--ok)]" /> Following live, newest at the bottom
          </span>
        ) : (
          <button type="button" className="linklike text-[12px]" onClick={() => { setFollow(true); const el = box.current; if (el) el.scrollTop = el.scrollHeight; }}
                  data-testid="conversation-newest">
            Jump to the newest
          </button>
        )}
        {items.some((i) => i.kind === "tool") && (
          <button type="button" className="linklike text-[12px]" onClick={() => { setOpenAll(!openAll); if (openAll) setOpened(new Set()); }}
                  data-testid="conversation-open-all">
            {openAll ? "Close all tools" : "Open all tools"}
          </button>
        )}
      </div>
    </div>
  );
}
