/*
 * Browser e2e for TR-1269: axe on the component gallery (light and dark) and on two edition pages,
 * Factory Home (the busy sample) and SRE Home (one incident). A serious or critical violation fails,
 * unless it is in KNOWN below with its reason; the rest are printed as notes.
 * Runs two real daemons on scratch homes, a factory cell and an AI SRE cell, serving the built console.
 * Run:  npm run build && node tests/a11y.e2e.cjs     (from ui/, tares installed in ../.venv)
 * TARES_E2E_PORT sets the first daemon's port (default 8891); the SRE cell takes it +2, each MCP port +1.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { AxeBuilder } = require(path.join(UI_DIR, "node_modules", "@axe-core", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8891);
const SRE_PORT = PORT + 2;
const FAILS = new Set(["serious", "critical"]);
// What axe checks: WCAG 2.1 A and AA. Best practices are not rules here.
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

// Known serious findings, each held here until its ticket (TR-1391) lands, so CI stays green while any NEW
// serious or critical violation fails. A node is known when its rule matches and axe's message names
// the colour. Both are shared paper values (argus-ui theme.css, the skill, Tares): the fix goes there
// first. An entry that matches nothing any more fails the run, so the list can only shrink.
const KNOWN = [
  { rule: "color-contrast", has: "foreground color: #218a45",
    why: "--ok green on paper is 4.14:1 (3.79:1 on a hovered row) for the 11px status label; AA asks 4.5:1" },
  { rule: "color-contrast", has: "foreground color: #cc2d27",
    why: "the danger red on the error banner's tint is 4.37:1; AA asks 4.5:1" },
];
const knownHits = new Map(KNOWN.map((k) => [k, 0]));
const knownOf = (v, n) => KNOWN.find((k) => k.rule === v.id && [...n.any, ...n.all, ...n.none].some((c) => (c.message || "").includes(k.has)));

let pass = 0, fail = 0, notes = 0;
const ck = (label, cond, detail = "") => {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}${detail ? `  (${detail})` : ""}`); }
};

async function waitFor(url, tries = 150) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, PYTHONPATH: REPO, TARES_OTLP_GRPC_PORT: "off", ...extra };
}

/** One violation, as a line a person can act on: the rule, how bad, where. */
const describe = (v) => `${v.id} (${v.impact}): ${v.help}. ${v.nodes.slice(0, 3).map((n) => `${n.html.slice(0, 120)} ${(n.any[0] || n.all[0] || n.none[0] || {}).message || ""}`.trim()).join(" | ")}${v.nodes.length > 3 ? ` and ${v.nodes.length - 3} more` : ""}`;

async function audit(page, label) {
  const { violations } = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const bad = [];
  for (const v of violations.filter((v) => FAILS.has(v.impact))) {
    const unknown = v.nodes.filter((n) => { const k = knownOf(v, n); if (k) knownHits.set(k, knownHits.get(k) + 1); return !k; });
    if (unknown.length) bad.push({ ...v, nodes: unknown });
  }
  for (const v of violations.filter((v) => !FAILS.has(v.impact))) { notes++; console.log(`  note ${label}: ${describe(v)}`); }
  ck(`${label}: no serious or critical axe violation`, bad.length === 0, bad.map(describe).join("; "));
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const homes = [fs.mkdtempSync(path.join(os.tmpdir(), "tares-a11y-factory-")), fs.mkdtempSync(path.join(os.tmpdir(), "tares-a11y-sre-"))];
  execFileSync(python, [path.join(__dirname, "seed-sre-home.py"), homes[1], "new-cell"], { cwd: REPO, encoding: "utf8", env: cleanEnv({}) });
  const start = (edition, home, port) => spawn(tares, ["up", "--port", String(port), "--data-dir", home], {
    cwd: REPO, stdio: "ignore", detached: true,
    env: cleanEnv({ TARES_EDITION: edition, TARES_HOME: home, TARES_PORT: String(port), TARES_MCP_PORT: String(port + 1), TARES_UI_DIST: dist }),
  });
  const daemons = [start("factory", homes[0], PORT), start("sre", homes[1], SRE_PORT)];
  let browser;
  try {
    ck("factory daemon up", await waitFor(`http://127.0.0.1:${PORT}/health`));
    ck("sre daemon up", await waitFor(`http://127.0.0.1:${SRE_PORT}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const open = async (port, route, ready, theme = "light") => {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
      await ctx.addInitScript((t) => { try { localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares-theme", t); } catch { /* blocked */ } }, theme);
      const page = await ctx.newPage();
      await page.goto(`http://127.0.0.1:${port}${route}`, { waitUntil: "load" });
      await page.getByTestId(ready).first().waitFor();
      await page.waitForLoadState("networkidle");
      return { ctx, page };
    };

    console.log("== the component gallery ==");
    for (const theme of ["light", "dark"]) {
      const { ctx, page } = await open(PORT, "/dev/components", "dev-components", theme);
      await audit(page, `gallery, ${theme}`);
      await ctx.close();
    }

    console.log("== edition pages ==");
    for (const [label, port, route, ready] of [
      ["Factory Home, busy", PORT, "/?sample=busy", "factory-home"],
      ["SRE Home, one incident", SRE_PORT, "/?sample=one-incident", "sre-home"],
    ]) {
      const { ctx, page } = await open(port, route, ready);
      await audit(page, label);
      await ctx.close();
    }
    console.log("== known findings ==");
    for (const [k, hits] of knownHits) {
      console.log(`  known ${k.rule} ${k.has}: ${k.why} (${hits} elements)`);
      ck(`known finding still present, else remove it from KNOWN: ${k.rule} ${k.has}`, hits > 0);
    }
  } catch (e) {
    ck("the run finished", false, e.message.split("\n")[0]);
  } finally {
    if (browser) await browser.close();
    for (const d of daemons) { try { process.kill(-d.pid); } catch { /* gone */ } }
    for (const h of homes) fs.rmSync(h, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed, ${notes} notes`);
  process.exit(fail ? 1 : 0);
})();
