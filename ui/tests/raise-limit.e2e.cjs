/*
 * Browser e2e for TR-670: an agent at its daily run cap shows "Raise the limit" on the project's
 * health banner. It used to open the agent's own page, which has no cap field; it must open
 * Settings, Limits, with the daily cap field in view and marked for that agent.
 *
 * Seeds a scratch cell (tests/seed-capped-agent.py), then runs the real daemon on it, serving the
 * built console.
 * Run:  npm run build && node tests/raise-limit.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of the banner and of where the button lands.
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
// short enough that the cap field starts below the fold, so "in view" needs the scroll (TR-671)
const VIEW_H = 400;

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-raise-limit-e2e-"));
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
    const page = await browser.newPage({ viewport: { width: 1280, height: VIEW_H } });
    await page.goto(`${BASE}/projects/${project}`, { waitUntil: "networkidle" });
    const raise = page.getByRole("button", { name: "Raise the limit" }).or(page.getByRole("link", { name: "Raise the limit" })).first();
    await raise.waitFor();
    ck("the banner says watcher reached its limit", (await page.locator(".gf-health").innerText()).includes("watcher reached its limit of 1 run a day"));
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "1-banner.png") });

    await raise.click();
    await page.waitForLoadState("networkidle");
    const url = new URL(page.url());
    ck("it opens the console's Settings, not the agent page", url.pathname === "/settings", page.url());
    ck("on the Limits tab, for this agent", url.searchParams.get("tab") === "agents" && url.searchParams.get("agent") === "watcher", page.url());

    const field = page.getByTestId("daily-cap-field");
    await field.waitFor();
    ck("the daily cap field is shown", (await field.locator("input").inputValue()) === "1");
    const said = (await field.innerText()).replace(/\s+/g, " ");
    ck("it says who it is for", said.includes("watcher stopped at this limit"));
    ck("and that an agent with its own limit is exempt", said.includes("every agent without its own limit"), said);
    await page.waitForTimeout(500);   // let the scroll settle
    const box = await field.boundingBox();
    ck("the field is in view", !!box && box.y >= 0 && box.y + box.height <= VIEW_H, JSON.stringify(box));
    ck("the page scrolled to it", (await page.evaluate(() => window.scrollY)) > 0);
    ck("the field is marked", /field-focus/.test(await field.getAttribute("class")));
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "2-settings-limits.png") });

    // Settings opened from the sidebar, with no agent asked for, shows the plain field
    await page.goto(`${BASE}/settings?tab=agents`, { waitUntil: "networkidle" });
    const plain = page.getByTestId("daily-cap-field");
    await plain.waitFor();
    ck("without an agent the field is not marked", !/field-focus/.test(await plain.getAttribute("class")));
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
