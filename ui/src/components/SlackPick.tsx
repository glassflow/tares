import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, type SlackChannel, type SlackChannels } from "../api";

// ── the channel list, shared by every picker on screen ─────────────────────
// One read for the whole page however many pickers it shows (the plan step has one per agent).
// While any picker is mounted the list is re-read every few seconds, when the window regains
// focus, and when a dropdown opens. The cell caches it and drops the cache when Slack says a
// channel changed, so a channel Tares was just added to shows up within one poll.
const POLL_MS = 5000;
let shared: SlackChannels | undefined;
// A failed poll never replaces a good list already on screen: the open dropdown, its search text
// and the choice stay put, and the next poll tries again. The failure is said only when there has
// never been a good list. Not connected and a missing scope are answers, not failures: they show.
let inflight: Promise<void> | null = null;
let timer: ReturnType<typeof setInterval> | undefined;
const listeners = new Set<(s: SlackChannels) => void>();

function readChannels(): Promise<void> {
  if (inflight) return inflight;
  inflight = api.slackChannels()
    .then((s) => s, (e) => ({ channels: [], reason: "error", detail: String((e as Error).message ?? e) }) as SlackChannels)
    .then((s) => {
      if (s.reason === "error" && shared?.reason === null) s = { ...shared, stale: true, detail: s.detail };
      shared = s; listeners.forEach((fn) => fn(s));
    })
    .finally(() => { inflight = null; });
  return inflight;
}

const onFocus = () => { void readChannels(); };

function useSlackChannels(): { slack: SlackChannels | undefined; reload: () => void } {
  const [slack, setSlack] = useState<SlackChannels | undefined>(shared);
  useEffect(() => {
    listeners.add(setSlack);
    if (listeners.size === 1) {
      timer = setInterval(() => { if (!document.hidden) void readChannels(); }, POLL_MS);
      window.addEventListener("focus", onFocus);
    }
    void readChannels();
    return () => {
      listeners.delete(setSlack);
      if (!listeners.size) {
        clearInterval(timer);
        window.removeEventListener("focus", onFocus);
      }
    };
  }, []);
  return { slack, reload: () => { void readChannels(); } };
}

const label = (c: SlackChannel) => (c.is_private ? `🔒 ${c.name}` : `#${c.name}`);

