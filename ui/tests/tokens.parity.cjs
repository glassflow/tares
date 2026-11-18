/*
 * TR-1268: Tares and Rius stay one family. Every `--kit-*` value in src/styles.css must equal the
 * shared paper token it copies (`--agency-*` in tests/paper-tokens.css, a vendored copy of the
 * glassflow-paper-design skill's references/tokens.css; its header names the source commit), unless
 * tests/tokens.parity.allow.json lists it with the reason Tares differs on purpose.
 *
 * Fails on: a --kit-* value that differs from its shared token; a --kit-* name with no shared token
 * and no allowlist entry; an allowlist entry that no longer differs or no longer exists (so the list
 * only shrinks). The shared tokens are HSL triplets rounded to whole numbers and Tares writes hex,
 * so a value matches when every RGB channel is within 3 of the triplet's.
 *
 * Run:  npm run test:tokens     (from ui/)
 * To change a shared value: change it in argus-ui, then the skill, then re-vendor
 * tests/paper-tokens.css in a PR. To differ on purpose: add an allowlist entry with its reason.
 */
const fs = require("fs");
const path = require("path");

const UI = path.join(__dirname, "..");
const STYLES = process.env.TOKENS_STYLES || path.join(UI, "src", "styles.css");
const SHARED = path.join(__dirname, "paper-tokens.css");
const ALLOW = path.join(__dirname, "tokens.parity.allow.json");
const TOLERANCE = 3;

// Tares names a few tokens shorter than the kit does; everything else is kit-X = agency-X.
const RENAMED = { "kit-muted": "agency-muted-foreground", "kit-dark-muted": "agency-dark-muted-foreground" };

const strip = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");
const decls = (css, prefix) => {
  const out = new Map();
  for (const m of strip(css).matchAll(new RegExp(`--(${prefix}-[a-z0-9-]+)\\s*:\\s*([^;]+);`, "g"))) {
    if (out.has(m[1]) && out.get(m[1]) !== m[2].trim()) throw new Error(`--${m[1]} is declared twice with different values`);
    out.set(m[1], m[2].trim());
  }
  return out;
};

function hex(v) {
  const m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(v);
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return { rgb: [n >> 16, (n >> 8) & 255, n & 255], alpha: m[2] ? parseInt(m[2], 16) / 255 : 1 };
}
function hsl(v) {
  const m = /^(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%$/.exec(v);
  if (!m) return null;
  const h = Number(m[1]) / 360, s = Number(m[2]) / 100, l = Number(m[3]) / 100;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const ch = (t) => {
    t = (t + 1) % 1;
    const c = t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
    return Math.round(c * 255);
  };
  return { rgb: [ch(h + 1 / 3), ch(h), ch(h - 1 / 3)], alpha: 1 };
}
const toHex = (rgb) => "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");

const kit = decls(fs.readFileSync(STYLES, "utf8"), "kit");
const shared = decls(fs.readFileSync(SHARED, "utf8"), "agency");
const allow = JSON.parse(fs.readFileSync(ALLOW, "utf8"));
const problems = [];
const used = new Set();
let matched = 0, onPurpose = 0;

if (kit.size === 0) problems.push(`no --kit-* token found in ${path.relative(UI, STYLES)}`);
for (const [name, value] of kit) {
  const entry = allow[`--${name}`];
  const twin = RENAMED[name] ?? name.replace(/^kit-/, "agency-");
  const mine = hex(value);
  if (!mine) { problems.push(`--${name}: "${value}" is not a #rrggbb or #rrggbbaa value`); continue; }
  let differs;
  if (!shared.has(twin)) differs = `has no shared token (--${twin})`;
  else {
    const theirs = hsl(shared.get(twin));
    if (!theirs) { problems.push(`--${twin}: "${shared.get(twin)}" is not an HSL triplet`); continue; }
    const off = Math.max(...mine.rgb.map((c, i) => Math.abs(c - theirs.rgb[i])));
    if (off > TOLERANCE || mine.alpha !== 1) differs = `is ${value}, the shared --${twin} is ${shared.get(twin)} (${toHex(theirs.rgb)})`;
  }
  if (!differs) matched++;
  if (entry) {
    used.add(`--${name}`);
    if (!differs) problems.push(`--${name} now matches --${twin}: remove it from tokens.parity.allow.json`);
    else if (!entry.reason || !entry.ticket) problems.push(`--${name}: its allowlist entry needs a reason and a ticket`);
    else onPurpose++;
  } else if (differs) {
    problems.push(`--${name} ${differs}. Fix the value, or list it in tokens.parity.allow.json with the reason Tares differs`);
  }
}
for (const name of Object.keys(allow)) if (!used.has(name)) problems.push(`${name} is in tokens.parity.allow.json but not in styles.css: remove the entry`);

for (const p of problems) console.log(`FAIL ${p}`);
console.log(`\n${matched} kit tokens match the shared paper tokens, ${onPurpose} differ on purpose, ${problems.length} problems`);
process.exit(problems.length ? 1 : 0);
