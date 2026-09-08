import { memo, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import { TimeAgo } from "./bits";
import { applyProposal, ProposalCard } from "./proposals";
import type { DecisionMap, Proposal } from "./proposals";
import { useAgentStream } from "./useAgentStream";
import type { StreamEvent } from "./useAgentStream";

type SessionMeta = { id: string; title: string; created_at: string; updated_at: string };

// The Ask assistant. Backs both the /ask page and the global ⌘K palette — the page for long
// sessions, the overlay to ask from anywhere without losing your place.
//
// There is no second "organize" surface any more. It sent the same tools to the same endpoint and
// differed only by a system prompt, so the judgement it carried (what makes a good key, labels come
// from real fields, watch for value variants) now applies to every proposal — see tares/agent.py.
// The full-source sweep it ran is a starter prompt below.
//
// The wire itself (fetch, SSE parse, Stop) lives in useAgentStream, shared with the AI-guided
// project builder; this component owns the transcript and how it renders.

export type ToolPart = {
  type: "tool"; id: string; name: string; input: unknown;
  // filled in by the matching `tool_done` event
  ms?: number; ok?: boolean; preview?: string;
};
type Part = { type: "text"; text: string } | ToolPart | { type: "proposal"; proposal: Proposal };
type Msg = { role: "user" | "assistant"; parts: Part[] };

const ORGANIZE_PROMPT =
  "Organize my data: inventory every source that has data, then propose the labels, keys and " +
  "views that would let me correlate it. Check existing labels first and don't duplicate them.";

const STARTERS: { title: string; prompts: string[] }[] = [
  {
    title: "Explore the data",
    prompts: [
      "What sources am I ingesting, and what does each one contain?",
      "What entities exist and how many events does each have?",
    ],
  },
  {
    title: "Debug something",
    prompts: [
      "Is anything not ingesting, empty, or sparse? What looks off?",
      "What data is stale or unusually low-volume?",
    ],
  },
  {
    title: "Organize it",
    prompts: [
      ORGANIZE_PROMPT,
      "Which of my labels would make bad entity keys, and why?",
    ],
  },
];

const textOf = (m: Msg, decisions?: DecisionMap) =>
  m.parts.map((p) => {
    if (p.type === "text") return p.text;
    if (p.type === "proposal") {
      const st = decisions?.[p.proposal.id]?.status ?? "pending";
      const what = p.proposal.kind === "labels"
        ? `labels for source ${p.proposal.source}`
        : `${p.proposal.kind} ${p.proposal.name}`;
      return `\n[proposal: ${what}; ${st}]\n`;
    }
    return "";
  }).join("");

function compact(v: unknown): string {
  const s = JSON.stringify(v);
  return s === "{}" ? "" : s.length > 60 ? s.slice(0, 60) + "…" : s;
}

/** Hysteresis, not one threshold. With a single cutoff the state flips back and forth while you
 *  scroll across it, and each flip re-rendered the transcript AND mounted/unmounted a sticky
 *  element — which made the whole viewport shiver on a fast scroll. Re-attach only well inside the
 *  tail, detach only well outside it, so crossing once cannot oscillate.
 *  The gap also covers the pinned composer, which hides ~76px of the bottom. */
const ATTACH_PX = 180;
const DETACH_PX = 360;

/** Grow with the content up to a ceiling, then scroll inside itself. */
const MAX_COMPOSER_PX = 160;
const growTextarea = (ta: HTMLTextAreaElement) => {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, MAX_COMPOSER_PX) + "px";
};

const scrollToEnd = () => {
  const el = document.scrollingElement || document.documentElement;
  el.scrollTop = el.scrollHeight;
};

