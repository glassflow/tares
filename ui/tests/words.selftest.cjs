/*
 * TR-726: self-test of the words matcher, the static scanner and the browser helpers (no browser:
 * the helpers run against a stand-in page). Exit 1 on any mismatch.
 * Run:  node tests/words.selftest.cjs     (from ui/)
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const ts = require("typescript");
const { EventEmitter } = require("events");
const { offendersIn, checkWords, expectOneCall, READ_TEXT } = require("./words.cjs");
const { scanSource, listFiles } = require("./words.check.cjs");

// words.ts is what the screens import, so the test loads that file itself (compiled in memory).
function loadTs(file) {
  const out = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  }).outputText;
  const m = new Module(file);
  m.filename = file;
  m.paths = Module._nodeModulePaths(path.dirname(file));
  m._compile(out, file);
  return m.exports;
}
const WORDS_TS = path.join(__dirname, "..", "src", "editions", "words.ts");
const WORDS_MD = path.join(__dirname, "..", "src", "editions", "WORDS.md");
const { forbiddenIn, durationWords } = loadTs(WORDS_TS);

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The matcher.
for (const [text, want] of [
  ["3 runs today", ["runs"]],
  ["Sources", ["sources"]],
  ["Run its 5 checks", ["run"]],
  ["a\u2014b", ["em dash"]],
  ["Trigger happy", ["trigger"]],
  ["View PR", []],
  ["open source", []],
  ["Run the tests", []],
  ["View the sources", ["sources"]],
  ["Preview the page", []],
  ["Stations and entities", ["stations", "entities"]],
  ["3 firings, 2 deliveries", ["firings", "deliveries"]],
  ["Try its checks", []],
  // Known limits (see WORDS.md): the allowed phrase "runs on" lets "3 runs on main" through, and
  // only whole words match, so inflected forms pass. Pinned so a change to either is a decision.
  ["3 runs on main", []],
  ["3 runs today on main", ["runs"]],
  ["running", []],
  ["triggered", []],
  ["Views and runs on main", ["views"]],
]) {
  const got = offendersIn(text);
  ck(`matcher: ${JSON.stringify(text)} -> ${JSON.stringify(want)}`, same(got, want), JSON.stringify(got));
  const viaTs = forbiddenIn(text);
  ck(`forbiddenIn (words.ts): ${JSON.stringify(text)} -> ${JSON.stringify(want)}`, same(viaTs, want), JSON.stringify(viaTs));
}
ck("forbiddenIn: names come in the order they appear, once each",
  same(forbiddenIn("Entities, then sources, then entities again"), ["entities", "sources"]));
ck("forbiddenIn: line breaks and runs of spaces do not hide an allowed phrase",
  same(forbiddenIn("open\n  source   and View\tPR"), []));
ck("forbiddenIn: an em dash comes first when it comes first", same(forbiddenIn("a\u2014b sources"), ["em dash", "sources"]));

// durationWords: one unit, floored; when the count is 1 the next unit follows if it is not zero.
for (const [secs, want] of [
  [0, "0 seconds"], [1, "1 second"], [17, "17 seconds"], [17.9, "17 seconds"], [59, "59 seconds"],
  [60, "1 minute"], [61, "1 minute, 1 second"], [125, "2 minutes"], [480, "8 minutes"], [3599, "59 minutes"],
  [3600, "1 hour"], [3660, "1 hour, 1 minute"], [7200, "2 hours"], [86400, "1 day"], [86430, "1 day"],
  [151200, "1 day, 18 hours"], [518400, "6 days"],
  [-5, "0 seconds"], [NaN, "0 seconds"], [Infinity, "0 seconds"],
]) {
  ck(`durationWords(${secs}) -> ${JSON.stringify(want)}`, durationWords(secs) === want, durationWords(secs));
}

// The static scanner, on fixture strings.
const scan = (src, name = "Fixture.tsx") => scanSource(name, src).map((h) => `${h.line} ${h.word}`);
for (const [label, src, want] of [
  ["JSX text", "export const X = () => <p>3 runs today</p>;", ["1 runs"]],
  ["a constant outside JSX", 'const LABEL = {a: "3 runs"};', ["1 runs"]],
  ["a template literal part", "const t = `${n} sources`;", ["1 sources"]],
  ["an attribute value", 'const X = () => <input placeholder="Add a source" />;', ["1 source"]],
  ["the second line", 'const a = 1;\nconst L = {a: "Sources"};', ["2 sources"]],
  ["an em dash in JSX text", "const X = () => <p>a &mdash; b</p>;", ["1 em dash"]],
  ["&mdash; in a JSX attribute", 'const X = () => <input placeholder="a &mdash; b" />;', ["1 em dash"]],
  ["&#8212; in a JSX attribute", 'const X = () => <input title="a &#8212; b" />;', ["1 em dash"]],
  ["&#x2014; in a JSX attribute", 'const X = () => <input aria-label="a &#x2014; b" />;', ["1 em dash"]],
  ["a literal em dash in a JSX attribute", 'const X = () => <input title="a \u2014 b" />;', ["1 em dash"]],
  ["&mdash; in a plain string stays text", 'const s = "a &mdash; b";', []],
  ["exempt tool output", "const X = () => <pre data-words-exempt>3 runs</pre>;", []],
  ["exempt, with children", "const X = () => <div data-words-exempt><b>3 runs</b></div>;", []],
  ["a name people chose", "const X = () => <span data-user-text>my-source</span>;", []],
  ["an import path", 'import x from "./sources";', []],
  ["an export path", 'export { y } from "./sources";', []],
  ["a route", 'const X = () => <Link to="/sources">Back</Link>;', []],
  ["a route string", 'const p = "/sources/new";', []],
  ["a className", 'const X = () => <p className="run source">Hi</p>;', []],
  ["an object key", "const o = {run: 1, sources: 2};", []],
  ["a quoted object key", 'const o = {"run": 1, "sources": 2};', []],
  ["a quoted key does not hide its value", 'const o = {"run": "3 runs"};', ["1 runs"]],
  ["a type literal", 'type K = "run" | "view";', []],
  ["the words-ok comment", 'const k = "run"; // words-ok', []],
  ["an allowed phrase", 'const k = "View PR";', []],
  ["clean text", "const X = () => <p>Your crew is idle.</p>;", []],
]) {
  const got = scan(src);
  ck(`scanner: ${label}`, same(got, want), JSON.stringify(got));
}

// The files the scanner reads: .ts and .tsx, nothing else (WORDS.md says the same).
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "words-"));
fs.mkdirSync(path.join(tmp, "sub"));
for (const f of ["a.ts", "b.tsx", "sub/c.ts", "d.json", "e.md", "f.cjs"]) fs.writeFileSync(path.join(tmp, f), "");
const listed = listFiles(tmp).map((f) => path.relative(tmp, f)).sort();
fs.rmSync(tmp, { recursive: true });
ck("scanner reads .ts and .tsx only", same(listed, ["a.ts", "b.tsx", path.join("sub", "c.ts")]), JSON.stringify(listed));
ck("WORDS.md names the limits of the check",
  /"3 runs on main" passes/.test(fs.readFileSync(WORDS_MD, "utf8").replace(/\s+/g, " "))
  && /"triggered" pass/.test(fs.readFileSync(WORDS_MD, "utf8").replace(/\s+/g, " ")));
ck("WORDS.md says the static check reads .ts and .tsx files only",
  /static check[^]*reads[^]*`\.ts` and `\.tsx` files only/.test(fs.readFileSync(WORDS_MD, "utf8").replace(/\s+/g, " ")));

// checkWords reads the page text; a stand-in page returns what a person would read.
async function viaCheckWords(text) {
  const results = [];
  await checkWords({ evaluate: async () => text }, (l, c, d) => results.push([l, c, d]), "state");
  return results;
}

// The exempt regions are hidden by a stylesheet while the text is read, and the stylesheet goes
// away afterwards, even when the read fails. Run READ_TEXT against a stand-in document.
function readWithFakeDocument(readFails) {
  const log = [];
  global.document = {
    head: { appendChild: (el) => log.push(`add ${el.textContent}`) },
    createElement: () => ({ textContent: "", remove() { log.push("remove"); } }),
    body: { get innerText() { log.push("read"); if (readFails) throw new Error("boom"); return "text"; } },
  };
  let threw = false;
  try { READ_TEXT(); } catch { threw = true; } finally { delete global.document; }
  return { log, threw };
}
{
  const { log, threw } = readWithFakeDocument(false);
  ck("checkWords: the stylesheet is added, then the text read, then the stylesheet removed",
    !threw && log.length === 3 && log[0].startsWith("add ") && log[1] === "read" && log[2] === "remove", JSON.stringify(log));
  ck("checkWords: the stylesheet hides data-words-exempt and data-user-text",
    /\[data-words-exempt\]/.test(log[0]) && /\[data-user-text\]/.test(log[0]) && /display:\s*none/.test(log[0]), log[0]);
  const failed = readWithFakeDocument(true);
  ck("checkWords: the stylesheet is removed when the read fails", failed.threw && failed.log[failed.log.length - 1] === "remove", JSON.stringify(failed.log));
}

// expectOneCall counts the GETs to /api/ until the page is idle.
function fakePage(requests) {
  const page = new EventEmitter();
  page.off = page.removeListener;
  page.waitForLoadState = async () => { for (const r of requests) page.emit("request", r); };
  return page;
}
const req = (method, url) => ({ method: () => method, url: () => `http://127.0.0.1:8800${url}` });
async function viaExpectOneCall(requests, route = "/api/home") {
  const page = fakePage(requests);
  const results = [];
  const done = expectOneCall(page, route);
  await done((l, c, d) => results.push(c), "state");
  return results;
}

(async () => {
  let r = await viaCheckWords("Your crew is idle.");
  ck("checkWords: clean page passes both checks", r.length === 2 && r.every((x) => x[1]));
  r = await viaCheckWords("3 runs today");
  ck("checkWords: an internal name fails the first check", r[0][1] === false && /runs/.test(r[0][2]));
  r = await viaCheckWords("a\u2014b");
  ck("checkWords: an em dash fails the second check", r[0][1] === true && r[1][1] === false);

  let c = await viaExpectOneCall([req("GET", "/api/whoami"), req("GET", "/api/home?x=1")]);
  ck("expectOneCall: one read, whoami not counted, query ignored", same(c, [true, true]), JSON.stringify(c));
  c = await viaExpectOneCall([req("GET", "/api/home"), req("GET", "/api/home")]);
  ck("expectOneCall: two reads of the route fail", same(c, [false, true]), JSON.stringify(c));
  c = await viaExpectOneCall([req("GET", "/api/home"), req("GET", "/api/projects")]);
  ck("expectOneCall: another read fails", same(c, [true, false]), JSON.stringify(c));
  c = await viaExpectOneCall([req("GET", "/api/home"), req("POST", "/api/home/actions/raise_budget")]);
  ck("expectOneCall: a write is not a read", same(c, [true, true]), JSON.stringify(c));
  c = await viaExpectOneCall([req("GET", "/assets/index.js")]);
  ck("expectOneCall: no call to the route fails", same(c, [false, true]), JSON.stringify(c));

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