/** The channel a saved value names: its ID, or a name (`#name` or `name`, from slack://channel/<name>). */
function findChannel(channels: SlackChannel[], value: string): SlackChannel | undefined {
  const v = value.trim().replace(/^#/, "");
  return v ? channels.find((c) => c.id === v || c.name === v) : undefined;
}

/** The Slack channel something posts to: a searchable list of every public channel and the private
 *  ones Tares is in. Never a box to type an ID into; when there is nothing to pick from it says why
 *  and what to do. Emits the channel's ID. A saved value (an ID or a name) shows by its name when
 *  it is in the list, and as itself when it is not. */
export function SlackPick({ value, onChange, disabled, emptyLabel = "Pick a channel" }: {
  value: string; onChange: (id: string) => void; disabled?: boolean;
  emptyLabel?: string;   // what no channel means here ("Pick a channel", or "No channel: console only")
}) {
  const { slack, reload } = useSlackChannels();
  return (
    <div className="slack-pick">
      <SlackPickBody slack={slack} reload={reload} value={value} onChange={onChange}
                     disabled={disabled} emptyLabel={emptyLabel} />
      <span className="help">
        Private channels show up here once you add Tares to them: in the channel, type <code>/invite @Tares</code>.
      </span>
    </div>
  );
}

function SlackPickBody({ slack, reload, value, onChange, disabled, emptyLabel }: {
  slack: SlackChannels | undefined; reload: () => void;
  value: string; onChange: (id: string) => void; disabled?: boolean; emptyLabel: string;
}) {
  const saved = value ? <> It is set to <span className="mono">{value}</span> for now.</> : null;
  if (!slack) return <p className="help">Reading your Slack channels…</p>;
  if (slack.reason === "no_token") {
    return (
      <p className="help">
        Slack is not connected to Tares yet. <Link to="/settings?tab=slack">Connect Slack under Settings, Slack</Link>, then pick the channel here.{saved}
      </p>
    );
  }
  if (slack.reason === "missing_scope") {
    return (
      <p className="help">
        Tares cannot list your Slack channels: the connection is missing a permission it needs.{" "}
        <Link to="/settings?tab=slack">Reconnect Slack under Settings, Slack</Link> to list them.{saved}
      </p>
    );
  }
  if (slack.reason === "error") {
    return (
      <p className="help">
        Slack did not answer when Tares asked for your channels{slack.detail ? ` (${slack.detail})` : ""}.{" "}
        <button type="button" className="linklike" onClick={reload}>Try again</button>{saved}
      </p>
    );
  }
  if (!slack.channels.length) {
    return (
      <p className="help">
        Your Slack has no channel Tares can see yet. Create one, or add Tares to a private one, and it shows up here.{saved}
      </p>
    );
  }
  return <ChannelCombo channels={slack.channels} value={value} onChange={onChange}
                       disabled={disabled} emptyLabel={emptyLabel} onOpen={reload} />;
}

/** The dropdown: a button showing the choice, and a list with a search box on top. */
function ChannelCombo({ channels, value, onChange, disabled, emptyLabel, onOpen }: {
  channels: SlackChannel[]; value: string; onChange: (id: string) => void; disabled?: boolean;
  emptyLabel: string; onOpen: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hi, setHi] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);

  const current = findChannel(channels, value);
  const shownValue = current ? label(current) : value ? `${value} (not in the list)` : emptyLabel;
  const needle = q.trim().toLowerCase().replace(/^#/, "");
  // "" (no channel) first, then the channels that match; a saved value not in the list stays pickable
  const items: { id: string; text: string }[] = [
    ...(needle ? [] : [{ id: "", text: emptyLabel }]),
    ...(value && !current && !needle ? [{ id: value, text: `${value} (not in the list)` }] : []),
    ...channels.filter((c) => !needle || c.name.toLowerCase().includes(needle))
      .map((c) => ({ id: c.id, text: label(c) })),
  ];
  const selectedId = current ? current.id : value;

  useEffect(() => { if (open) search.current?.focus(); }, [open]);

  const show = () => {
    setQ(""); setHi(Math.max(0, items.findIndex((i) => i.id === selectedId))); setOpen(true); onOpen();
  };
  const pick = (id: string) => { onChange(id); setOpen(false); };
  const keys = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { setOpen(false); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (items[hi]) pick(items[hi].id); }
  };

  return (
    <div className="combo picker" ref={root}
         onBlur={(e) => { if (!root.current?.contains(e.relatedTarget as Node | null)) setOpen(false); }}>
      <button type="button" className="picker-btn" disabled={disabled} aria-label="Slack channel"
              aria-haspopup="listbox" aria-expanded={open}
              onClick={() => (open ? setOpen(false) : show())}
              onKeyDown={(e) => {
                if (!open && ["Enter", " ", "ArrowDown", "ArrowUp"].includes(e.key)) { e.preventDefault(); show(); }
              }}>
        <span className="picker-value">{shownValue}</span>
        <svg className="picker-caret" width="10" height="7" viewBox="0 0 10 7" fill="none" aria-hidden="true">
          <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <div className="combo-list slack-pick-list">
          <input ref={search} type="text" className="slack-pick-search" placeholder="Search channels"
                 aria-label="Search Slack channels" autoComplete="off" data-1p-ignore data-lpignore="true"
                 value={q} onChange={(e) => { setQ(e.target.value); setHi(0); }} onKeyDown={keys} />
          <div role="listbox" aria-label="Slack channels">
            {items.map((it, i) => (
              <div key={it.id || "-"} role="option" aria-selected={it.id === selectedId}
                   className={"combo-item" + (i === hi ? " active" : "")}
                   onMouseDown={(e) => { e.preventDefault(); pick(it.id); }}
                   onMouseEnter={() => setHi(i)}>
                {it.text}
              </div>
            ))}
            {!items.length && <div className="combo-item help">No channel matches “{q.trim()}”.</div>}
          </div>
        </div>
      )}
    </div>
  );
}
