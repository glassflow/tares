/*
 * Browser e2e for TR-725: /dev/components shows every edition component, square and flat, in light
 * and dark, and fits a 390px screen. Runs the real daemon on a scratch home, serving the built console.
 * Run:  npm run build && node tests/component-gallery.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep gallery-light.png, gallery-dark.png and gallery-narrow.png.
 * TARES_E2E_PORT sets the daemon port (default 8831; the MCP port is the next one).
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8831);
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

const TESTIDS = ["btn-primary", "btn-secondary", "btn-quiet", "list-section", "panel", "tabs", "dialog-open", "empty-state", "stat",
  "progress", "initials", "status-label", "skeleton", "input", "textarea", "select", "switch", "checkbox", "banner", "toast-open",
  "sparkline", "line-chart"];

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-gallery-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const env = { ...process.env, TARES_EDITION: "platform", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    const id = (t) => page.getByTestId(t);
    const shot = async (n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const style = (t, props) => id(t).first().evaluate((e, ps) => { const s = getComputedStyle(e); return ps.map((p) => s[p]); }, props);
    const wide = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    const open = async () => { await page.goto(`${BASE}/dev/components`, { waitUntil: "networkidle" }); await id("dev-components").waitFor(); };

    await open();
    for (const t of TESTIDS) ck(`${t} is on the page`, await id(t).first().isVisible());

    // Square and flat: radius 0, no shadow.
    for (const t of ["btn-primary", "btn-secondary", "panel", "input"]) {
      const [r, sh] = await style(t, ["borderRadius", "boxShadow"]);
      ck(`${t} has square corners and no shadow`, r === "0px" && sh === "none", `${r} / ${sh}`);
    }
    await id("dialog-open").click();
    await id("dialog").waitFor();
    const [dr, dsh] = await style("dialog", ["borderRadius", "boxShadow"]);
    ck("the dialog opens, square, with no shadow", dr === "0px" && dsh === "none", `${dr} / ${dsh}`);
    await page.keyboard.press("Escape");
    await id("dialog").waitFor({ state: "detached" });

    await id("toast-open").click();
    const toast = page.locator("li[data-state=open]").filter({ hasText: "Settings for project-x are saved." });
    ck("the toast shows", await toast.isVisible());

    // The charts: a polyline, and two series as paths with a dashed mark.
    ck("the sparkline draws a polyline", (await id("sparkline").locator("polyline").count()) === 1);
    ck("the line chart has two series as paths", (await id("line-chart").locator("path").count()) === 2);
    ck("it has at least one dashed mark with a label", (await id("line-chart").locator("line[stroke-dasharray] + line title").count()) >= 1);
    ck("it names both series in its legend", /Errors/.test(await id("line-chart").innerText()) && /Requests per minute/.test(await id("line-chart").innerText()));
    ck("an empty chart says so", await page.getByText("No data in this window.").isVisible());
    ck("and draws no path", (await id("line-chart-empty").locator("path").count()) === 0);

    ck("no horizontal scroll at 1440px", (await wide()) <= 0, `${await wide()}px`);
    await page.getByTestId("theme-light").click();
    await shot("gallery-light.png");

    await page.evaluate(() => localStorage.setItem("tares-theme", "dark"));
    await open();
    ck("dark theme applies from storage", (await page.evaluate(() => document.documentElement.getAttribute("data-theme"))) === "dark");
    ck("the page background changes with the theme", (await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) !== "rgb(255, 255, 255)");
    await shot("gallery-dark.png");

    await page.setViewportSize({ width: 390, height: 844 });
    await open();
    ck("no horizontal scroll at 390px", (await wide()) <= 0, `${await wide()}px`);
    ck("the line chart fits at 390px", await id("line-chart").evaluate((e) => e.getBoundingClientRect().right <= window.innerWidth));
    await shot("gallery-narrow.png");

    ck("no console errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
