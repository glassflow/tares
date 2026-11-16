/*
 * TR-726: the static words check. No browser, no daemon. Reads every string in the console screens
 * (.ts and .tsx) with the TypeScript compiler: JSX text, string literals and template literal parts,
 * constants outside JSX included, and fails on a forbidden word (src/editions/words.json) or an em dash.
 *
 * Two scopes (TR-1259):
 *   edition  src/editions, src/components/edition, src/pages/dev, src/ee. Every hit fails.
 *   legacy   the rest of src/pages and src/components (the platform pages and the shared components
 *            outside the kit in src/components/ui). A file may carry at most its count in
 *            words-contract.baseline.json; over that, every hit in it is printed and the check fails.
 *            Under it the check passes and says so; `--write` (npm run words-baseline) writes the lower
 *            counts back. The baseline only goes down: --write refuses while anything fails. A raise is a
 *            hand edit to the baseline, reviewed with its reason in the PR.
 *
 * Skipped: import and export paths, require("..."), the values of className, data-testid, to, href and
 * key, object keys, type-level literals, strings that start with "/" (route paths), any JSX element
 * with data-words-exempt or data-user-text, and a line ending in the comment // words-ok.
 * Run:  node tests/words.check.cjs [--write]     (from ui/)
 */
const fs = require("fs");
const path = require("path");
const ts = require("typescript");
const { offendersIn, WORDS } = require("./words.cjs");

const UI_DIR = path.resolve(__dirname, "..");
const ROOTS = ["src/editions", "src/components/edition", "src/pages/dev", "src/ee"];
const LEGACY_ROOTS = ["src/pages", "src/components"];
const KIT = "src/components/ui";
const BASELINE = path.join(UI_DIR, "words-contract.baseline.json");
const SKIP_ATTRS = new Set(["className", "data-testid", "to", "href", "key"]);
const EXEMPT_ATTRS = new Set(["data-words-exempt", "data-user-text"]);
const OK_COMMENT = /\/\/\s*words-ok\s*$/;
// JSX text and JSX attribute strings decode HTML entities, so &mdash; renders as an em dash there.
const decodeDash = (s) => s.replace(/&(?:mdash|#8212|#x2014);/gi, "\u2014");

function hasExemptAttr(node) {
  const opening = ts.isJsxElement(node) ? node.openingElement : node;
  return opening.attributes.properties.some((p) => ts.isJsxAttribute(p) && EXEMPT_ATTRS.has(p.name.getText()));
}

// [{line, word}] for one file's source text.
function scanSource(fileName, text) {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.ES2020, true, kind);
  const lines = text.split("\n");
  const out = [];

  const report = (node, value, start) => {
    const words = offendersIn(value);
    if (!words.length) return;
    const at = sf.getLineAndCharacterOfPosition(start ?? node.getStart(sf)).line;
    if (OK_COMMENT.test((lines[at] || "").trimEnd())) return;
    for (const w of words) out.push({ line: at + 1, word: w });
  };

  const isKeyOrPath = (n) => {
    const p = n.parent;
    if (!p) return false;
    if ((ts.isImportDeclaration(p) || ts.isExportDeclaration(p)) && p.moduleSpecifier === n) return true;
    if (ts.isExternalModuleReference(p) || ts.isLiteralTypeNode(p) || ts.isImportTypeNode(p)) return true;
    if (ts.isCallExpression(p) && p.arguments[0] === n && (p.expression.kind === ts.SyntaxKind.ImportKeyword
        || (ts.isIdentifier(p.expression) && p.expression.text === "require"))) return true;
    if ((ts.isPropertyAssignment(p) || ts.isPropertySignature(p) || ts.isPropertyDeclaration(p) || ts.isMethodDeclaration(p)
        || ts.isEnumMember(p)) && p.name === n) return true;
    if (ts.isElementAccessExpression(p) && p.argumentExpression === n) return true;
    return false;
  };

  const visit = (node) => {
    if ((ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) && hasExemptAttr(node)) return;
    if (ts.isJsxAttribute(node) && SKIP_ATTRS.has(node.name.getText())) return;
    if (ts.isJsxText(node)) {
      const raw = node.getText(sf);
      report(node, decodeDash(raw), node.getStart(sf) + (raw.length - raw.trimStart().length));
    } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
               || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      if (!isKeyOrPath(node) && !(node.text || "").startsWith("/")) {
        report(node, ts.isJsxAttribute(node.parent) ? decodeDash(node.text) : node.text);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

function listFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) return listFiles(p);
    return /\.tsx?$/.test(e.name) ? [p] : [];
  });
}

