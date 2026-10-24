/*
 * Browser e2e for TR-671: an agent at its daily run cap says when its next run is allowed, and its
 * run list folds the runs the cap refused into one line.
 *
 * Seeds a scratch cell (tests/seed-capped-agent.py: one run 22h48m ago, then 8 refused ones), then
 * runs the real daemon on it, serving the built console.
 * Run:  npm run build && node tests/capped-runs.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of the banner and of the run list.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8813);
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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-capped-runs-e2e-"));
  const dist = path.join(UI_DIR, "dist");
  const project = execFileSync(python, [path.join(__dirname, "seed-capped-agent.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop();
  ck("seeded a project", /^uc_/.test(project), project);

  const env = { ...process.env, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_AGENT_DAILY_CAP: "1", ANTHROPIC_API_KEY: "sk-ant-scratch-not-real" };
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });

    await page.goto(`${BASE}/projects/${project}`, { waitUntil: "networkidle" });
    const banner = page.locator(".gf-health");
    await banner.waitFor();
    const text = await banner.innerText();
    ck("the banner says watcher reached its limit", text.includes("watcher reached its limit of 1 run a day"), text);
    // the one counted run is 22h48m old, so the window frees up in 1h 12m (a few seconds have passed)
    ck("the banner says when the next run is allowed", /next run allowed in 1h 1[12]m/.test(text), text);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "1-banner.png") });

    await page.goto(`${BASE}/agents/watcher`, { waitUntil: "networkidle" });
    await page.locator("table tbody tr").first().waitFor();
    const rows = page.locator("table tbody tr.clickable");
    const group = page.getByTestId("capped-group");
    ck("the refused runs are one line", (await group.count()) === 1, `${await group.count()} groups`);
    ck("it counts them", /8 runs skipped, capped/.test(await group.first().innerText().catch(() => "")));
    ck("the list is that line and the run that did happen", (await rows.count()) === 2, `${await rows.count()} rows`);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "2-runs.png"), fullPage: true });

    await group.first().click();
    ck("opening it lists each refused run", (await rows.count()) === 2 + 8, `${await rows.count()} rows`);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "3-runs-open.png"), fullPage: true });
    await group.first().click();
    ck("closing it folds them again", (await rows.count()) === 2, `${await rows.count()} rows`);
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