export default function AskChat({ history = false }: { history?: boolean }) {
  const [ready, setReady] = useState<boolean>();      // is a key configured on the server?
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const { send: stream, stop, streaming: busy } = useAgentStream();
  const [decisions, setDecisions] = useState<DecisionMap>({});
  // Follow the tail only while the reader is at the tail. The old code called scrollTo() on every
  // streamed chunk unconditionally, so scrolling up to re-read something was undone by the next
  // token and a long answer could not be read until it finished.
  //
  // `stick` is a REF, not state: the scroll handler runs on every scroll event, and setting state
  // there re-rendered the entire transcript — markdown, tables and all — at scroll speed. Only the
  // jump button needs to re-render, so only it gets state, and only when the value truly changes.
  const stick = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const refreshKey = () => api.capabilities()
    .then((c) => setReady(!!c.agent_key_configured)).catch(() => setReady(false));
  useEffect(() => { refreshKey(); }, []);

  // ── server-side chat history ──────────────────────────────────────────────
  // The conversation is saved to the daemon after every exchange, so navigating away (or closing
  // the browser) loses nothing: on mount the latest session is resumed, and older ones are
  // offered on the empty screen. The id is a ref, not state — nothing renders it, and a state
  // update here would re-render the transcript for no reason.
  const sessionId = useRef("");
  const dirty = useRef(false);   // set by a sent message or a proposal decision, never by opening
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const refreshSessions = () =>
    api.askSessions().then((r) => setSessions(r.sessions)).catch(() => {});

  const restore = (id: string, state: string) => {
    try {
      const st = JSON.parse(state || "{}");
      sessionId.current = id;
      dirty.current = false;
      setMsgs(st.msgs ?? []);
      setDecisions(st.decisions ?? {});
      stick.current = true;
      setShowJump(false);
    } catch { /* an unreadable blob is a fresh start, not an error screen */ }
  };

  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;   // resume once per mount, never after the user pressed New
    booted.current = true;
    api.askSessions().then(async (r) => {
      setSessions(r.sessions);
      if (!r.sessions.length) return;
      const full = await api.askSession(r.sessions[0].id);
      restore(full.id, full.state);
    }).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Save when a turn settles (stream finished, or a proposal decided) — never per token. The
  // debounce coalesces the decision clicks that land in a burst.
  const saveTimer = useRef<number>();
  useEffect(() => {
    if (busy || msgs.length === 0 || !dirty.current) return;
    if (!sessionId.current) sessionId.current = crypto.randomUUID().replace(/-/g, "");
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const first = msgs.find((m) => m.role === "user");
      const title = (first ? textOf(first).trim() : "conversation").slice(0, 80);
      api.saveAskSession(sessionId.current, title, JSON.stringify({ msgs, decisions }))
        .then(() => { dirty.current = false; refreshSessions(); })
        .catch(() => {});   // a failed save must not disturb the conversation
    }, 600);
    return () => window.clearTimeout(saveTimer.current);
  }, [busy, msgs, decisions]);

  const openSession = async (id: string) => {
    try {
      const full = await api.askSession(id);
      restore(full.id, full.state);
    } catch { refreshSessions(); }   // deleted meanwhile; drop it from the list
  };
  const newConversation = () => {
    sessionId.current = "";
    dirty.current = false;
    setMsgs([]); setDecisions({});
    stick.current = true; setShowJump(false);
  };
  const removeSession = async (id: string) => {
    try { await api.deleteAskSession(id); } catch { /* already gone */ }
    if (sessionId.current === id) { sessionId.current = ""; setMsgs([]); setDecisions({}); }
    refreshSessions();
  };

  // One layout read per animation frame, never per scroll event, and no React state unless the
  // button's visibility actually changes.
  const lastTop = useRef(0);
  const queued = useRef(false);
  useEffect(() => {
    const measure = () => {
      queued.current = false;
      const el = document.scrollingElement || document.documentElement;
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      // Detach on a deliberate upward scroll, or once well clear of the tail. Re-attach only when
      // back inside it — streamed text lands between a programmatic scroll and its event, so
      // "am I exactly at the bottom?" is the wrong question to ask here.
      if (el.scrollTop < lastTop.current - 2 || gap > DETACH_PX) stick.current = false;
      else if (gap < ATTACH_PX) stick.current = true;
      lastTop.current = el.scrollTop;
      setShowJump(!stick.current);
    };
    const onScroll = () => {
      if (queued.current) return;
      queued.current = true;
      requestAnimationFrame(measure);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Coalesce: a burst of streamed chunks scrolls once per frame, not once per token. Setting
  // scrollTop directly beats scrollIntoView on a sentinel — the sentinel sits above the sticky
  // composer, so aligning to it made the browser scroll, re-place the sticky bar, then scroll
  // again on the next chunk.
  const scrollQueued = useRef(false);
  useEffect(() => {
    if (!stick.current || scrollQueued.current) return;
    scrollQueued.current = true;
    requestAnimationFrame(() => { scrollQueued.current = false; if (stick.current) scrollToEnd(); });
  }, [msgs]);

  if (ready === undefined) return <div className="dim">loading…</div>;
  if (!ready) return <KeySetup onSaved={refreshKey} />;

  const mutLast = (fn: (parts: Part[]) => Part[]) =>
    setMsgs((cur) => cur.map((m, i) => (i === cur.length - 1 ? { ...m, parts: fn(m.parts) } : m)));
  const appendText = (t: string) => mutLast((parts) => {
    const last = parts[parts.length - 1];
    if (last && last.type === "text") return [...parts.slice(0, -1), { type: "text", text: last.text + t }];
    return [...parts, { type: "text", text: t }];
  });
  const addTool = (id: string, name: string, inputv: unknown) =>
    mutLast((parts) => [...parts, { type: "tool", id, name, input: inputv }]);
  const finishTool = (id: string, patch: Partial<ToolPart>) =>
    mutLast((parts) => parts.map((p) => (p.type === "tool" && p.id === id ? { ...p, ...patch } : p)));

  // One transcript message on the wire. A turn the user pressed Stop on before it produced any
  // text serializes to "" — tool calls contribute nothing — and the Messages API rejects empty
  // content, so the NEXT question would fail with a ⚠️ pointing at nothing. Say what happened
  // instead of dropping the turn: dropping it would leave two user turns in a row.
  const wire = (m: Msg) => {
    const content = textOf(m, decisions);
    return { role: m.role, content: content.trim() ? content : "[stopped before answering]" };
  };

  async function send(text: string) {
    if (!text.trim() || busy) return;
    dirty.current = true;
    setInput("");
    if (taRef.current) { taRef.current.style.height = "auto"; }
    stick.current = true;
    const history: Msg[] = [...msgs, { role: "user", parts: [{ type: "text", text }] }];
    setMsgs([...history, { role: "assistant", parts: [] }]);
    const onEvent = (e: StreamEvent) => {
      if (e.type === "text") appendText(e.text);
      else if (e.type === "tool") addTool(e.id, e.name, e.input);
      else if (e.type === "tool_done") finishTool(e.id, { ms: e.ms, ok: e.ok, preview: e.preview });
      else if (e.type === "proposal") mutLast((parts) => [...parts, { type: "proposal", proposal: e.proposal }]);
      else if (e.type === "error") appendText(`\n\n⚠️ ${e.detail}`);
    };
    await stream(history.map(wire), onEvent);
  }

  const decide = (id: string, status: "applied" | "skipped" | "error", detail?: string) => {
    dirty.current = true;
    setDecisions((d) => ({ ...d, [id]: { status, detail } }));
  };
  const apply = async (p: Proposal) => {
    try { await applyProposal(p); decide(p.id, "applied"); }
    catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); }
  };

  return (
    <div className={"askchat" + (history ? " with-rail" : "")}>
      {history && (
        <aside className="ask-side">
          <button type="button" className="primary ask-rail-new" onClick={newConversation}
                  disabled={busy}>+ New conversation</button>
          <div className="help" style={{ margin: "14px 0 4px" }}>Recents</div>
          <div className="ask-rail-list">
            {sessions.map((sess) => (
              <div key={sess.id}
                   className={"ask-rail-item" + (sess.id === sessionId.current ? " active" : "")}
                   onClick={() => { if (!busy) openSession(sess.id); }}>
                <span className="t" title={sess.title}>{sess.title || "conversation"}</span>
                <span className="help when"><TimeAgo ts={sess.updated_at} /></span>
                <button type="button" className="x" title="delete this conversation"
                        onClick={(e) => { e.stopPropagation(); removeSession(sess.id); }}>×</button>
              </div>
            ))}
            {sessions.length === 0 && <div className="help">no conversations yet</div>}
          </div>
        </aside>
      )}
      <div className="ask-main">
      <div className="chat">
        {msgs.length === 0 && (
          <div className="starters">
            {STARTERS.map((g) => (
              <div key={g.title} className="starter-group">
                <div className="help">{g.title}</div>
                {g.prompts.map((p) => (
                  <button key={p} className="starter" onClick={() => send(p)}>{p}</button>
                ))}
              </div>
            ))}
          </div>
        )}
        {msgs.map((m, i) => (
          <Turn key={i} msg={m} decisions={decisions} apply={apply} decide={decide}
                thinking={busy && i === msgs.length - 1 && m.parts.length === 0} />
        ))}
      </div>

      {/* Always mounted; only its `hidden` class changes. Adding and removing a STICKY element as
          you scroll past the threshold relayouts the page under the scroll, which is what made the
          viewport shiver. */}
      <div className={"jump-latest-wrap" + (showJump && msgs.length > 0 ? "" : " hidden")}>
        <button className="jump-latest"
                onClick={() => { stick.current = true; setShowJump(false); scrollToEnd(); }}>
          ↓ latest
        </button>
      </div>

      {/* A textarea, not an input: a question worth asking a data tool often carries a log line or
          a config snippet, and a one-line field can't hold one. Enter sends, Shift+Enter breaks. */}
      <form className="askbar" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea ref={taRef} value={input} placeholder="ask about your data…" rows={1} autoFocus
                  onChange={(e) => { setInput(e.target.value); growTextarea(e.target); }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
                  }} />
        {/* One button, two jobs — the prototype's call, and right: Send and Stop are never both
            available, so two buttons is one more thing to read mid-answer. */}
        {busy
          ? <button type="button" className="danger" onClick={stop}>Stop</button>
          : <button className="primary" disabled={!input.trim()}>Send</button>}
        {!history && msgs.length > 0 && !busy && (
          <button type="button" className="dim" title="start a new conversation"
                  onClick={newConversation}>New</button>
        )}
      </form>
      </div>
    </div>
  );
}

