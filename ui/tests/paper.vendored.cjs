/*
 * TR-1539: the shared paper files are vendored from glassflow/arche-design by `npm run paper:sync`
 * and never edited here. paper/VENDORED.json lists every vendored file with the sha256 of its bytes
 * (CRLF read as LF).
 *
 * Fails, naming each file, on: a listed file that is missing; a listed file whose hash differs (a
 * hand edit); a file under paper/, src/paper/ or paper-kit/ that VENDORED.json does not list.
 *
 * Run:  npm run test:paper     (from ui/)
 * To change a vendored file: change it in arche-design, tag it, then `npm run paper:sync`.
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const UI = path.join(__dirname, "..");
const MANIFEST = "paper/VENDORED.json";
const ROOTS = ["paper", "src/paper", "paper-kit"];

const sha256 = (buf) => crypto.createHash("sha256").update(buf.toString("latin1").replace(/\r\n/g, "\n"), "latin1").digest("hex");
const walk = (dir) =>
  !fs.existsSync(dir)
    ? []
    : fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
        const p = path.join(dir, e.name);
        return e.isDirectory() ? walk(p) : e.isFile() ? [p] : [];
      });

const problems = [];
let ok = 0;
let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(path.join(UI, MANIFEST), "utf8"));
} catch (e) {
  console.log(`FAIL ${MANIFEST} cannot be read: ${e.message}`);
  process.exit(1);
}
const listed = manifest.files || {};
const names = Object.keys(listed);
if (names.length === 0) problems.push(`${MANIFEST} lists no files`);

for (const rel of names.sort()) {
  const file = path.join(UI, rel);
  if (!fs.existsSync(file)) problems.push(`${rel} is listed in ${MANIFEST} but missing`);
  else if (sha256(fs.readFileSync(file)) !== listed[rel]) problems.push(`${rel} differs from its vendored hash: do not edit it, change arche-design and run npm run paper:sync`);
  else ok++;
}
for (const root of ROOTS) {
  for (const f of walk(path.join(UI, root))) {
    const rel = path.relative(UI, f).split(path.sep).join("/");
    if (rel !== MANIFEST && !(rel in listed)) problems.push(`${rel} is not listed in ${MANIFEST}: vendored folders hold only synced files`);
  }
}

for (const p of problems) console.log(`FAIL ${p}`);
console.log(`\n${ok} vendored files match, ${problems.length} problems`);
process.exit(problems.length ? 1 : 0);
