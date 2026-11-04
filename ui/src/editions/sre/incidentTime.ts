// The words for times on the Incidents screens. The cell does not know the viewer's zone and writes
// its own times in UTC ("since 14:20"), so these read UTC too and a line never disagrees with another.

const MIN = 60_000;
const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** "14:20" */
export const clock = (ms: number) => (Number.isFinite(ms) ? new Date(ms).toISOString().slice(11, 16) : "");

/** A rough length of time, as the cell says it ("a while" when the time is missing or bad): "less than a minute", "45 minutes", "3 days". */
export function spanWords(ms: number) {
  if (!Number.isFinite(ms)) return "a while";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return "less than a minute";
  if (s < 3600) return plural(Math.floor(s / 60), "minute");
  if (s < 86400) return plural(Math.floor(s / 3600), "hour");
  return plural(Math.floor(s / 86400), "day");
}

export const ageWords = (ms: number) => (ms < MIN ? "just now" : `${spanWords(ms)} ago`);

/** A length of time with its second unit: "1 hour 4 minutes", "2 days 3 hours", "12 minutes". */
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

const daysBetween = (ms: number, now: number) => Math.floor(now / DAY) - Math.floor(ms / DAY);

/** "14:05" for today, "yesterday, 14:05", else "27 Oct, 14:05" (the cell's own "since" words). A missing or
 *  bad date gives "". */
export function sinceWords(ms: number, now: number) {
  if (!Number.isFinite(ms)) return "";
  const gap = daysBetween(ms, now);
  if (gap <= 0) return clock(ms);
  if (gap === 1) return `yesterday, ${clock(ms)}`;
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${clock(ms)}`;
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

/** "27 Oct, 14:05": a date the stamp does not name. A missing or bad date gives "", never "NaN". */
export function dateWords(ms: number) {
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${clock(ms)}`;
}

/** Sentence case: the cell writes "open 38 minutes, nobody on it" and the row starts a sentence. */
export const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