/** One turn. Consecutive tool calls fold into a single line: they are mechanical detail in the
 *  middle of the reasoning, and at full weight they were most of what you scrolled past.
 *
 *  memo(): only the streaming turn changes, so re-rendering the parent must not re-parse the
 *  markdown of every finished one. */
const Turn = memo(function Turn({ msg, decisions, apply, decide, thinking }: {
  msg: Msg; decisions: DecisionMap; thinking: boolean;
  apply: (p: Proposal) => void;
  decide: (id: string, status: "applied" | "skipped" | "error", detail?: string) => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="turn user">
        <div className="turn-who">you</div>
        <div className="bubble-text">{textOf(msg)}</div>
      </div>
    );
  }

  // group runs of tool calls so they collapse together
  const blocks: ({ kind: "tools"; tools: Extract<Part, { type: "tool" }>[] } | { kind: "part"; part: Part })[] = [];
  for (const p of msg.parts) {
    const last = blocks[blocks.length - 1];
    if (p.type === "tool" && last && last.kind === "tools") last.tools.push(p);
    else if (p.type === "tool") blocks.push({ kind: "tools", tools: [p] });
    else blocks.push({ kind: "part", part: p });
  }

  const copyable = msg.parts.filter((p) => p.type === "text").map((p) => (p as { text: string }).text).join("");

  return (
    <div className="turn assistant">
      <div className="turn-who">
        tares
        {copyable && (
          <button className="turn-copy" title="copy this answer"
                  onClick={() => navigator.clipboard?.writeText(copyable)}>copy</button>
        )}
      </div>
      {blocks.map((b, j) => b.kind === "tools"
        ? <ToolRun key={j} tools={b.tools} />
        : b.part.type === "proposal"
        ? <ProposalCard key={j} proposal={b.part.proposal} decision={decisions[b.part.proposal.id]}
                        onApply={() => apply((b.part as { proposal: Proposal }).proposal)}
                        onSkip={() => decide((b.part as { proposal: Proposal }).proposal.id, "skipped")} />
        : <div key={j} className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{(b.part as { text: string }).text}</ReactMarkdown></div>)}
      {thinking && <div className="dim">thinking…</div>}
    </div>
  );
});