// What to write instead, from words.json: "runs: factory says session, sre says check ...".
const SAY = new Map();
for (const e of WORDS.forbidden) {
  const by = Object.entries(e).filter(([k]) => !["word", "plural", "allow"].includes(k));
  const one = by.every(([, v]) => v === by[0][1]);
  const hint = one ? `say ${by[0][1]}` : by.map(([ed, v]) => `${ed} says ${v}`).join(", ");
  SAY.set(e.word, hint);
  SAY.set(e.plural, hint);
}
SAY.set("em dash", "use a comma, a colon or a new sentence");
const describe = (word) => `${word} (${SAY.get(word) || "see src/editions/WORDS.md"})`;

const under = (rel, root) => rel === root || rel.startsWith(root + "/");
const isEdition = (rel) => ROOTS.some((r) => under(rel, r));

// Every file the check reads, relative to uiDir, with its scope.
function filesToRead(uiDir = UI_DIR) {
  const rel = (f) => path.relative(uiDir, f).split(path.sep).join("/");
  const edition = ROOTS.flatMap((r) => listFiles(path.join(uiDir, r)).map(rel));
  const legacy = LEGACY_ROOTS.flatMap((r) => listFiles(path.join(uiDir, r)).map(rel))
    .filter((f) => !isEdition(f) && !under(f, KIT));
  return [...edition.map((f) => ({ rel: f, legacy: false })), ...legacy.map((f) => ({ rel: f, legacy: true }))];
}

// Judge scanned files against the baseline. files: [{rel, legacy, hits: [{line, word}]}].
// Returns the lines to print, whether the check failed, and the counts --write would keep.
function judge(files, baseline) {
  const lines = [];
  const counts = {};
  let failed = false, problems = 0;
  const seen = new Set();
  for (const { rel, legacy, hits } of files) {
    seen.add(rel);
    const allowed = legacy ? (baseline[rel] ?? 0) : 0;
    const n = hits.length;
    if (legacy && n) counts[rel] = n;
    if (n > allowed) {
      failed = true;
      problems += n;
      const why = legacy ? `   (${n} in this file, the baseline allows ${allowed})` : "";
      for (const h of hits) lines.push(`${rel}:${h.line} ${describe(h.word)}${why}`);
    } else if (n < allowed) {
      lines.push(`${rel}: ${n} left, the baseline allows ${allowed}. Lower it: npm run words-baseline`);
    }
  }
  for (const rel of Object.keys(baseline)) {
    if (!seen.has(rel)) lines.push(`${rel}: in the baseline but not read. Drop it: npm run words-baseline`);
  }
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  lines.push(failed
    ? `${problems} problems in ${files.length} files`
    : `0 problems (${files.length} files read; ${total} legacy hits in ${Object.keys(counts).length} files, held by the baseline)`);
  return { lines, failed, counts };
}

function readBaseline(file = BASELINE) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
}

function writeBaseline(counts, file = BASELINE) {
  const sorted = Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
  fs.writeFileSync(file, JSON.stringify(sorted, null, 2) + "\n");
}

function main() {
  const baseline = readBaseline();
  const files = filesToRead().map((f) => ({
    ...f,
    hits: scanSource(f.rel, fs.readFileSync(path.join(UI_DIR, f.rel), "utf8")),
  }));
  const { lines, failed, counts } = judge(files, baseline);
  for (const l of lines) console.log(l);
  if (process.argv.includes("--write")) {
    if (failed) {
      console.error("Not written: a count rose or an edition file has a hit. Fix the words first.");
    } else {
      writeBaseline(counts);
      console.log(`words-contract.baseline.json written: ${Object.values(counts).reduce((a, b) => a + b, 0)} hits in ${Object.keys(counts).length} files`);
    }
  }
  process.exit(failed ? 1 : 0);
}

if (require.main === module) main();
module.exports = { scanSource, listFiles, filesToRead, judge, readBaseline, writeBaseline, describe };
