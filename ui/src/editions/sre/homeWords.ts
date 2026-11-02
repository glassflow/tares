// Small wording helpers for the AI SRE home page. Times the cell words itself (the headline, the
// "Posted to" line) are UTC, so these are UTC too.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const pad = (n: number) => String(n).padStart(2, "0");

/** "at 14:09" for a time today (UTC), "on 29 Oct, 03:00" for an earlier day. Empty when the time does not parse. */
export function atWords(iso: string, now = Date.now()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const today = new Date(now);
  const same = d.getUTCFullYear() === today.getUTCFullYear() && d.getUTCMonth() === today.getUTCMonth() && d.getUTCDate() === today.getUTCDate();
  return same ? `at ${time}` : `on ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${time}`;
}

/** "6 days ago", "3 minutes ago". */
export function agoWords(iso: string, now = Date.now()): string {
  const ms = now - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "";
  const m = Math.max(0, Math.round(ms / 60_000));
  const [n, unit] = m < 60 ? [m, "minute"] : m < 1440 ? [Math.round(m / 60), "hour"] : [Math.round(m / 1440), "day"];
  if (m < 1) return "just now";
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

/** "$0.005", "$0.01", "$1.25": at least two decimals, more only when they are not zero. */
export function money(n: number): string {
  let s = n.toFixed(4).replace(/0+$/, "");
  const dec = s.split(".")[1] ?? "";
  if (dec.length < 2) s = n.toFixed(2);
  return `$${s}`;
}

export const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en")} ${n === 1 ? one : many}`;
