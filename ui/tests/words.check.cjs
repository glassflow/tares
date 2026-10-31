/*
 * TR-726: the static words check. No browser, no daemon. Reads every string in the edition screens
 * (src/editions, src/components/edition, src/pages/dev; .ts and .tsx) with the TypeScript compiler:
 * JSX text, string literals and template literal parts, constants outside JSX included. Fails on a
 * forbidden word (src/editions/words.json) or an em dash. The platform console (src/pages) is not read.
 *
 * Skipped: import and export paths, require("..."), the values of className, data-testid, to, href and
 * key, object keys, type-level literals, strings that start with "/" (route paths), any JSX element
 * with data-words-exempt or data-user-text, and a line ending in the comment // words-ok.
 * Run:  node tests/words.check.cjs     (from ui/)
 */
const fs = require("fs");
const path = require("path");
const ts = require("typescript");
const { offendersIn } = require("./words.cjs");

const UI_DIR = path.resolve(__dirname, "..");
const ROOTS = ["src/editions", "src/components/edition", "src/pages/dev"];
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

function main() {
  let problems = 0, files = 0;
  for (const root of ROOTS) {
    for (const file of listFiles(path.join(UI_DIR, root))) {
      files++;
      const rel = path.relative(UI_DIR, file);
      for (const hit of scanSource(file, fs.readFileSync(file, "utf8"))) {
        console.log(`${rel}:${hit.line} ${hit.word}`);
        problems++;
      }
    }
  }
  console.log(problems ? `${problems} problems in ${files} files` : `0 problems (${files} files read)`);
  process.exit(problems ? 1 : 0);
}

if (require.main === module) main();
module.exports = { scanSource, listFiles };
