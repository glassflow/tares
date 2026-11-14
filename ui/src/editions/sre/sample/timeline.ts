// Sample data for the Timeline page, shaped exactly like GET /api/sre/timeline and
// GET /api/sre/timeline/raw, with the words the cell writes. Only `?sample=<name>` shows it, and
// only in a browser with the sample flag set. Every name here is invented. The clock is the
// sample's own "now", so the hour headings read the same on any day.
//
//   normal   the last 6 hours, all services, an incident after a deploy
//   raw      the same, with the lines of the 15:00 window open
//   service  one service, the last hour
//   kinds    only alerts and deploys, over 24 hours
//   empty    a service with nothing in the range
//   load     the first read has not come back
//   err      the first read failed
//   rawerr   the lines of a window did not load
//   long     a very long log line in the open lines
//   many     a full page of 200 items, with an older page to load

import type { RangeKey, TimelineData, TimelineItem, TimelineKind, TimelineRawData, TimelineRawRow, TimelineTone } from "../Timeline.api";

export const SAMPLE_NAMES = ["normal", "raw", "service", "kinds", "empty", "load", "err", "rawerr", "long", "many"] as const;
export type SampleName = (typeof SAMPLE_NAMES)[number];
export const isSample = (n: string | null): n is SampleName => !!n && (SAMPLE_NAMES as readonly string[]).includes(n);

/** The moment the samples are read at. */
export const SAMPLE_NOW = "2026-11-02T15:16:00+00:00";

/** An ISO time in the sample's day: `back` days before the 2nd, at h:m:s UTC. */
const iso = (back: number, h: number, m: number, s = 0) =>
  new Date(Date.UTC(2026, 10, 2 - back, h, m, s)).toISOString().replace(".000Z", "+00:00");
const clock = (t: string) => t.slice(11, 16);
const words = (at: string, end: string | null) => (end ? `${clock(at)} to ${clock(end)}` : clock(at));

const SERVICES = ["api-server", "docs-site", "edge-proxy", "service-a"];
const WINDOW = { from: iso(0, 15, 0), to: iso(0, 15, 10), count: 214 };
const WINDOW_ID = `win:edge-proxy:${WINDOW.from}`;

function item(kind: TimelineKind, id: string, at: string, end: string | null, service: string, text: string, tone: TimelineTone,
              extra: { link?: string; raw?: { from: string; to: string; count: number } } = {}): TimelineItem {
  return { id, kind, at, end, time_words: words(at, end), service, text, tone, link: extra.link ?? null, raw: extra.raw ?? null };
}

// ── the story: a deploy of edge-proxy at 14:52, errors rise, an incident opens at 15:10, Alex takes it
const TITLE = "edge-proxy errors rose after the 14:52 deploy";
const LINK = "/incidents/inc-sample-1";
const OPENED = item("incident", "inc:inc-sample-1:opened", iso(0, 15, 10), null, "edge-proxy", `Incident opened: ${TITLE}`, "bad", { link: LINK });
const ON_IT = item("incident", "inc:inc-sample-1:on_it", iso(0, 15, 14), null, "edge-proxy", `Alex is on it: ${TITLE}`, "warn", { link: LINK });
const WIN_NOW = item("window", WINDOW_ID, WINDOW.from, WINDOW.to, "edge-proxy",
  "edge-proxy: 214 events, 41 errors, errors up 10x on the 10 minutes before", "bad", { raw: WINDOW });
const WIN_BEFORE = item("window", `win:edge-proxy:${iso(0, 14, 50)}`, iso(0, 14, 50), iso(0, 15, 0), "edge-proxy",
  "edge-proxy: 61 events, 4 errors, errors up 2x on the 10 minutes before", "warn",
  { raw: { from: iso(0, 14, 50), to: iso(0, 15, 0), count: 61 } });
const DEPLOY = item("deploy", `deploy:example-org/edge-proxy:${iso(0, 14, 52)}:push:edge-proxy`, iso(0, 14, 52), null, "edge-proxy",
  "example-org/edge-proxy pushed to main by Kim: 3 commits", "info");
const ALERT = item("alert", `alert:alerts-prod:${iso(0, 15, 0)}:HighErrorRate:api-server`, iso(0, 15, 0), null, "api-server",
  "Alert HighErrorRate is active on api-server (severity page)", "bad");
const CHECKS = item("check", "checks:chk-sample-1", iso(0, 14, 20), iso(0, 14, 50), "", "6 checks, all quiet", "ok");
const METRIC = item("metric", `metric:api-server:latency_p95:${iso(0, 14, 0)}`, iso(0, 14, 0), null, "api-server",
  "api-server: latency_p95 820 ms, up from 210 ms", "info");
