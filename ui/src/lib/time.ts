// The console's one home for time (TR-1261). Two grammars, as .impeccable.md sets them:
//
//   an instant   a UTC stamp, "2026-11-12 14:05Z" (formatStampUTC), with "3h ago" in its tooltip
//                (formatDistance). Edition sentences say it in words: "14:05", "yesterday, 14:05".
//   a length     in words, the same ladder the server writes (tares/goal.py span_words, age_words):
//                "less than a minute", "45 minutes", "3 days"; "just now" and "8 minutes ago".
//
// Everything here is pure, locale-free and reads UTC, so two lines on one page never disagree and
// the viewer's zone never renames a day. `npm run design-contract` fails on toLocale*String and
// Intl.DateTimeFormat anywhere else (the time selector); grouped numbers live in lib/number.ts.
// The stamp ladder (formatStampUTC, formatRelative, formatUntil, formatDistance) is argus-ui's
// src/lib/time.ts, so Tares and Rius write an instant the same way.

export const MIN = 60_000;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const pad = (n: number, w = 2) => String(n).padStart(w, "0");
const toMs = (value: string | number) => (typeof value === "number" ? value : Date.parse(value));

// --- an instant, as a stamp ---------------------------------------------------

/** How much of the clock a stamp carries: lists read to the minute, a log line to the second. */
export type StampPrecision = "minute" | "second" | "ms";

/** "2026-07-03 11:57Z". A value that does not parse passes through untouched, so a bad wire value
 *  is visible rather than silently reformatted. */
export function formatStampUTC(value: string | number, precision: StampPrecision = "minute"): string {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) return String(value);
  const d = new Date(ms);
  let clock = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  if (precision !== "minute") clock += `:${pad(d.getUTCSeconds())}`;
  if (precision === "ms") clock += `.${pad(d.getUTCMilliseconds(), 3)}`;
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${clock}Z`;
}

/** "2mo ago": the backward ladder for a stamp's tooltip. Under a minute ahead is clock skew and
 *  reads "just now"; further ahead reads "in the future", because that is a data problem. A bad
 *  time gives "". */
export function formatRelative(from: number, now: number): string {
  if (!Number.isFinite(from)) return "";
  const sec = Math.round((now - from) / 1000);
  if (sec < -60) return "in the future";
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.floor(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

/** "in 27d": the forward twin. Rounds up, so a 30-day expiry reads "in 30d" the moment it lands,
 *  and days run to 59 before months take over. */
export function formatUntil(to: number, now: number): string {
  if (!Number.isFinite(to)) return "";
  const sec = Math.ceil((to - now) / 1000);
  if (sec <= 0) return "now";
  const min = Math.ceil(sec / 60);
  if (min < 60) return `in ${min}m`;
  const hr = Math.ceil(min / 60);
  if (hr < 24) return `in ${hr}h`;
  const day = Math.ceil(hr / 24);
  if (day < 60) return `in ${day}d`;
  if (day < 365) return `in ${Math.round(day / 30)}mo`;
  return `in ${Math.round(day / 365)}y`;
}

/** The tooltip phrase for any instant: past "2mo ago", future "in 27d". */
export function formatDistance(ts: number, now: number): string {
  return ts - now > MIN ? formatUntil(ts, now) : formatRelative(ts, now);
}

/** "Nov 12, 2026": a calendar day, UTC. A value that does not parse passes through untouched. */
export function formatDayUTC(value: string | number): string {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) return String(value);
  const d = new Date(ms);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
}

// --- an instant, in a sentence --------------------------------------------------

/** "14:20", UTC. A missing or bad time gives "", never "NaN". */
export const clock = (ms: number) => (Number.isFinite(ms) ? new Date(ms).toISOString().slice(11, 16) : "");

const daysBetween = (ms: number, now: number) => Math.floor(now / DAY) - Math.floor(ms / DAY);

/** "27 Oct, 14:05": a date the other helpers do not name. A missing or bad date gives "". */
export function dateWords(ms: number) {
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${clock(ms)}`;
}

/** "14:05" today, "yesterday, 14:05", else "27 Oct, 14:05". A missing or bad date gives "". */
export function sinceWords(ms: number, now: number) {
  if (!Number.isFinite(ms)) return "";
  const gap = daysBetween(ms, now);
  if (gap <= 0) return clock(ms);
  if (gap === 1) return `yesterday, ${clock(ms)}`;
  return dateWords(ms);
}

/** "today 14:11", "yesterday 14:11", "Fri 12:40" within the last week, else `far`. */
export function stamp(ms: number, now: number, far: string) {
  if (!Number.isFinite(ms)) return far;
  const gap = daysBetween(ms, now);
  if (gap <= 0) return `today ${clock(ms)}`;
  if (gap === 1) return `yesterday ${clock(ms)}`;
  if (gap < 7) return `${WEEKDAYS[new Date(ms).getUTCDay()]} ${clock(ms)}`;
  return far;
}

// --- a length of time, in words -------------------------------------------------

/** A rough length, one unit: "less than a minute", "45 minutes", "3 days" ("a while" when the
 *  length is missing or bad). Same as the server's span_words. */
export function spanWords(ms: number) {
  if (!Number.isFinite(ms)) return "a while";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return "less than a minute";
  if (s < 3600) return plural(Math.floor(s / 60), "minute");
  if (s < 86400) return plural(Math.floor(s / 3600), "hour");
  return plural(Math.floor(s / 86400), "day");
}

/** How long ago: "just now" under a minute, else "8 minutes ago". Same as the server's age_words.
 *  A time ahead of now (clock skew) reads "just now". */
export const ageWords = (ms: number) => (ms < MIN ? "just now" : `${spanWords(ms)} ago`);

/** A length with its second unit: "12 minutes", "1 hour 4 minutes", "2 days 3 hours". */
export function durationWords(ms: number) {
  if (!Number.isFinite(ms)) return "a while";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return "less than a minute";
  const m = Math.floor(s / 60);
  if (m < 60) return plural(m, "minute");
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${plural(h, "hour")} ${plural(m % 60, "minute")}` : plural(h, "hour");
  const d = Math.floor(h / 24);
  return h % 24 ? `${plural(d, "day")} ${plural(h % 24, "hour")}` : plural(d, "day");
}

// --- the viewer's zone ----------------------------------------------------------

/** The viewer's IANA zone ("Europe/Belgrade"), for a setting that asks for one. Display never uses
 *  it: every time on screen reads UTC. */
export const viewerZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
