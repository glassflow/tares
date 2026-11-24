/*
 * Browser e2e for TR-1553: the console embedded in the desktop (?embed=desktop&theme=light|dark).
 *
 * The desktop draws its own rail and top bar, so an embedded console drops its header, its menu and its
 * drawer and gives the page the whole width; it paints the desktop's theme before the first paint, over
 * the saved one, and never saves it. The mode holds for the tab (sessionStorage) after a link drops the
 * query, and a console opened without the parameter is what it always was.
 *
 * Starts two scratch daemons with an empty cell each (TARES_EDITION factory and sre) on TARES_E2E_PORT
 * (default 9962) and +1, each with a fresh data directory, and stops only those.
 * Run:  npm run test:embedded      (builds first: the daemon serves ui/dist; tares installed in ../.venv or on PATH)
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

const PORT = Number(process.env.TARES_E2E_PORT || 9962);
const SHOTS = process.env.SHOTS_DIR;
const EDITIONS = ["factory", "sre"];
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

    // opts.saved: the theme the person saved in the console; every data-theme value the page ever wears is
    // collected in window.__themes, so "no flash" is read from what was painted, not from where it ended.
    const open = async (width, opts = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: opts.system || "light" });
      if (opts.saved) await ctx.addInitScript((t) => { try { localStorage.setItem("tares-theme", t); } catch { /* blocked */ } }, opts.saved);
      await ctx.addInitScript(() => {
        window.__themes = [];
        const note = () => { const t = document.documentElement && document.documentElement.getAttribute("data-theme"); if (t) window.__themes.push(t); };
        new MutationObserver(note).observe(document, { attributes: true, subtree: true, attributeFilter: ["data-theme"] });
      });
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (err) => errors.push(String(err)));
      return { page, ctx, errors };
    };
    const visit = async (page, e, to) => {
      await page.goto(`${base(e)}${to}`, { waitUntil: "load" });
      await page.locator("main.main").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const chrome = (page) => page.evaluate(() => ({
      header: document.querySelectorAll('[data-testid="edition-header"]').length,
      menu: document.querySelectorAll('nav[aria-label="Main"]').length,
      menuButton: document.querySelectorAll('[data-testid="menu-button"]').length,
      brand: document.querySelectorAll('[data-testid="brand-word"]').length,
      switcher: document.querySelectorAll('[data-testid="workspace-switcher"]').length,
      account: document.querySelectorAll('[data-testid="account-button"], [data-testid="theme-row"]').length,
    }));
    const mainBox = (page) => page.evaluate(() => {
      const m = document.querySelector("main.main"), r = m.getBoundingClientRect();
      return { top: r.top, left: r.left, width: r.width, vw: window.innerWidth, max: getComputedStyle(m).maxWidth, text: m.innerText.trim().length };
    });
    const theme = (page) => page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    const saved = (page) => page.evaluate(() => localStorage.getItem("tares-theme"));

    // 1. without the parameter the console is what it was: header, menu, its own saved theme
    for (const e of EDITIONS) {
      const { page, ctx, errors } = await open(1440, { saved: "dark" });
      await visit(page, e, "/projects");
      const c = await chrome(page);
      ck(`${e}: plain, the header is there`, c.header === 1 && c.brand === 1, JSON.stringify(c));
      ck(`${e}: plain, the menu is there`, c.menu === 1, JSON.stringify(c));
      ck(`${e}: plain, the saved theme wins`, (await theme(page)) === "dark");
      ck(`${e}: plain, main keeps its width cap`, (await mainBox(page)).max === "1600px");
      ck(`${e}: plain, no page error`, errors.length === 0, errors.join(" | "));
      await ctx.close();
    }

    // 2. ?embed=desktop drops the chrome and keeps the page
    for (const e of EDITIONS) {
      const { page, ctx, errors } = await open(1440);
      await visit(page, e, "/projects?embed=desktop&theme=light");
      const c = await chrome(page);
      ck(`${e}: embedded, no header`, c.header === 0 && c.brand === 0 && c.account === 0, JSON.stringify(c));
      ck(`${e}: embedded, no menu and no workspace switcher`, c.menu === 0 && c.switcher === 0, JSON.stringify(c));
      const b = await mainBox(page);
      ck(`${e}: embedded, the page starts at the top (no bar above it)`, b.top < 1, `top ${b.top}`);
      ck(`${e}: embedded, the page starts at the left (no menu beside it)`, b.left < 1, `left ${b.left}`);
      ck(`${e}: embedded, the page takes the whole width`, Math.abs(b.width - b.vw) < 1 && b.max === "none", `width ${b.width} of ${b.vw}, max ${b.max}`);
      ck(`${e}: embedded, the page itself is drawn`, b.text > 0);
      ck(`${e}: embedded, no page error`, errors.length === 0, errors.join(" | "));
      await shot(page, `${e}-embedded-1440.png`);
      await ctx.close();
    }

    // 3. the same on a phone width: no Menu button, no drawer
    {
      const { page, ctx } = await open(390);
      await visit(page, "factory", "/projects?embed=desktop");
      const c = await chrome(page);
      ck("narrow: embedded, no header, no Menu button and no drawer", c.header === 0 && c.menuButton === 0 && c.menu === 0, JSON.stringify(c));
      await ctx.close();
    }

    // 4. the theme: the desktop's wins over the saved one, from the first paint, and is not saved
    for (const [want, had] of [["dark", "light"], ["light", "dark"]]) {
      const { page, ctx } = await open(1440, { saved: had, system: had });
      await visit(page, "factory", `/projects?embed=desktop&theme=${want}`);
      ck(`embedded theme=${want}: painted over a saved ${had} theme`, (await theme(page)) === want);
      const seen = await page.evaluate(() => window.__themes);
      ck(`embedded theme=${want}: no flash, only ${want} was ever painted`, seen.length > 0 && seen.every((t) => t === want), seen.join(","));
      ck(`embedded theme=${want}: the saved theme is left as it was`, (await saved(page)) === had, String(await saved(page)));
      await ctx.close();
    }
    {
      const { page, ctx } = await open(1440, { system: "dark" });
      await visit(page, "factory", "/projects?embed=desktop&theme=light");
      ck("embedded theme=light: wins over a dark system theme", (await theme(page)) === "light");
      ck("embedded theme=light: nothing saved", (await saved(page)) === null);
      await ctx.close();
    }
    {
      // a theme the desktop did not ask for (or asked for wrongly) leaves the saved one
      const { page, ctx } = await open(1440, { saved: "dark" });
      await visit(page, "factory", "/projects?embed=desktop");
      ck("embedded, no theme asked: the saved theme stands", (await theme(page)) === "dark");
      await page.goto(`${base("factory")}/projects?embed=desktop&theme=purple`, { waitUntil: "load" });
      await page.locator("main.main").waitFor();
      ck("embedded, an unknown theme is ignored", (await theme(page)) === "dark");
      await ctx.close();
    }

    // 4b. a theme set from inside an embedded console (the gallery has the switch; the shell's own account
    // menu is gone) is painted and not saved either
    {
      const { page, ctx } = await open(1440);
      await page.goto(`${base("factory")}/dev/components?embed=desktop&theme=dark`, { waitUntil: "load" });
      await page.getByTestId("theme-light").click();
      ck("embedded: a theme picked inside the console is painted", (await theme(page)) === "light");
      ck("embedded: and not saved over the person's own", (await saved(page)) === null, String(await saved(page)));
      await ctx.close();
    }
    {
      const { page, ctx } = await open(1440);
      await page.goto(`${base("factory")}/dev/components`, { waitUntil: "load" });
      await page.getByTestId("theme-dark").click();
      ck("plain: a theme picked in the console is saved, as before", (await saved(page)) === "dark", String(await saved(page)));
      await ctx.close();
    }

    // 5. the mode holds for the tab: a link that drops the query is still embedded, a new tab is not
    {
      const { page, ctx } = await open(1440);
      await visit(page, "factory", "/projects?embed=desktop&theme=dark");
      await page.goto(`${base("factory")}/settings`, { waitUntil: "load" });
      await page.locator("main.main").waitFor();
      const c = await chrome(page);
      ck("embedded: the next page, with no query, is still embedded", c.header === 0 && c.menu === 0, JSON.stringify(c));
      ck("embedded: and still the desktop's theme", (await theme(page)) === "dark");
      const other = await ctx.newPage();
      await other.goto(`${base("factory")}/projects`, { waitUntil: "load" });
      await other.locator("main.main").waitFor();
      const o = await chrome(other);
      ck("a new tab of the same browser is not embedded", o.header === 1 && o.menu === 1, JSON.stringify(o));
      await ctx.close();
    }

    // 6. a new query replaces the stored one; another value of embed is no embed
    {
      const { page, ctx } = await open(1440);
      await visit(page, "factory", "/projects?embed=desktop&theme=dark");
      await page.goto(`${base("factory")}/projects?embed=desktop&theme=light`, { waitUntil: "load" });
      await page.locator("main.main").waitFor();
      ck("embedded: a new query theme replaces the stored one", (await theme(page)) === "light");
      await ctx.close();
    }
    {
      const { page, ctx } = await open(1440);
      await visit(page, "factory", "/projects?embed=mobile&theme=dark");
      const c = await chrome(page);
      ck("embed=mobile is not an embed: the header and menu stay", c.header === 1 && c.menu === 1, JSON.stringify(c));
      await ctx.close();
    }

    // 7. a page that fills the header's slots (Setup) draws without them and without an error
    {
      const { page, ctx, errors } = await open(1440);
      await visit(page, "factory", "/setup?embed=desktop");
      const c = await chrome(page);
      ck("embedded: Setup draws with no header to put its crumb in", c.header === 0 && (await mainBox(page)).text > 0);
      ck("embedded: Setup raises no page error", errors.length === 0, errors.join(" | "));
      await shot(page, "factory-setup-embedded.png");
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