/** A run of tool calls as a pipeline: one node per call, with a state dot, the tool, how long it
 *  took, and its input/output behind a click. Borrowed from the chat prototype, and worth it —
 *  a fold reading "2 steps" told you nothing while it was happening, so a read that takes four
 *  seconds looked exactly like a hung one. */
export function ToolRun({ tools }: { tools: ToolPart[] }) {
  return (
    <div className="rail">
      {tools.map((t) => <ToolNode key={t.id} tool={t} />)}
    </div>
  );
}

function ToolNode({ tool }: { tool: ToolPart }) {
  const [open, setOpen] = useState(false);
  const done = tool.ms !== undefined;
  const state = !done ? "running" : tool.ok === false ? "fail" : "done";
  const args = compact(tool.input);
  return (
    <div className={"node" + (open ? " open" : "")} data-state={state}>
      <button className="node-head" onClick={() => setOpen((o) => !o)}>
        <span className="node-tool mono">{tool.name}</span>
        <span className="node-label">
          {!done ? "running…" : tool.ok === false ? "failed" : args || "done"}
        </span>
        {done && <span className="node-time mono">{fmtMs(tool.ms!)}</span>}
      </button>
      {open && (
        <div className="node-body">
          <div className="kv"><b>input</b> <span className="mono">{args || "{}"}</span></div>
          {tool.preview !== undefined && (
            <div className="kv"><b>output</b> <span className="mono">{tool.preview || "(empty)"}</span></div>
          )}
        </div>
      )}
    </div>
  );
}

