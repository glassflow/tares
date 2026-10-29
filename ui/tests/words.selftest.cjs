/*
 * TR-726: self-test of the words matcher, the static scanner and the browser helpers (no browser:
 * the helpers run against a stand-in page). Exit 1 on any mismatch.
 * Run:  node tests/words.selftest.cjs     (from ui/)
 */
const { EventEmitter } = require("events");
const { offendersIn, checkWords, expectOneCall } = require("./words.cjs");
const { scanSource } = require("./words.check.cjs");

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
]) {
  const got = offendersIn(text);
  ck(`matcher: ${JSON.stringify(text)} -> ${JSON.stringify(want)}`, same(got, want), JSON.stringify(got));
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
  ["exempt tool output", "const X = () => <pre data-words-exempt>3 runs</pre>;", []],
  ["exempt, with children", "const X = () => <div data-words-exempt><b>3 runs</b></div>;", []],
  ["a name people chose", "const X = () => <span data-user-text>my-source</span>;", []],
  ["an import path", 'import x from "./sources";', []],
  ["an export path", 'export { y } from "./sources";', []],
  ["a route", 'const X = () => <Link to="/sources">Back</Link>;', []],
  ["a route string", 'const p = "/sources/new";', []],
  ["a className", 'const X = () => <p className="run source">Hi</p>;', []],
  ["an object key", "const o = {run: 1, sources: 2};", []],
  ["a type literal", 'type K = "run" | "view";', []],
  ["the words-ok comment", 'const k = "run"; // words-ok', []],
  ["an allowed phrase", 'const k = "View PR";', []],
  ["clean text", "const X = () => <p>Your crew is idle.</p>;", []],
]) {
  const got = scan(src);
  ck(`scanner: ${label}`, same(got, want), JSON.stringify(got));
}

// checkWords reads the page text; a stand-in page returns what a person would read.
async function viaCheckWords(text) {
  const results = [];
  await checkWords({ evaluate: async () => text }, (l, c, d) => results.push([l, c, d]), "state");
  return results;
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
