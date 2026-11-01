/*
 * Browser e2e for TR-728: the shell of the factory and sre editions. Header (mark, Tares, edition
 * name, search, account button), the left menu with only the entries that have a page and Settings
 * last, the account menu (Sign out only with a key, a version row), the narrow layout (a Menu button
 * and a drawer) and the platform edition keeping its old sidebar.
 *
 * Starts three scratch daemons with an empty cell each (TARES_EDITION factory, sre, platform) on
 * TARES_E2E_PORT (default 8440), +1 and +2, each with a fresh data directory, and stops only those.
 * Run:  npm run build && node tests/edition-shell.e2e.cjs     (from ui/, tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8440);
const SHOTS = process.env.SHOTS_DIR;
const EDITIONS = ["factory", "sre", "platform"];
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
      homes[e] = fs.mkdtempSync(path.join(os.tmpdir(), `tares-edition-shell-${e}-`));
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
    const labels = (page) => page.locator('nav[aria-label="Main"] a').allInnerTexts();
    const open = async (e, width, withKey = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      if (withKey) await ctx.addInitScript(() => localStorage.setItem("tares_token", "scratch-key-not-real"));
      const page = await ctx.newPage();
      await page.goto(`${base(e)}/projects`, { waitUntil: "networkidle" });
      return { page, ctx };
    };

    // 1. each edition at 1440: header and menu
    const expect = {
      factory: { name: "Software Factory", menu: ["Home", "Projects", "Crew", "Settings"] },
      sre: { name: "AI SRE", menu: ["Home", "Timeline", "Settings"] },
    };
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 1440);
      const header = page.getByTestId("edition-header");
      await header.waitFor();
      const htext = await header.innerText();
      ck(`${e}: the header says Tares`, /Tares/.test(htext), htext);
      ck(`${e}: and the edition name`, (await page.getByTestId("edition-name").innerText()) === expect[e].name);
      const items = (await labels(page)).map((t) => t.trim());
      ck(`${e}: the menu is ${expect[e].menu.join(", ")}`, items.join(",") === expect[e].menu.join(","), items.join(","));
      ck(`${e}: nothing in the menu says Sources`, !items.includes("Sources"));
      ck(`${e}: Settings is the last row`, items[items.length - 1] === "Settings");
      ck(`${e}: the old sidebar is not there`, (await page.locator(".sidebar").count()) === 0);
      const box = await page.locator('nav[aria-label="Main"]').boundingBox();
      ck(`${e}: the menu is 232 wide`, Math.abs(box.width - 232) < 1, String(box.width));
      const sbox = await page.getByTestId("edition-search").boundingBox();
      ck(`${e}: the search field is 280 by 36`, Math.abs(sbox.width - 280) < 1 && Math.abs(sbox.height - 36) < 1, `${sbox.width}x${sbox.height}`);
      const active = await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts();
      ck(`${e}: on /projects the active row is ${e === "factory" ? "Projects" : "none"}`,
         active.join(",") === (e === "factory" ? "Projects" : ""), active.join(","));
      if (e === "sre") {
        await page.goto(`${base(e)}/timeline`, { waitUntil: "networkidle" });
        ck("sre: Timeline has no page yet and lands on the not-found page, inside the shell",
           (await page.getByTestId("edition-header").count()) === 1);
      }
      await shot(page, `${e}-1440.png`);
      await page.goto(`${base(e)}/crew`, { waitUntil: "networkidle" });
      await checkWords(page, ck, `${e} page`);

      // search, then Enter, opens the Ask dialog
      const search = page.getByTestId("edition-search");
      await search.fill("what changed today");
      await search.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Ask your data" });
      ck(`${e}: search and Enter open the Ask dialog`, await dialog.isVisible());
      ck(`${e}: and clear the field`, (await search.inputValue()) === "");
      await page.keyboard.press("Escape");
      ck(`${e}: Escape closes it`, (await dialog.count()) === 0);
      await search.press("Enter");
      ck(`${e}: Enter on an empty field opens nothing`, (await dialog.count()) === 0);
      await ctx.close();
    }

    // 2. the account menu: without a key there is no Sign out
    for (const e of ["factory", "sre"]) {
      let { page, ctx } = await open(e, 1440);
      await page.getByTestId("account-button").click();
      const menu = page.getByTestId("account-menu");
      ck(`${e}: the account button opens the menu`, await menu.isVisible());
      ck(`${e}: it has a Light or Dark mode row`, (await menu.getByRole("menuitem", { name: /(Light|Dark) mode/ }).count()) === 1);
      ck(`${e}: it has a version row`, /^Version\s+Tares \d/.test((await page.getByTestId("account-version").innerText()).replace(/\s+/g, " ")),
         await page.getByTestId("account-version").innerText().catch(() => ""));
      ck(`${e}: no key, no Sign out`, (await menu.getByText("Sign out").count()) === 0);
      ck(`${e}: no workspace link on a self-hosted cell`, (await menu.getByText("Workspace").count()) === 0);
      await page.getByTestId("edition-name").click();
      ck(`${e}: a click outside closes it`, (await menu.count()) === 0);
      await page.getByTestId("account-button").click();
      await page.keyboard.press("Escape");
      ck(`${e}: Escape closes it`, (await menu.count()) === 0);
      await ctx.close();

      ({ page, ctx } = await open(e, 1440, true));
      await page.getByTestId("account-button").click();
      ck(`${e}: with a key, Sign out is there`, (await page.getByTestId("account-menu").getByRole("menuitem", { name: "Sign out" }).count()) === 1);
      if (e === "factory") await shot(page, "factory-account-menu.png");
      await ctx.close();
    }

    // 3. narrow: a Menu button and a drawer
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 390);
      await page.getByTestId("menu-button").waitFor();
      ck(`${e} 390: the menu is hidden`, (await page.locator('nav[aria-label="Main"]').count()) === 0);
      ck(`${e} 390: the search field is an icon button`, (await page.getByTestId("edition-search").count()) === 0
         && await page.getByTestId("edition-search-button").isVisible());
      ck(`${e} 390: the page does not scroll sideways`, await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      await page.getByTestId("menu-button").click();
      const nav = page.locator('nav[aria-label="Main"]');
      await nav.waitFor();
      const items = (await labels(page)).map((t) => t.trim());
      ck(`${e} 390: the drawer has the same entries`, items.join(",") === expect[e].menu.join(","), items.join(","));
      ck(`${e} 390: the drawer is 260 wide`, Math.abs((await nav.boundingBox()).width - 260) < 1);
      if (e === "factory") await shot(page, "factory-390-drawer.png");
      await page.keyboard.press("Escape");
      ck(`${e} 390: Escape closes the drawer`, (await nav.count()) === 0);
      await page.getByTestId("menu-button").click();
      await page.getByTestId("edition-scrim").click({ position: { x: 340, y: 300 } });
      ck(`${e} 390: the scrim closes the drawer`, (await nav.count()) === 0);
      await page.getByTestId("menu-button").click();
      await nav.getByRole("link", { name: "Settings" }).click();
      await page.waitForURL(/\/settings/);
      ck(`${e} 390: choosing a link closes the drawer`, (await nav.count()) === 0);
      await page.getByTestId("edition-search-button").click();
      ck(`${e} 390: the search icon opens the Ask dialog`, await page.getByRole("dialog", { name: "Ask your data" }).isVisible());
      await ctx.close();
    }

    // 4. a long cell name in the sre header shrinks with an ellipsis; the edition name does not
    {
      const long = "glassflow-web-eu-central-production-readonly";
      const ctx = await browser.newContext({ viewport: { width: 390, height: 900 } });
      const page = await ctx.newPage();
      // The cloud's /health points at a workspace list; this one names this very host.
      await page.route(`${base("sre")}/health`, async (route) => {
        const res = await route.fetch();
        await route.fulfill({ response: res, json: { ...(await res.json()), workspaces_url: "http://cp.test/workspaces" } });
      });
      await page.route("http://cp.test/workspaces", (route) => route.fulfill({
        status: 200, contentType: "application/json", headers: { "access-control-allow-origin": base("sre"), "access-control-allow-credentials": "true" },
        body: JSON.stringify({ workspaces: [{ slug: long, host: `127.0.0.1:${PORT + 1}`, url: "", role: "owner", owner_email: "", state: "active",
                                              is_default: true, open_url: "" }], links: {} }),
      }));
      await page.goto(`${base("sre")}/projects`, { waitUntil: "networkidle" });
      const cell = page.getByTestId("cell-name");
      await cell.waitFor();
      ck("sre: the cell name carries a title with the full name", (await cell.getAttribute("title")) === long);
      ck("sre: it is cut short at 390 wide", await cell.evaluate((el) => el.scrollWidth > el.clientWidth));
      const name = page.getByTestId("edition-name");
      ck("sre: the edition name is whole", await name.evaluate((el) => el.scrollWidth <= el.clientWidth && el.clientWidth > 40));
      await shot(page, "sre-390-long-name.png");
      await ctx.close();
    }

    // 5. the platform edition keeps its sidebar
    {
      const { page, ctx } = await open("platform", 1440);
      await page.locator(".sidebar").waitFor();
      ck("platform: the old sidebar is there with Sources", await page.getByRole("link", { name: "Sources", exact: true }).isVisible());
      ck("platform: no edition header", (await page.getByTestId("edition-header").count()) === 0);
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
