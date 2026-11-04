// Sample data for the Runbooks and memory page, shown at /runbooks?sample=<name> when the browser has
// the sample flag (see ../../sample.ts). It is shaped exactly like the API answer and invented:
// Alex, Kim, Lee, edge-proxy, service-a, docs-site.

import { ApiError } from "../../../api";
import type { RunbookLine, RunbookService, RunbooksData } from "../Runbooks.api";

const MAX_LINE = 500;
const SAME = "That line is already in this runbook.";
const EMPTY_WORDS =
  "No services yet. Once your connections send data, each service gets a page here for what the investigator should know.";
const NOT_SAVED = "The line was not saved. Nothing changed, try again.";

const linesWords = (n: number) => (n === 0 ? "No lines yet" : n === 1 ? "1 line" : `${n} lines`);
const line = (text: string, by: string, when_words: string): RunbookLine => ({ text, by, when_words });
const svc = (name: string, runbook: RunbookLine[], memory: RunbookLine[] = [], used_words: string | null = null): RunbookService =>
  ({ name, lines_words: linesWords(runbook.length), runbook, memory, used_words });

const EDGE: RunbookLine[] = [
  line("Check /health on the load balancer before restarting edge-proxy.", "Alex", "3 days ago"),
  line("The checkout-v2 cluster retries 3 times; a burst of 502s after a deploy is usually the retry budget.", "Kim", "2 weeks ago"),
  line("Rolling back is safe: the last 5 deploys had no migrations.", "Alex", "2 weeks ago"),
  line("Page the payments team only when 5xx stays above 2% for 10 minutes.", "you", "today"),
  line("Ignore the nightly 02:00 spike: it is the log rotation.", "Lee", "1 month ago"),
];
const EDGE_MEMORY: RunbookLine[] = [
  line("A restart of edge-proxy cleared a 502 burst on 28 Oct; the cause was a stale upstream list.", "Investigator", "5 days ago"),
  line("p95 latency above 800 ms usually follows a cache flush.", "Investigator", "3 weeks ago"),
];
const SERVICE_A_MEMORY: RunbookLine[] = [
  line("A restart of service-a cleared a burst of failed jobs on 28 Oct; the cause was a stale worker list.", "Investigator", "5 days ago"),
  line("Job latency above 800 ms usually follows a cache flush.", "Investigator", "3 weeks ago"),
];
const LONG_LINE =
  "The checkout service depends on the payments gateway, the inventory cache and the fraud scorer, and when any of the three slows " +
  "down the symptom is a burst of 502 errors from edge-proxy. Before restarting anything, check the gateway status page, then the " +
  "cache hit rate, then the scorer queue depth; if all three look normal, the cause is almost always a bad deploy of checkout-v2, " +
  "so roll it back first and investigate afterwards. Do not restart the cache during business hours.";   // 500 characters or fewer

const API_SERVER: RunbookLine[] = [
  line("Restart api-server one pod at a time, never all at once.", "Kim", "4 days ago"),
  line("Errors on /login after a deploy mean the session store is still warming up.", "Lee", "3 weeks ago"),
  line("Do not scale below 3 pods during business hours.", "Alex", "2 months ago"),
];
const CHECKOUT: RunbookLine[] = [
  line(LONG_LINE, "Kim", "1 week ago"),
  line("Roll back the last deploy first.", "Alex", "3 days ago"),
];

/** The five services of the mock. `serviceA` is what service-a holds: one line, or only memory. */
function five(serviceA: "line" | "memory"): RunbookService[] {
  return [
    svc("edge-proxy", EDGE, EDGE_MEMORY, "Read by the investigator 4 times this week"),
    svc("api-server", API_SERVER, [], "Read by the investigator 2 times this week"),
    svc("checkout", CHECKOUT, [], "Read by the investigator 12 times this week"),
    serviceA === "line"
      ? svc("service-a", [line("Restart the job runner before the failover.", "Lee", "yesterday")])
      : svc("service-a", [], SERVICE_A_MEMORY),
    svc("docs-site", []),
  ];
}

