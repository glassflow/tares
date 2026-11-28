// The console is the factory only (TR-1817). This walks the imports from src/main.tsx (static,
// `import type`, `export ... from`, dynamic `import()` and Vite's `import.meta.glob`, which is how the
// ee screens load) and fails when the walk reaches a file of
// the platform or the AI SRE console: anything under src/editions/sre, src/pages (src/pages/dev is
// the component gallery and may stay) or ChooseEdition. For each one it prints the chain that leads
// there, so the edge to cut is plain. Run it with `npm run test:closure`.
const fs = require("fs");
const path = require("path");

const UI = path.resolve(__dirname, "..");
const SRC = path.join(UI, "src");
const ENTRY = path.join(SRC, "main.tsx");
const ALIASES = [
  ["@paper-kit/", path.join(UI, "paper-kit", "src") + "/"],
  ["@paper-kit", path.join(UI, "paper-kit", "src", "index.ts")],
  ["@/", SRC + "/"],
];
const EXTS = ["", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", "/index.tsx", "/index.ts", "/index.js"];

function offence(file) {
  const rel = path.relative(SRC, file).split(path.sep).join("/");
  if (rel.startsWith("..")) return null;
  if (rel.startsWith("editions/sre/")) return "the AI SRE console";
  if (rel.startsWith("pages/") && !rel.startsWith("pages/dev/")) return "a platform page";
  if (/(^|\/)ChooseEdition\.[jt]sx?$/.test(rel)) return "the edition chooser";
  return null;
}

function resolve(spec, from) {
  let base = null;
  if (spec.startsWith("./") || spec.startsWith("../")) base = path.resolve(path.dirname(from), spec);
  else {
    for (const [prefix, target] of ALIASES) {
      if (spec === prefix || spec.startsWith(prefix)) { base = target + spec.slice(prefix.length); break; }
    }
  }
  if (base === null) return null;   // a package: not ours to walk
  for (const ext of EXTS) {
    const f = base + ext;
    if (fs.existsSync(f) && fs.statSync(f).isFile()) return f;
  }
  return undefined;
}

// Comments out, so an import named in a comment is not walked.
function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\"'`])\/\/.*$/gm, "$1");
}

const IMPORT_RES = [
  /\bimport\s+(?:type\s+)?[^'"`;]*?\bfrom\s*["']([^"']+)["']/g,   // import x from, import type x from
  /\bimport\s*["']([^"']+)["']/g,                                  // import "./side-effect"
  /\bexport\s+(?:type\s+)?[^'"`;]*?\bfrom\s*["']([^"']+)["']/g,   // export ... from
  /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g,                        // import("./lazy")
];

// import.meta.glob("./ee/index.ts") or a pattern with * and **: every file it matches, as relative specs.
const GLOB_RE = /\bimport\.meta\.glob(?:<[^>]*>)?\s*\(\s*(\[[^\]]*\]|["'][^"']+["'])/g;
function globSpecs(arg, from) {
  const pats = [...arg.matchAll(/["']([^"']+)["']/g)].map((m) => m[1]).filter((p) => !p.startsWith("!"));
  const out = [];
  for (const pat of pats) {
    if (!pat.includes("*")) { out.push(pat); continue; }
    const fixed = pat.slice(0, pat.indexOf("*")).replace(/[^/]*$/, "");
    const root = path.resolve(path.dirname(from), fixed);
    const re = new RegExp("^" + pat.slice(fixed.length).replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*\*\//g, "\u0000").replace(/\*/g, "[^/]*").replace(/\u0000/g, "(?:.*/)?") + "$");
    const walk = (dir) => {
      if (!fs.existsSync(dir)) return;
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const f = path.join(dir, e.name);
        if (e.isDirectory()) walk(f);
        else if (re.test(path.relative(root, f).split(path.sep).join("/"))) out.push("./" + path.relative(path.dirname(from), f));
      }
    };
    walk(root);
  }
  return out;
}

function importsOf(file) {
  if (!/\.(tsx?|jsx?|mjs|cjs)$/.test(file)) return [];
  const text = stripComments(fs.readFileSync(file, "utf8"));
  const out = new Set();
  for (const re of IMPORT_RES) for (const m of text.matchAll(re)) out.add(m[1]);
  for (const m of text.matchAll(GLOB_RE)) for (const spec of globSpecs(m[1], file)) out.add(spec);
  return [...out];
}

const parent = new Map([[ENTRY, null]]);
const queue = [ENTRY];
const missing = [];
const offenders = [];
while (queue.length) {
  const file = queue.shift();
  const why = offence(file);
  if (why) { offenders.push([file, why]); continue; }   // the first chain is enough; do not walk past it
  for (const spec of importsOf(file)) {
    const to = resolve(spec, file);
    if (to === null) continue;
    if (to === undefined) { missing.push(`${path.relative(UI, file)} imports ${spec}, which is not a file`); continue; }
    if (parent.has(to)) continue;
    parent.set(to, file);
    queue.push(to);
  }
}

const chain = (file) => {
  const steps = [];
  for (let f = file; f; f = parent.get(f)) steps.unshift(path.relative(SRC, f));
  return steps.join("\n      -> ");
};

console.log(`factory closure: ${parent.size} files reached from src/main.tsx`);
for (const m of missing) console.log(`  unresolved: ${m}`);
if (offenders.length === 0 && missing.length === 0) {
  console.log("ok: no platform page, AI SRE screen or edition chooser is reachable");
  process.exit(0);
}
for (const [file, why] of offenders) {
  console.log(`\n  FAIL ${path.relative(SRC, file)} (${why}) is reachable:\n      ${chain(file)}`);
}
console.log(`\n${offenders.length} file(s) outside the factory are reachable` + (missing.length ? `, ${missing.length} import(s) unresolved` : ""));
process.exit(1);