const fmtMs = (ms: number) => (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`);

/** The key is stored on the SERVER, under Settings — the same one Slack and trigger-woken agents
 *  resolve. It used to live in this browser's localStorage and ride along as a header, so a key
 *  added here made Ask work while Slack still reported no key configured (NF-125). */
export function KeySetup({ onSaved }: { onSaved: () => void }) {
  const [value, setValue] = useState("");
  const [err, setErr] = useState<string>();
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true); setErr(undefined);
    try { await api.setAnthropicKey(value.trim()); onSaved(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    finally { setSaving(false); }
  }

  return (
    <div className="panel" style={{ maxWidth: 560 }}>
      <h2 style={{ marginTop: 0 }}>Add your Anthropic API key</h2>
      <p className="help" style={{ whiteSpace: "normal" }}>
        The assistant runs on your Tares daemon using this key. It is stored on this instance and
        used by everything that reasons over your data: this assistant, Tares agents woken by
        triggers, and <span className="mono">/tares ask</span> in Slack. You can change or remove it
        later under <strong>Settings</strong>. Get one at{" "}
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a>.
      </p>
      {err && <div className="alert error">{err}</div>}
      <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) save(); }}>
        <input type="password" placeholder="sk-ant-…" value={value}
               onChange={(e) => setValue(e.target.value)}
               style={{ width: "100%", boxSizing: "border-box", padding: "0.5rem 0.7rem" }} autoFocus />
        <div className="btnrow" style={{ marginTop: 12 }}>
          <button className="primary" disabled={!value.trim() || saving}>
            {saving ? "saving…" : "Save key"}
          </button>
        </div>
      </form>
    </div>
  );
}
