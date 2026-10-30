/*
 * Browser e2e for TR-693: the project page says a build station "is waiting for you" only when it
 * needs the person (a permission prompt), not whenever its turn ends (it may wait on its own
 * Monitor or on the reviewer, and resumes by itself).
 *
 * Seeds a scratch cell (tests/seed-project-idle.py runs the plugin's real hook script for builders:
 * one idle after a turn, one stopped at a permission prompt, one idle for 30 minutes), then runs
 * the real daemon on it, serving the built console.
 * Run:  npm run build && node tests/project-idle.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CODE points at another checkout to run its daemon
 * and hook script (the "before" shots run main's); TARES_UI_DIST at its console build.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const CODE = process.env.TARES_E2E_CODE ? path.resolve(process.env.TARES_E2E_CODE) : REPO;
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8825);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-project-idle-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const env = { ...process.env, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, ANTHROPIC_API_KEY: "sk-ant-scratch-not-real",
                PYTHONPATH: CODE };   // the venv installs one checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: CODE, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    const ship = path.join(CODE, "claude-plugin/scripts/ship.py");
    const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-project-idle.py"), String(PORT), ship,
      path.join(home, "plugin")], { cwd: CODE, encoding: "utf8", env }).trim().split("\n").pop());
    ck("seeded three projects", Object.keys(ids).length === 3, JSON.stringify(ids));

    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const open = async (name) => {
      await page.goto(`${BASE}/projects/${ids[name]}`);
      await page.waitForSelector(".fo", { timeout: 15000 });
      await page.waitForTimeout(1500);   // the sessions and tickets load after the shell
    };
    const banner = () => page.locator(".fo-attn").allInnerTexts();

    console.log("== a builder idle between turns: no banner ==");
    await open("fb-90a1");
    let texts = await banner();
    ck("no 'waiting for you' banner", !texts.some((t) => /waiting for you/i.test(t)), texts.join(" | "));
    ck("no banner of any kind", texts.length === 0, texts.join(" | "));
    ck("the ticket is not called stopped", !/stopped/i.test(await page.locator(".fo").first().innerText()));
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "idle-builder.png") });

    console.log("== a builder idle for 30 minutes: still no banner ==");
    await open("Slow build");
    texts = await banner();
    ck("no banner for an idle session past the 10-minute working window", texts.length === 0, texts.join(" | "));
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "idle-30-min.png") });

    console.log("== a builder at a permission prompt: banner ==");
    await open("Billing export");
    texts = await banner();
    ck("the banner says the station is waiting for you",
       texts.some((t) => /billing-export-build-t4-api is waiting for you/.test(t)), texts.join(" | "));
    ck("it carries what Claude Code said", texts.some((t) => /needs your permission to use Bash/.test(t)), texts.join(" | "));
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "permission-prompt.png") });
  } finally {
    if (browser) await browser.close();
    daemon.kill();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