const QUIET_EDGE = item("quiet", `quiet:edge-proxy:${iso(0, 13, 0)}`, iso(0, 13, 0), iso(0, 14, 50), "edge-proxy",
  "edge-proxy: normal from 13:00 to 14:50", "ok", { raw: { from: iso(0, 13, 0), to: iso(0, 14, 50), count: 18420 } });
const QUIET_A = item("quiet", `quiet:service-a:${iso(0, 13, 0)}`, iso(0, 13, 0), iso(0, 13, 50), "service-a",
  "service-a: normal from 13:00 to 13:50", "ok", { raw: { from: iso(0, 13, 0), to: iso(0, 13, 50), count: 4310 } });
const ALERT_RESOLVED = item("alert", `alert:alerts-prod:${iso(1, 17, 40)}:HighErrorRate:api-server`, iso(1, 17, 40), null, "api-server",
  "Alert HighErrorRate on api-server resolved", "ok");

const NORMAL: TimelineItem[] = [ON_IT, OPENED, WIN_NOW, ALERT, WIN_BEFORE, DEPLOY, CHECKS, QUIET_EDGE, METRIC, QUIET_A];

const data = (range_words: string, items: TimelineItem[], extra: Partial<TimelineData> = {}): TimelineData =>
  ({ range_words, services: SERVICES, items, next_before: null, empty_words: null, ...extra });

// ── many: 20 hours of ten items, newest first inside every hour; an older page behind it
const SLOTS_FULL = [57, 52, 46, 40, 34, 28, 22, 16, 10, 4];
const SLOTS_NOW = [15, 14, 13, 11, 9, 8, 6, 4, 2, 1];
const TOPICS = ["Update answer page", "Fix retry budget", "Add search index", "Tidy build cache", "Rename config key"];

