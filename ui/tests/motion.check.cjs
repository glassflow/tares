// The motion contract's CSS and test side (TR-1431); the TSX side is the motion selector of
// eslint-design-contract.config.mjs. It fails when:
//   1. a transition or animation in src/**/*.css names a raw time or curve instead of a token
//      (the tokens themselves, in the `@theme static` block that defines --dur-*, are the one place);
//   2. src/lib/motion.ts disagrees with the tokens it mirrors;
//   3. an e2e suite that drives Chromium loads playwright itself instead of tests/browser.cjs, and so
//      runs with motion on.
// Run: npm run motion-contract. Prints one FAIL line per finding and exits 1 on any.
const fs = require("fs");
const path = require("path");

const UI = path.resolve(__dirname, "..");
const fails = [];
const fail = (where, what) => fails.push(`FAIL ${where}: ${what}`);

function cssFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? cssFiles(path.join(dir, e.name)) : e.name.endsWith(".css") ? [path.join(dir, e.name)] : []);
}

const lineOf = (text, i) => text.slice(0, i).split("\n").length;
// Blank out comments, keeping offsets, so a line number still points at the source.
const uncomment = (t) => t.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));

// 1. raw times in CSS
// 0s and 0ms are no choice of time (a delay dropped), so they pass.
const RAW = /(?<![\w.-])(?!0+m?s(?![\w-]))\d*\.?\d+m?s(?![\w-])|cubic-bezier\(|steps\(/;
const DECL = /(?<![\w-])(transition|animation)(-duration|-delay|-timing-function)?\s*:\s*([^;}]*)/g;
let tokens = null;
for (const file of cssFiles(path.join(UI, "src"))) {
  const rel = path.relative(UI, file);
  const text = uncomment(fs.readFileSync(file, "utf8"));
  // the token block is the one place a raw time belongs
  const block = /@theme static\s*\{[^}]*--dur-1:[^}]*\}/.exec(text);
  if (block) tokens = block[0];
  const clean = block ? text.slice(0, block.index) + block[0].replace(/[^\n]/g, " ") + text.slice(block.index + block[0].length) : text;
  for (const m of clean.matchAll(DECL)) {
    if (RAW.test(m[3])) fail(`${rel}:${lineOf(clean, m.index)}`, `${m[0].trim()} names a raw time or curve; use var(--dur-N) and var(--ease-*)`);
  }
}

// 2. src/lib/motion.ts mirrors the tokens
if (!tokens) fail("src/styles.css", "no `@theme static` block defining --dur-1");
else {
  const css = Object.fromEntries([...tokens.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  const ts = fs.readFileSync(path.join(UI, "src/lib/motion.ts"), "utf8");
  const dur = /DUR = \{([^}]*)\}/.exec(ts);
  const pairs = dur ? [...dur[1].matchAll(/(\d+):\s*(\d+)/g)].map((m) => [m[1], Number(m[2])]) : [];
  if (!pairs.length) fail("src/lib/motion.ts", "no DUR table");
  for (const [n, ms] of pairs) {
    const want = css[`dur-${n}`];
    if (want !== `${ms}ms`) fail("src/lib/motion.ts", `DUR[${n}] is ${ms}ms, styles.css --dur-${n} is ${want ?? "missing"}`);
  }
  for (const n of Object.keys(css).filter((k) => /^dur-\d$/.test(k))) {
    if (!pairs.some(([k]) => `dur-${k}` === n)) fail("src/lib/motion.ts", `DUR has no entry for --${n}`);
  }
  const ease = /EASE_OUT = "([^"]+)"/.exec(ts)?.[1];
  if (ease !== css["ease-out"]) fail("src/lib/motion.ts", `EASE_OUT is ${ease}, styles.css --ease-out is ${css["ease-out"]}`);
}

// 3. every browser suite runs quiet
for (const f of fs.readdirSync(path.join(UI, "tests")).filter((f) => f.endsWith(".e2e.cjs"))) {
  const s = fs.readFileSync(path.join(UI, "tests", f), "utf8");
  if (/node_modules["'],\s*["']playwright["']\s*\)|require\(["']playwright["']\)/.test(s)) fail(`tests/${f}`, "loads playwright itself; take chromium from ./browser.cjs");
  else if (/\bchromium\b/.test(s) && !s.includes('require("./browser.cjs")')) fail(`tests/${f}`, "drives Chromium without ./browser.cjs");
}

for (const l of fails) console.log(l);
console.log(fails.length ? `motion contract: ${fails.length} finding(s)` : "motion contract: clean");
process.exit(fails.length ? 1 : 0);