const GENERIC = [
  "Check /health on the load balancer before restarting.",
  "A burst of 502s after a deploy is usually the retry budget.",
  "Rolling back is safe: the last 5 deploys had no migrations.",
  "Page the payments team only when 5xx stays above 2% for 10 minutes.",
  "Ignore the nightly 02:00 spike: it is the log rotation.",
];
const WHO = ["Alex", "Kim", "Lee"];
const WHEN = ["3 days ago", "2 weeks ago", "1 month ago", "yesterday", "today"];

/** 50 services; every fourth has none, the rest 1 to 5 lines. service-02 has 3. */
function many(): RunbookService[] {
  return Array.from({ length: 50 }, (_, i) => {
    const name = `service-${String(i + 1).padStart(2, "0")}`;
    const n = name === "service-02" ? 3 : i % 4 === 0 ? 0 : 1 + ((i * 7) % 5);
    const runbook = Array.from({ length: n }, (_, k) => line(GENERIC[(i + k) % 5], WHO[(i + k) % 3], WHEN[(i + k) % 5]));
    return svc(name, runbook, [], n > 0 ? `Read by the investigator ${1 + (i % 6)} times this week` : null);
  });
}

type Start = { selected: string; text: string; error: string };
const NONE: Start = { selected: "", text: "", error: "" };

interface Sample { data: RunbooksData | "loading" | "error"; start: Start }
const of = (services: RunbookService[], start: Partial<Start> = {}): Sample =>
  ({ data: { services, empty_words: null }, start: { ...NONE, ...start } });

const SAMPLES: Record<string, Sample> = {
  normal: of(five("line")),
  typing: of(five("line"), { text: "Restart edge-proxy one pod at a time, never all at once." }),
  dupe: of(five("line"), { text: "Rolling back is safe: the last 5 deploys had no migrations.", error: SAME }),
  nolines: of(five("line"), { selected: "docs-site" }),
  memonly: of(five("memory"), { selected: "service-a" }),
  empty: { data: { services: [], empty_words: EMPTY_WORDS }, start: NONE },
  loading: { data: "loading", start: NONE },
  error: { data: "error", start: NONE },
  addfail: of(five("line"), { text: "Restart edge-proxy one pod at a time.", error: NOT_SAVED }),
  long: of(five("line"), { selected: "checkout" }),
  many: of(many(), { selected: "service-02" }),
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** The service a sample opens on, and what its add box holds and says. */
export const sampleStart = (name: string): Start => SAMPLES[name].start;

/** What a read of the page answers for a sample: the data, never an answer (`loading`), or an error (`error`). */
export function sampleLoad(name: string): Promise<RunbooksData> {
  const s = SAMPLES[name].data;
  if (s === "loading") return new Promise(() => {});
  if (s === "error") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(s);
}

const tidy = (s: string) => s.split(/\s+/).filter(Boolean).join(" ");
const change = (d: RunbooksData, service: string, f: (r: RunbookLine[]) => RunbookLine[]): RunbooksData => ({
  ...d,
  services: d.services.map((s) => {
    if (s.name !== service) return s;
    const runbook = f(s.runbook);
    return { ...s, runbook, lines_words: linesWords(runbook.length) };
  }),
});

/** A sample has no cell to write to: it answers as the cell does, on this copy of the data. In
 *  `addfail` every add fails the way a cell that did not save the line does. */
export function sampleAdd(name: string, d: RunbooksData, service: string, text: string): RunbooksData {
  if (name === "addfail") throw new ApiError(NOT_SAVED, 500);
  const one = tidy(text);
  if (!one) throw new ApiError("Write the line to add.", 400);
  if (one.length > MAX_LINE) throw new ApiError(`That line is ${one.length} characters. Keep it to ${MAX_LINE}.`, 400);
  const have = d.services.find((s) => s.name === service)?.runbook ?? [];
  if (have.some((l) => l.text.toLowerCase() === one.toLowerCase())) throw new ApiError(SAME, 409);
  return change(d, service, (r) => [...r, line(one, "you", "today")]);
}

export function sampleRemove(d: RunbooksData, service: string, index: number): RunbooksData {
  return change(d, service, (r) => r.filter((_, i) => i !== index));
}
