// TR-1539: vendor the shared paper files from glassflow/arche-design into the console.
//
//   npm run paper:sync -- [--tokens <paper-tokens tag>] [--paper-kit <paper-kit tag>]
//
// Either flag is optional, at least one is required; a part you leave out stays as it is.
//   --tokens     tokens.css -> ui/paper/, paper-theme.css and paper-motion.css -> ui/src/paper/
//   --paper-kit  packages/paper-kit/** -> ui/paper-kit/ (cleared first, so a deleted file goes away)
// Every CSS, TS, TSX and JS file gets a first-line comment naming the tag and the 40-char commit, and
// ui/paper/VENDORED.json records both and the sha256 of every vendored file (CRLF read as LF).
// `npm run test:paper` checks those hashes, so a vendored copy is never edited by hand.
//
// Only node: built-ins (it runs on Node 20 in CI). The network is used here and nowhere else.
// ARCHE_DESIGN_REPO overrides the repository to clone (a path or URL), for testing a branch locally.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UI = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO = "glassflow/arche-design";
const SOURCE = process.env.ARCHE_DESIGN_REPO || `https://github.com/${REPO}`;
const REFS = "skills/glassflow-paper-design/references";
const TOKEN_FILES = [
  { from: `${REFS}/tokens.css`, to: "paper/tokens.css" },
  { from: `${REFS}/paper-theme.css`, to: "src/paper/paper-theme.css" },
  { from: `${REFS}/paper-motion.css`, to: "src/paper/paper-motion.css" },
];
const KIT_FROM = "packages/paper-kit";
const KIT_TO = "paper-kit";
const MANIFEST = "paper/VENDORED.json";
const BLOCK = new Set([".css", ".ts", ".tsx", ".js"]);
const LINE = new Set([".mjs"]);

// fail() throws instead of exiting: process.exit skips `finally`, which would leave the temp clone behind.
// The one catch at the bottom prints the message and exits, after every finally has run.
class SyncFailure extends Error {}
const fail = (msg) => {
  throw new SyncFailure(msg);
};

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag !== "--tokens" && flag !== "--paper-kit") fail(`unknown argument ${flag}. Usage: npm run paper:sync -- [--tokens <tag>] [--paper-kit <tag>]`);
    const tag = argv[++i];
    if (!tag || !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(tag)) fail(`${flag} needs a tag name, got ${JSON.stringify(tag ?? "")}`);
    out[flag === "--tokens" ? "tokens" : "paperKit"] = tag;
  }
  if (!out.tokens && !out.paperKit) fail("give --tokens <tag>, --paper-kit <tag>, or both");
  return out;
}

const git = (args, opts = {}) => spawnSync("git", args, { encoding: "utf8", ...opts });
const sha256 = (buf) => createHash("sha256").update(buf.toString("latin1").replace(/\r\n/g, "\n"), "latin1").digest("hex");
const sortKeys = (v) =>
  Array.isArray(v) || v === null || typeof v !== "object"
    ? v
    : Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]));

function header(ext, tag, commit) {
  const text = `vendored from ${REPO} ${tag} ${commit}: do not edit, run npm run paper:sync`;
  if (BLOCK.has(ext)) return `/* ${text} */\n`;
  if (LINE.has(ext)) return `// ${text}\n`;
  return "";
}

function walk(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(p));
    else if (e.isFile()) out.push(p);
  }
  return out;
}

function clone(tag) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "paper-sync-"));
  const r = git(["clone", "--quiet", "--depth", "1", "--branch", tag, "--", SOURCE, dir]);
  if (r.status !== 0) {
    fs.rmSync(dir, { recursive: true, force: true });
    fail(`could not clone ${SOURCE} at tag ${tag}. Is the tag pushed?\n${(r.stderr || "").trim()}`);
  }
  const head = git(["-C", dir, "rev-parse", "HEAD"]);
  const commit = (head.stdout || "").trim();
  if (head.status !== 0 || !/^[0-9a-f]{40}$/.test(commit)) {
    fs.rmSync(dir, { recursive: true, force: true });
    fail(`could not read the commit of tag ${tag}`);
  }
  return { dir, commit };
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  const status = git(["-C", UI, "status", "--porcelain"]);
  if (status.status !== 0) fail("git status failed: is ui/ inside a git checkout?");
  if (status.stdout.trim()) fail(`the working tree is not clean, commit or stash first:\n${status.stdout.trimEnd()}`);

  const manifestPath = path.join(UI, MANIFEST);
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const before = new Map();
  for (const root of ["paper", "src/paper", "paper-kit"]) {
    for (const f of walk(path.join(UI, root))) {
      const rel = path.relative(UI, f).split(path.sep).join("/");
      if (rel !== MANIFEST) before.set(rel, sha256(fs.readFileSync(f)));
    }
  }
  const files = { ...(manifest.files || {}) };
  const touched = new Set();
  // Nothing is written to ui/ until every source was found: vendor() only stages the bytes here.
  const staged = new Map();
  let clearKit = false;

  function vendor(srcDir, from, to, tag, commit) {
    const src = path.join(srcDir, from);
    if (!fs.existsSync(src)) fail(`${from} is not in ${REPO} at ${tag}`);
    const bytes = Buffer.concat([Buffer.from(header(path.extname(src), tag, commit)), fs.readFileSync(src)]);
    staged.set(to, bytes);
    files[to] = sha256(bytes);
    touched.add(to);
  }

  if (args.tokens) {
    const { dir, commit } = clone(args.tokens);
    try {
      for (const f of TOKEN_FILES) vendor(dir, f.from, f.to, args.tokens, commit);
      manifest.tokens = { tag: args.tokens, commit };
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  if (args.paperKit) {
    const { dir, commit } = clone(args.paperKit);
    try {
      const root = path.join(dir, KIT_FROM);
      if (!fs.existsSync(root)) fail(`${KIT_FROM} is not in ${REPO} at ${args.paperKit}`);
      clearKit = true;
      for (const k of Object.keys(files)) if (k.startsWith(`${KIT_TO}/`)) delete files[k];
      for (const f of walk(root)) {
        const rel = path.relative(root, f).split(path.sep).join("/");
        vendor(root, rel, `${KIT_TO}/${rel}`, args.paperKit, commit);
      }
      manifest.paperKit = { tag: args.paperKit, commit };
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  // Every source is staged: now write. A kit sync clears ui/paper-kit/ first so a deleted file goes away.
  if (clearKit) fs.rmSync(path.join(UI, KIT_TO), { recursive: true, force: true });
  for (const [to, bytes] of staged) {
    const dest = path.join(UI, to);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, bytes);
  }

  const added = [], changed = [], removed = [];
  for (const rel of touched) {
    if (!before.has(rel)) added.push(rel);
    else if (before.get(rel) !== files[rel]) changed.push(rel);
  }
  for (const rel of before.keys()) if (!fs.existsSync(path.join(UI, rel))) removed.push(rel);

  manifest.repo = REPO;
  manifest.files = files;
  fs.mkdirSync(path.dirname(manifestPath), { recursive: true });
  fs.writeFileSync(manifestPath, JSON.stringify(sortKeys(manifest), null, 2) + "\n");

  const show = (label, list) => list.sort().forEach((f) => console.log(`${label} ${f}`));
  show("added  ", added);
  show("changed", changed);
  show("removed", removed);
  console.log(`${added.length} added, ${changed.length} changed, ${removed.length} removed`);
}

try {
  main();
} catch (e) {
  if (!(e instanceof SyncFailure)) throw e;
  console.error(`paper:sync: ${e.message}`);
  process.exit(1);
}
