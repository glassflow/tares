/*
 * TR-1261: unit tests of src/lib/time.ts and src/lib/number.ts, the console's one home for time.
 * The modules are compiled in memory from the files the screens import. Exit 1 on any mismatch.
 * Run:  npm run test:time     (from ui/)
 */
const fs = require("fs");
const path = require("path");
const Module = require("module");
const ts = require("typescript");

function loadTs(file) {
  const out = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const m = new Module(file);
  m.filename = file;
  m._compile(out, file);
  return m.exports;
}
const LIB = path.join(__dirname, "..", "src", "lib");
const t = loadTs(path.join(LIB, "time.ts"));
const n = loadTs(path.join(LIB, "number.ts"));

let pass = 0, fail = 0;
const eq = (label, got, want) => {
  if (got === want) pass++;
  else { fail++; console.log(`FAIL ${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
};
const { MIN, HOUR, DAY } = t;
const S = 1000;
const at = (iso) => Date.parse(iso);
const NOW = at("2026-11-12T14:05:00Z");
const BAD = [NaN, Date.parse(""), Date.parse("not a date"), Infinity];

// --- formatStampUTC: the instant as a stamp, in UTC whatever the machine's zone ---
eq("stamp, minute", t.formatStampUTC("2026-11-12T14:05:37.250Z"), "2026-11-12 14:05Z");
eq("stamp, second", t.formatStampUTC("2026-11-12T14:05:37.250Z", "second"), "2026-11-12 14:05:37Z");
eq("stamp, ms", t.formatStampUTC("2026-11-12T14:05:37.250Z", "ms"), "2026-11-12 14:05:37.250Z");
eq("stamp from a number", t.formatStampUTC(NOW), "2026-11-12 14:05Z");
eq("stamp of an offset time reads UTC", t.formatStampUTC("2026-11-12T23:30:00-02:00"), "2026-11-13 01:30Z");
eq("stamp pads month, day and hour", t.formatStampUTC("2026-01-02T03:04:00Z"), "2026-01-02 03:04Z");
eq("stamp of a bad string passes it through", t.formatStampUTC("soon"), "soon");
eq("stamp of NaN passes it through", t.formatStampUTC(NaN), "NaN");

// --- formatDayUTC: the calendar day ---
eq("day", t.formatDayUTC("2026-11-12T14:05:00Z"), "Nov 12, 2026");
eq("day reads UTC at the edge", t.formatDayUTC("2026-11-12T23:30:00-02:00"), "Nov 13, 2026");
eq("day of a bad string passes it through", t.formatDayUTC("soon"), "soon");

// --- formatRelative: the tooltip's backward ladder, floored ---
for (const [ago, want] of [
  [0, "just now"], [4 * S, "just now"], [5 * S, "5s ago"], [59 * S, "59s ago"], [60 * S, "1m ago"],
  [90 * MIN, "1h ago"], [119 * MIN, "1h ago"], [2 * HOUR, "2h ago"], [23 * HOUR, "23h ago"], [DAY, "1d ago"],
  [29 * DAY, "29d ago"], [30 * DAY, "1mo ago"], [359 * DAY, "11mo ago"], [360 * DAY, "1y ago"],
  [-30 * S, "just now"], [-61 * S, "in the future"],
]) eq(`relative ${ago}ms`, t.formatRelative(NOW - ago, NOW), want);
for (const b of BAD) eq(`relative of ${b} is empty`, t.formatRelative(b, NOW), "");

// --- formatUntil: the forward twin, rounded up ---
for (const [ahead, want] of [
  [0, "now"], [-5 * S, "now"], [1 * S, "in 1m"], [59 * MIN, "in 59m"], [60 * MIN, "in 1h"], [61 * MIN, "in 2h"],
  [24 * HOUR, "in 1d"], [30 * DAY, "in 30d"], [59 * DAY, "in 59d"], [60 * DAY, "in 2mo"], [365 * DAY, "in 1y"],
]) eq(`until ${ahead}ms`, t.formatUntil(NOW + ahead, NOW), want);
for (const b of BAD) eq(`until ${b} is empty`, t.formatUntil(b, NOW), "");

// --- formatDistance: past backward, more than a minute ahead forward ---
eq("distance past", t.formatDistance(NOW - 3 * HOUR, NOW), "3h ago");
eq("distance future", t.formatDistance(NOW + 27 * DAY, NOW), "in 27d");
eq("distance skew under a minute is past", t.formatDistance(NOW + 30 * S, NOW), "just now");

// --- the instant in a sentence ---
eq("clock", t.clock(at("2026-11-12T09:07:59Z")), "09:07");
eq("dateWords", t.dateWords(at("2026-10-27T14:05:00Z")), "27 Oct, 14:05");
eq("sinceWords today", t.sinceWords(at("2026-11-12T00:01:00Z"), NOW), "00:01");
eq("sinceWords yesterday", t.sinceWords(at("2026-11-11T23:59:00Z"), NOW), "yesterday, 23:59");
eq("sinceWords further back", t.sinceWords(at("2026-10-27T14:05:00Z"), NOW), "27 Oct, 14:05");
eq("sinceWords of a future time says its clock", t.sinceWords(NOW + HOUR, NOW), "15:05");
eq("stamp today", t.stamp(at("2026-11-12T08:00:00Z"), NOW, "far"), "today 08:00");
eq("stamp yesterday", t.stamp(at("2026-11-11T08:00:00Z"), NOW, "far"), "yesterday 08:00");
eq("stamp this week names the day", t.stamp(at("2026-11-06T12:40:00Z"), NOW, "far"), "Fri 12:40");
eq("stamp a week back is far", t.stamp(at("2026-11-05T12:40:00Z"), NOW, "far"), "far");
for (const b of BAD) {
  eq(`clock of ${b}`, t.clock(b), "");
  eq(`dateWords of ${b}`, t.dateWords(b), "");
  eq(`sinceWords of ${b}`, t.sinceWords(b, NOW), "");
  eq(`stamp of ${b}`, t.stamp(b, NOW, "far"), "far");
}

// --- a length in words: the server's ladder (tares/goal.py span_words, age_words) ---
for (const [ms, want] of [
  [0, "less than a minute"], [59 * S, "less than a minute"], [60 * S, "1 minute"], [119 * S, "1 minute"],
  [45 * MIN, "45 minutes"], [HOUR - 1, "59 minutes"], [HOUR, "1 hour"], [2 * HOUR + 59 * MIN, "2 hours"],
  [DAY - 1, "23 hours"], [DAY, "1 day"], [3 * DAY + 23 * HOUR, "3 days"], [-5 * MIN, "less than a minute"],
]) eq(`spanWords ${ms}`, t.spanWords(ms), want);
for (const [ms, want] of [
  [0, "just now"], [59 * S, "just now"], [-HOUR, "just now"], [60 * S, "1 minute ago"], [8 * MIN, "8 minutes ago"],
  [HOUR, "1 hour ago"], [6 * DAY, "6 days ago"],
]) eq(`ageWords ${ms}`, t.ageWords(ms), want);
for (const [ms, want] of [
  [30 * S, "less than a minute"], [12 * MIN, "12 minutes"], [HOUR, "1 hour"], [64 * MIN, "1 hour 4 minutes"],
  [2 * HOUR, "2 hours"], [DAY, "1 day"], [DAY + 18 * HOUR, "1 day 18 hours"], [2 * DAY + 3 * HOUR + 59 * MIN, "2 days 3 hours"],
]) eq(`durationWords ${ms}`, t.durationWords(ms), want);
for (const b of [NaN, Infinity, -Infinity]) {
  eq(`spanWords of ${b}`, t.spanWords(b), "a while");
  eq(`durationWords of ${b}`, t.durationWords(b), "a while");
}
eq("ageWords of NaN says no NaN", /NaN/.test(t.ageWords(NaN)), false);

// --- the viewer's zone: a name, never empty ---
eq("viewerZone is a zone name", typeof t.viewerZone() === "string" && t.viewerZone().length > 0, true);

// --- numbers ---
eq("formatCount groups", n.formatCount(1100), "1,100");
eq("formatCount leaves small numbers", n.formatCount(7), "7");
eq("formatCount groups millions", n.formatCount(12408000), "12,408,000");
eq("formatDecimal keeps the digits asked for", n.formatDecimal(0.0025, 4), "0.0025");
eq("formatDecimal trims to the digits asked for", n.formatDecimal(1250.56, 1), "1,250.6");
eq("formatDecimal with no digits", n.formatDecimal(2.5, 0), "3");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
