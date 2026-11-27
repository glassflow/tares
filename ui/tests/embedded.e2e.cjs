/*
 * Browser e2e for TR-1553: the console's embedded mode. The Tares desktop opens the console with
 * ?embed=desktop&theme=light|dark; the console then leaves out its own header (brand, cell name,
 * workspace switcher, search box, account menu with its theme row and sign out), keeps its menu with
 * search at the top of it, and paints the desktop's theme without keeping it as the browser's choice.
 * The mode lasts for the tab (sessionStorage), across moves inside the console and a reload.
 * A browser visit with no params is unchanged, and ?embed= with another value leaves the mode.
 *
 * Starts two scratch daemons with an empty cell each (TARES_EDITION factory, platform) on
 * TARES_E2E_PORT (default 8871) and +1, each with a fresh data directory, and stops only those.
 * Run:  npm run build && TARES_E2E_PORT=<free port> node tests/embedded.e2e.cjs
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require("./browser.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8871);
const SHOTS = process.env.SHOTS_DIR;
const EDITIONS = ["factory", "platform"];
const base = (e) => `http://127.0.0.1:${PORT + EDITIONS.indexOf(e)}`;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 150) {
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
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const homes = {}, daemons = {};
  let browser;
  try {
    EDITIONS.forEach((e, i) => {
      homes[e] = fs.mkdtempSync(path.join(os.tmpdir(), `tares-embedded-${e}-`));
      const env = { ...process.env, TARES_EDITION: e, TARES_HOME: homes[e], TARES_PORT: String(PORT + i),
                    TARES_MCP_PORT: String(PORT + 20 + i), TARES_UI_DIST: dist,
                    PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
      for (const k of ["TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET", "TARES_SLACK_CONNECT_URL", "TARES_AUTH_TOKEN",
                       "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete env[k];
      daemons[e] = spawn(tares, ["up", "--port", String(PORT + i), "--data-dir", homes[e]], { cwd: REPO, stdio: "ignore", env, detached: true });
      console.log(`  daemon ${e}: pid ${daemons[e].pid}, port ${PORT + i}`);
    });
    for (const e of EDITIONS) ck(`${e} daemon up`, await waitFor(`${base(e)}/health`));

    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: false }); };

    // A fresh tab whose browser already holds a key (so Sign out would show) and a saved theme choice,
    // set once before the first load only: a later write by the page must stay visible to the checks.
    const open = async (width, saved) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      await ctx.addInitScript((s) => {
        if (sessionStorage.getItem("e2e-seeded")) return;
        sessionStorage.setItem("e2e-seeded", "1");
        localStorage.setItem("tares_token", "scratch-key-not-real");
        localStorage.setItem("tares-theme", s);
      }, saved);
      return { ctx, page: await ctx.newPage() };
    };
    const go = async (page, e, to) => {
      await page.goto(`${base(e)}${to}`, { waitUntil: "load" });
      await page.waitForLoadState("networkidle");
    };
    const attr = (page, a) => page.evaluate((n) => document.documentElement.getAttribute(n), a);
    const savedTheme = (page) => page.evaluate(() => localStorage.getItem("tares-theme"));
    const count = (page, id) => page.getByTestId(id).count();
    const signOuts = (page) => page.getByText("Sign out", { exact: true }).count();

    // 1. a browser visit is unchanged: header, account menu with the theme row and Sign out, saved theme
    {
      const { page, ctx } = await open(1440, "dark");
      await go(page, "factory", "/crew");
      await page.getByTestId("edition-header").waitFor();
      ck("browser: the header is there", (await count(page, "edition-header")) === 1 && (await count(page, "brand-mark")) === 1);
      ck("browser: the saved theme is painted", (await attr(page, "data-theme")) === "dark");
      ck("browser: no embedded mark on the page", (await attr(page, "data-embed")) === null);
      ck("browser: no search in the menu", (await count(page, "menu-search")) === 0);
      await page.getByTestId("account-button").click();
      ck("browser: the account menu has the theme row and Sign out",
         (await count(page, "theme-row")) === 1 && (await signOuts(page)) === 1);
      await shot(page, "browser-1440.png");
      await ctx.close();
    }

    // 2. embedded, wide: no header, the menu with search on top, the desktop's theme over a saved one
    {
      const { page, ctx } = await open(1440, "dark");
      await go(page, "factory", "/crew?embed=desktop&theme=light");
      await page.getByTestId("edition-menu").waitFor();
      ck("embedded: no header, no brand, no account button",
         (await count(page, "edition-header")) === 0 && (await count(page, "embed-bar")) === 0
         && (await count(page, "brand-mark")) === 0 && (await count(page, "account-button")) === 0);
      ck("embedded: no theme switch and no Sign out anywhere",
         (await count(page, "theme-row")) === 0 && (await signOuts(page)) === 0);
      ck("embedded: no cell name and no workspace switcher",
         (await count(page, "edition-name")) === 0 && (await count(page, "workspace-switcher")) === 0);
      ck("embedded: the boot script marked the page before the app ran", (await attr(page, "data-embed")) === "desktop");
      ck("embedded: the desktop's light theme wins over the saved dark", (await attr(page, "data-theme")) === "light");
      ck("embedded: the saved browser choice is left as it was", (await savedTheme(page)) === "dark");
      const top = await page.getByTestId("edition-menu").evaluate((n) => n.getBoundingClientRect().top);
      ck("embedded: the menu starts at the top of the view", Math.abs(top) < 1, `top ${top}`);
      ck("embedded: the menu keeps its pages", await page.getByTestId("menu-tickets").isVisible());
      ck("embedded: an empty page-actions row takes no room",
         (await page.getByTestId("embed-actions").evaluate((n) => n.getBoundingClientRect().height)) === 0);
      await shot(page, "embedded-light-1440.png");

      await page.getByTestId("menu-search").click();
      ck("embedded: search on top of the menu opens Ask",
         await page.getByRole("dialog", { name: "Ask your data" }).isVisible());
      await page.keyboard.press("Escape");

      // a move inside the console drops the query; the mode stays
      await page.getByTestId("menu-tickets").click();
      await page.waitForURL(/\/tickets$/);
      await page.waitForLoadState("networkidle");
      ck("embedded: after a move inside, the address has no params", !new URL(page.url()).search);
      ck("embedded: after a move inside, still no header", (await count(page, "edition-header")) === 0);
      ck("embedded: after a move inside, still light", (await attr(page, "data-theme")) === "light");
      await page.reload({ waitUntil: "networkidle" });
      await page.getByTestId("edition-menu").waitFor();
      ck("embedded: a reload of the tab keeps the mode and its theme",
         (await count(page, "edition-header")) === 0 && (await attr(page, "data-theme")) === "light");

      // ?embed= with another value leaves the mode; the saved choice comes back
      await go(page, "factory", "/crew?embed=none");
      await page.getByTestId("edition-header").waitFor();
      ck("leaving: ?embed=none brings the header back", (await count(page, "edition-header")) === 1);
      ck("leaving: and the browser's saved theme", (await attr(page, "data-theme")) === "dark" && (await attr(page, "data-embed")) === null);
      await ctx.close();
    }

    // 3. the other way round: the desktop's dark over a saved light; a page's header actions get a row
    {
      const { page, ctx } = await open(1440, "light");
      await go(page, "factory", "/crew?embed=desktop&theme=dark");
      await page.getByTestId("edition-menu").waitFor();
      ck("embedded: the desktop's dark wins over the saved light", (await attr(page, "data-theme")) === "dark");
      ck("embedded: the saved light is left as it was", (await savedTheme(page)) === "light");
      await shot(page, "embedded-dark-1440.png");
      await go(page, "factory", "/setup");
      const exit = page.getByTestId("setup-exit");
      await exit.waitFor();
      ck("embedded: Setup's way out sits in the page-actions row",
         await page.getByTestId("embed-actions").getByTestId("setup-exit").isVisible());
      ck("embedded: Setup has no header", (await count(page, "edition-header")) === 0);
      await shot(page, "embedded-setup-1440.png");
      await ctx.close();
    }

    // 4. embedded, phone width: a slim bar with Menu and search only, and the drawer still opens
    {
      const { page, ctx } = await open(390, "dark");
      await go(page, "factory", "/crew?embed=desktop&theme=light");
      await page.getByTestId("embed-bar").waitFor();
      ck("embedded 390: a slim bar, not the header", (await count(page, "edition-header")) === 0);
      ck("embedded 390: the bar has Menu and search",
         await page.getByTestId("menu-button").isVisible() && await page.getByTestId("edition-search-button").isVisible());
      ck("embedded 390: no brand, cell name or account button",
         (await count(page, "brand-mark")) === 0 && (await count(page, "edition-name")) === 0 && (await count(page, "account-button")) === 0);
      await page.getByTestId("menu-button").click();
      await page.getByTestId("edition-menu").waitFor();
      ck("embedded 390: the drawer opens with the pages", await page.getByTestId("edition-menu").getByTestId("menu-tickets").isVisible());
      await shot(page, "embedded-390.png");
      await ctx.close();
    }

    // 5. the platform edition: its sidebar keeps the pages and loses brand, theme switch and Sign out
    {
      const { page, ctx } = await open(1440, "dark");
      await go(page, "platform", "/projects");
      await page.locator(".sidebar").waitFor();
      ck("platform browser: brand, theme switch and Sign out are there",
         (await page.locator(".sidebar .brand").count()) === 1
         && (await page.locator('.sidebar [title="Toggle light / dark"]').count()) === 1 && (await signOuts(page)) === 1);
      await go(page, "platform", "/projects?embed=desktop&theme=light");
      await page.locator(".sidebar").waitFor();
      ck("platform embedded: no brand, no theme switch, no Sign out",
         (await page.locator(".sidebar .brand").count()) === 0
         && (await page.locator('.sidebar [title="Toggle light / dark"]').count()) === 0 && (await signOuts(page)) === 0);
      ck("platform embedded: the pages stay", await page.getByRole("link", { name: "Sources", exact: true }).isVisible());
      ck("platform embedded: the desktop's theme is painted", (await attr(page, "data-theme")) === "light");
      await shot(page, "platform-embedded-1440.png");
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    for (const e of EDITIONS) {
      const d = daemons[e];
      if (d && d.exitCode === null) { try { process.kill(-d.pid, "SIGTERM"); } catch { d.kill("SIGTERM"); } }
      if (homes[e]) try { fs.rmSync(homes[e], { recursive: true, force: true }); } catch { /* left for the OS */ }
    }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
