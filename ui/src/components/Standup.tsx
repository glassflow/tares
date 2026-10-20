import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import type { Standup } from "../types";
import { CopyButton } from "./bits";
import { Close } from "./icons";
import { SlackPick } from "./SlackPick";

// The crew's standup (Crew page): what moved in each handed-over project over a range, read from
// the ledger, with a line or two per project from the cell's model on top. The facts show first;
// the summary fills in when the model answers. Copy takes the markdown; Send posts it to Slack.

type Range = "day" | "week" | "custom";

const CHANNEL_KEY = "tares.standup.channel";

/** An ISO time with the browser's own offset, so the report's dates read in the person's zone. */
function localIso(d: Date): string {
  const pad = (n: number) => String(Math.trunc(Math.abs(n))).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    + `${off >= 0 ? "+" : "-"}${pad(off / 60)}:${pad(off % 60)}`;
}

function midnight(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Midnight of the last working day before today: Friday on a Saturday, Sunday or Monday. */
function lastWorkday(now: Date): Date {
  const back = { 0: 2, 1: 3, 6: 1 }[now.getDay()] ?? 1;
  const d = midnight(now);
  d.setDate(d.getDate() - back);
  return d;
}

function dayInput(d: Date): string {
  return localIso(d).slice(0, 10);
}

function fromInput(v: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

function rangeOf(range: Range, from: string, to: string): { since: string; until: string } | null {
  const now = new Date();
  if (range === "day") return { since: localIso(lastWorkday(now)), until: localIso(now) };
  if (range === "week") return { since: localIso(new Date(now.getTime() - 7 * 86400000)), until: localIso(now) };
  const a = fromInput(from), b = fromInput(to);
  if (!a || !b || a > b) return null;
  b.setDate(b.getDate() + 1);
  return { since: localIso(a), until: localIso(b > now ? now : b) };
}

function readChannel(): string {
  try { return localStorage.getItem(CHANNEL_KEY) ?? ""; } catch { return ""; }
}

export default function StandupSheet({ onClose }: { onClose: () => void }) {
  const today = new Date();
  const [range, setRange] = useState<Range>("day");
  const [from, setFrom] = useState(() => dayInput(new Date(today.getTime() - 7 * 86400000)));
  const [to, setTo] = useState(() => dayInput(today));
  const [data, setData] = useState<Standup | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [writing, setWriting] = useState(false);
  const [channel, setChannel] = useState(readChannel);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const asked = useRef(0);

  const span = rangeOf(range, from, to);
  const key = span ? `${span.since}|${span.until}` : "";
  // the day range moves with the clock; only a change of choice asks again
  const ask = range === "day" || range === "week" ? range : key;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!span) return;
    const n = ++asked.current;
    setData(null); setError(null); setSent(null); setSendErr(null); setWriting(false);
    api.standup(span.since, span.until).then((r) => {
      if (n !== asked.current) return;
      setData(r);
      if (r.projects.length === 0) return;
      setWriting(true);
      api.standupSummary(span.since, span.until)
        .then((s) => { if (n === asked.current) setData(s); })
        .catch((e) => { if (n === asked.current) setData({ ...r, summary_error: String((e as Error).message ?? e) }); })
        .finally(() => { if (n === asked.current) setWriting(false); });
    }).catch((e) => { if (n === asked.current) setError(String((e as Error).message ?? e)); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ask]);

  const pickChannel = (id: string) => {
    setChannel(id); setSent(null); setSendErr(null);
    try { localStorage.setItem(CHANNEL_KEY, id); } catch { /* remembered for this visit only */ }
  };

  const send = async () => {
    if (!data || !channel) return;
    setSending(true); setSent(null); setSendErr(null);
    try { await api.slackPost(channel, data.markdown); setSent(channel); }
    catch (e) { setSendErr(String((e as Error).message ?? e)); }
    finally { setSending(false); }
  };

  const dayLabel = [0, 1, 6].includes(today.getDay()) ? "Since Friday" : "Since yesterday";

  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet standup" role="dialog" aria-modal="true" aria-label="Standup">
        <div className="sheet-head">
          <div className="sheet-title"><h2>Standup</h2></div>
          <button className="sheet-close" onClick={onClose} aria-label="Close"><Close /></button>
        </div>
        <div className="sheet-body">
          <div className="standup-range" role="group" aria-label="Range">
            {([["day", dayLabel], ["week", "Last 7 days"], ["custom", "Custom"]] as [Range, string][]).map(([r, label]) => (
              <button key={r} type="button" className={range === r ? "active" : undefined}
                      aria-pressed={range === r} onClick={() => setRange(r)}>{label}</button>
            ))}
          </div>
          {range === "custom" && (
            <div className="standup-dates">
              <label>From <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></label>
              <label>To <input type="date" value={to} min={from} max={dayInput(today)} onChange={(e) => setTo(e.target.value)} /></label>
            </div>
          )}

          {!span ? <p className="fo-warn">Pick a start date on or before the end date.</p>
            : error ? <p className="fo-warn">{error}</p>
            : !data ? <p className="dim">Reading what the crew did…</p>
            : (
              <>
                {writing && <p className="dim standup-note">Writing the summary…</p>}
                {!writing && data.summary_error && (
                  <p className="dim standup-note">No summary this time: {data.summary_error}. The facts below are complete.</p>
                )}
                <div className="md standup-report">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}
                    components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>
                    {data.markdown}
                  </ReactMarkdown>
                </div>
              </>
            )}
        </div>
        <div className="sheet-foot standup-foot">
          <CopyButton text={data?.markdown ?? ""} label="Copy" what="the standup" primary
                      className={data ? undefined : "standup-wait"} />
          <div className="standup-slack">
            <SlackPick value={channel} onChange={pickChannel} emptyLabel="Pick a channel" />
            <div className="btnrow">
              <button type="button" disabled={!data || !channel || sending || writing} onClick={send}>
                {sending ? "Sending…" : "Send to Slack"}
              </button>
              {sent && <span className="dim">Sent.</span>}
            </div>
            {sendErr && <p className="fo-warn" style={{ margin: 0 }}>{sendErr}</p>}
          </div>
        </div>
      </div>
    </>
  );
}
