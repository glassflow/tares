import * as React from "react";

import { cn } from "@/lib/utils";
import { ChevronDown, ChevronRight, Pencil, Search, Terminal } from "../../components/icons";
import type { ConvoItem, ConvoTool } from "./FactoryCrew.api";

// "Show what it did" on the Crew page as a conversation (TR-1177). What the member said is on the left,
// what it was sent is on the right with who sent it, and each tool call is a card: its kind, its name, a
// one-line summary, how long it took and when; the input and the output open below it, each with Copy.
// A failed call has a red edge and a "Failed" mark. Three or more calls in a row with no words between
// them fold into one card. Session events are dividers. Oldest first, opening at the newest and
// following new lines unless the reader has scrolled up. Every string from the cell is drawn as text.

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

/** Text with `code` in backticks shown as code. */
function Prose({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g);
  return (
    <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">
      {parts.map((p, i) => (p.length > 2 && p.startsWith("`") && p.endsWith("`")
        ? <code key={i} className="border border-border bg-muted px-1 font-mono text-[0.9em]">{p.slice(1, -1)}</code>
        : <React.Fragment key={i}>{p}</React.Fragment>))}
    </span>
  );
}

function Initial({ name, dark }: { name: string; dark?: boolean }) {
  const letters = name.replace(/^the\s+/i, "").replace(/[^A-Za-z0-9]+/g, " ").trim().split(" ")
    .filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  return (
    <span aria-hidden className={cn("mt-5 inline-flex size-6 shrink-0 items-center justify-center font-mono text-[10px] font-bold",
      dark ? "bg-[var(--accent)] text-[#1e0f00]" : "border border-border bg-card text-foreground")}>
      {letters}
    </span>
  );
}

function Copy({ text }: { text: string }) {
  const [done, setDone] = React.useState(false);
  return (
    <button type="button" className="h-auto! min-h-0! border border-border bg-card px-1.5! py-0.5! text-[11px]! leading-tight! text-foreground"
            onClick={() => { void navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }); }}
            data-testid="tool-copy">
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function Block({ label, text, testid }: { label: string; text: string; testid: string }) {
  return (
    <div className="px-3 pb-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
        <Copy text={text} />
      </div>
      <pre data-words-exempt data-testid={testid}
           className={cn("m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words border border-border px-3 py-2 font-mono text-xs leading-normal", DARK)}>
        {text || " "}
      </pre>
    </div>
  );
}

function ToolCard({ t, open, onToggle, loadFull }: {
  t: ConvoTool; open: boolean; onToggle: () => void; loadFull: (id: string) => Promise<string>;
}) {
  const [full, setFull] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  React.useEffect(() => {
    if (open && t.more && full === null && !loading) {
      setLoading(true);
      loadFull(t.id).then(setFull).catch(() => setFull(null)).finally(() => setLoading(false));
    }
  }, [open, t.more, t.id, full, loading, loadFull]);
  const output = full ?? t.output;
  const meta = [took(t.ms), hm(t.at)].filter(Boolean).join(" · ");
  return (
    <div className={cn("border bg-card", t.failed ? "border-[var(--err)]" : "border-border")} data-testid="tool-card"
         data-failed={t.failed ? "true" : "false"}>
      <button type="button" className="block w-full min-w-0 px-2.5 py-2 text-left" aria-expanded={open}
              onClick={onToggle} data-testid="tool-head">
        <span className="flex w-full min-w-0 items-center gap-2">
        <span className={cn("inline-flex size-6 shrink-0 items-center justify-center border",
          t.failed ? "border-[var(--err)] text-[color:var(--err)]" : "border-border text-muted-foreground")}>
          <ToolIcon name={t.name} />
        </span>
        <span className="shrink-0 font-mono text-[13px] font-semibold" data-words-exempt>{toolName(t.name)}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" data-words-exempt>{t.summary}</span>
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
        <div className="border-t border-border pt-2" data-testid="tool-body">
          <Block label="Input" text={t.input} testid="tool-input" />
          {t.output !== null && (
            <Block label={loading ? "Output (loading the rest...)" : "Output"} text={output ?? ""} testid="tool-output" />
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
  const shown = open || openAll;
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
              return <div key={r.key} className="ml-8 min-w-0"><Group tools={r.tools} openAll={openAll} opened={opened} toggle={toggle} loadFull={loadFull} /></div>;
            }
            const it = r.item;
            if (it.kind === "tool") {
              return <div key={r.key} className="ml-8 min-w-0"><ToolCard t={it} open={openAll || opened.has(it.id)} onToggle={() => toggle(it.id)} loadFull={loadFull} /></div>;
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
                <div key={r.key} className="flex max-w-[92%] gap-2" data-testid="bubble-said">
                  <Initial name={who} />
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
              <div key={r.key} className="ml-auto flex max-w-[92%] justify-end gap-2" data-testid="bubble-asked">
                <div className="min-w-0 text-right">
                  <div className="mb-1 font-mono text-[11px] text-muted-foreground"><span data-user-text>{it.from}</span>{hm(it.at) ? ` · ${hm(it.at)}` : ""}</div>
                  <div className="inline-block max-w-full bg-foreground px-3 py-2 text-left text-sm text-background dark:bg-[var(--accent)] dark:text-[#1e0f00]" data-user-text><Prose text={it.text} /></div>
                </div>
                <Initial name={it.from} dark />
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