let pr = 413;
function hourItems(back: number, hour: number, minutes: number[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  minutes.forEach((m, i) => {
    const end = iso(back, hour, m);
    const key = `${back}-${hour}-${i}`;
    const n = back * 24 + (24 - hour);
    switch (i) {
      case 0: case 5: {
        const svc = i === 0 ? "service-a" : "edge-proxy";
        const start = iso(back, hour, m - 10 < 0 ? 0 : m - 10);
        const raw = { from: start, to: end, count: 1100 + ((n * 37) % 400) };
        const k = n % 3;
        const text = k === 0 ? `${svc}: ${raw.count.toLocaleString("en-US")} events, 18 errors, errors up 3x on the 10 minutes before`
          : k === 1 ? `${svc}: ${raw.count.toLocaleString("en-US")} events, 2 errors`
          : `${svc}: ${raw.count.toLocaleString("en-US")} events, events up 2.2x on the 10 minutes before`;
        out.push(item("window", `win:${key}`, start, end, svc, text, k === 0 ? "bad" : k === 1 ? "warn" : "info", { raw }));
        break;
      }
      case 1: case 6:
        out.push(item("check", `checks:${key}`, iso(back, hour, m > 12 ? m - 12 : 0), end, "", `${3 + ((n + i) % 5)} checks, all quiet`, "ok"));
        break;
      case 2: case 7: {
        const svc = i === 2 ? "docs-site" : "service-a";
        const start = iso(back, hour, 0);
        out.push(item("quiet", `quiet:${key}`, start, end, svc, `${svc}: normal from ${clock(start)} to ${clock(end)}`, "ok",
          { raw: { from: start, to: end, count: 900 + ((n * 53) % 500) } }));
        break;
      }
      case 3: case 8: {
        const repo = i === 3 ? "docs-site" : "service-a";
        const text = `example-org/${repo} merged: ${TOPICS[pr % TOPICS.length]} (#${pr})`;
        pr -= 1;
        out.push(item("deploy", `deploy:${key}`, end, null, repo, text, "info"));
        break;
      }
      case 4:
        out.push(item("alert", `alert:${key}`, end, null, "edge-proxy", "Alert HighLatency is active on edge-proxy (severity ticket)", "warn"));
        break;
      default:
        out.push(item("alert", `alert:${key}`, end, null, "api-server", "Alert DiskFilling is pending on api-server", "warn"));
    }
  });
  return out;
}

const endOf = (it: TimelineItem) => Date.parse(it.end ?? it.at);
const newestFirst = (a: TimelineItem[]) => [...a].sort((x, y) => endOf(y) - endOf(x));

function manyPages() {
  pr = 413;
  const first: TimelineItem[] = [];
  // today 15:00 down to 00:00, then yesterday 23:00 down to 20:00: 20 hours
  for (let h = 15; h >= 0; h--) first.push(...newestFirst(hourItems(0, h, h === 15 ? SLOTS_NOW : SLOTS_FULL)));
  for (let h = 23; h >= 20; h--) first.push(...newestFirst(hourItems(1, h, SLOTS_FULL)));
  const older: TimelineItem[] = [];
  for (let h = 19; h >= 16; h--) older.push(...newestFirst(hourItems(1, h, SLOTS_FULL)));
  older.push(...newestFirst(hourItems(1, 15, SLOTS_FULL.filter((m) => m >= 16))));
  return { first, older };
}

let MANY: ReturnType<typeof manyPages> | null = null;
const many = () => (MANY ??= manyPages());

// ── raw lines
const BASE_ROWS: [string, string, Record<string, unknown>][] = [
  ["GET /checkout 502 upstream connect error", "502", { service: "edge-proxy", status: 502 }],
  ["GET /cart 200 18ms", "200", { service: "edge-proxy", status: 200 }],
  ["GET /checkout 502 upstream connect error", "502", { service: "edge-proxy", status: 502 }],
  ["GET /health 200 2ms", "200", { service: "edge-proxy", status: 200 }],
  ["GET /api/orders 200 41ms", "200", { service: "edge-proxy", status: 200 }],
  ["POST /checkout 502 upstream connect error", "502", { service: "edge-proxy", status: 502 }],
];
const LONG_UNIT = "upstream connect error or disconnect/reset before headers. reset reason: connection termination, retry_budget_exceeded=true, cluster=checkout-v2, request_id=0f9c1d2e-77aa-4b1b-9a10-ffee00112233, ";
const LONG_TEXT = LONG_UNIT.repeat(3).slice(0, 500);

function rawRow(i: number, text?: string, fields?: Record<string, unknown>): TimelineRawRow {
  const [t, , f] = BASE_ROWS[i % BASE_ROWS.length];
  // newest first, all inside 15:00 to 15:10
  const sec = 598 - Math.floor(i * 2.7);
  return { at: iso(0, 15, Math.floor(sec / 60), sec % 60), feed: "prod-otlp logs", text: text ?? t, fields: fields ?? f };
}

export function sampleOpen(name: SampleName): string | null {
  return name === "raw" || name === "long" || name === "rawerr" ? WINDOW_ID : null;
}

/** What the lines call returns for a sample. Asked for 20, the sample gives the first few, so the
 *  box shows its "Show 200 more"; asked for more, it gives that many up to all of them. */
export function sampleRaw(name: SampleName, limit: number): Promise<TimelineRawData> {
  if (name === "rawerr") return Promise.reject(new Error("The cell did not answer."));
  const count = WINDOW.count;
  if (name === "long") {
    const rows = [rawRow(0), rawRow(1), rawRow(2, LONG_TEXT, {})];
    return Promise.resolve({ rows, count, more: true });
  }
  const n = limit <= 20 ? 4 : Math.min(limit, count);
  return Promise.resolve({ rows: Array.from({ length: n }, (_, i) => rawRow(i)), count, more: count > n });
}

// ── the filter bar a sample starts from, when the address does not say
export function sampleQuery(name: SampleName): { service: string; range: RangeKey; kinds: string } {
  switch (name) {
    case "service": return { service: "edge-proxy", range: "1h", kinds: "" };
    case "empty": return { service: "edge-proxy", range: "6h", kinds: "" };
    case "kinds": return { service: "", range: "24h", kinds: "alerts,deploys" };
    case "many": return { service: "", range: "24h", kinds: "" };
    default: return { service: "", range: "6h", kinds: "" };
  }
}

const NEVER = new Promise<TimelineData>(() => {});

export function sampleLoad(name: SampleName): Promise<TimelineData> {
  switch (name) {
    case "load": return NEVER;
    case "err": return Promise.reject(new Error("The cell did not answer."));
    case "service":
      return Promise.resolve(data("Last hour", [ON_IT, OPENED, WIN_NOW, WIN_BEFORE, DEPLOY,
        item("quiet", `quiet:edge-proxy:${iso(0, 14, 16)}`, iso(0, 14, 16), iso(0, 14, 50), "edge-proxy",
          "edge-proxy: normal from 14:16 to 14:50", "ok", { raw: { from: iso(0, 14, 16), to: iso(0, 14, 50), count: 3380 } })]));
    case "kinds":
      return Promise.resolve(data("Last 24 hours", [ALERT, DEPLOY, ALERT_RESOLVED]));
    case "empty":
      return Promise.resolve(data("Last 6 hours", [], { empty_words: "Nothing arrived for edge-proxy in the last 6 hours." }));
    case "rawerr":
    case "long":
      return Promise.resolve(data("Last 6 hours", [WIN_NOW]));
    case "many":
      return Promise.resolve(data("Last 24 hours", many().first, { next_before: many().first[many().first.length - 1].at }));
    default:
      return Promise.resolve(data("Last 6 hours", NORMAL));
  }
}

/** The older page that "Load earlier" asks for in the `many` sample. */
export function sampleEarlier(): Promise<TimelineData> {
  return Promise.resolve(data("Last 24 hours", many().older));
}
