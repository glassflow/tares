/*
 * Browser e2e for TR-728: the shell of the factory and sre editions. Header (mark, Tares, edition
 * name, workspace switcher, search, account button), the left menu with only the entries that have a
 * page and Settings last, the account menu, the narrow layout (a Menu button and a drawer) and the
 * platform edition keeping its old sidebar.
 *
 * Every edition is checked closed and with the account menu open at 1440 and 390 wide, and at 390
 * with the drawer open; each state runs the words check, the header overlap check and, where the
 * page has one read call, the one-call check. Platform is shot at both widths.
 *
 * Starts three scratch daemons with an empty cell each (TARES_EDITION factory, sre, platform) on
 * TARES_E2E_PORT (default 8835), +1 and +2, each with a fresh data directory, and stops only those.
 * Run:  npm run test:edition-shell     (builds first: the daemon serves ui/dist; tares installed in ../.venv or on PATH)
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
const { checkWords, offendersIn } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8835);
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

    // Patches one JSON answer of the daemon, leaving the rest of it as it is.
    const patch = (page, e, route, fn) => page.route(`${base(e)}${route}`, async (r) => {
      const res = await r.fetch();
      await r.fulfill({ response: res, json: fn(await res.json()) });
    });
    // opts.key: the console holds a key; opts.who: whoami names a person's key; opts.askKey: Ask has a model key
    const open = async (e, width, opts = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      if (opts.key) await ctx.addInitScript(() => localStorage.setItem("tares_token", "scratch-key-not-real"));
      const page = await ctx.newPage();
      if (opts.who) await patch(page, e, "/api/whoami", (j) => ({ ...j, id: "key-1", name: opts.who }));
      if (opts.askKey) await patch(page, e, "/api/capabilities", (j) => ({ ...j, agent_key_configured: true }));
      return { page, ctx };
    };
    const visit = async (page, e, to) => {
      await page.goto(`${base(e)}${to}`, { waitUntil: "load" });
      await page.getByTestId("edition-header").waitFor();
      await page.waitForLoadState("networkidle");
    };
    // Nothing in the header's left group runs into the right group, and the page does not scroll sideways.
    const fits = async (page, label) => {
      const r = await page.evaluate(() => {
        const [l, rt] = document.querySelector('[data-testid="edition-header"]').children;
        const edge = rt.getBoundingClientRect().left;
        return { bad: [...l.children].filter((c) => c.getBoundingClientRect().right > edge + 0.5).map((c) => c.textContent.trim().slice(0, 24) || c.tagName),
                 sideways: document.documentElement.scrollWidth > window.innerWidth };
      });
      ck(`${label}: nothing in the header runs into the search or account button`, r.bad.length === 0, r.bad.join(", "));
      ck(`${label}: the page does not scroll sideways`, !r.sideways);
    };


    // The shell's own words: the page between the header and menu belongs to its own ticket, so main is
    // left out. The Ask dialog and the account menu are part of the shell and stay in.
    const shellWords = async (page, label) => {
      const text = await page.evaluate(() => {
        const style = document.createElement("style");
        style.textContent = "[data-words-exempt],[data-user-text],main{display:none !important}";
        document.head.appendChild(style);
        try { return document.body.innerText; } finally { style.remove(); }
      });
      const found = offendersIn(text);
      ck(`${label}: the shell has no internal names`, found.filter((w) => w !== "em dash").length === 0, found.join(", "));
      ck(`${label}: and no em dashes`, !found.includes("em dash"));
    };
    // The reads the shell frame adds: GETs to /api/ minus whoami. Used to show the frame reads nothing
    // until the account menu opens, and then reads the version once.
    const trackReads = (page) => {
      const gets = [];
      page.on("request", (r) => { if (r.method() === "GET") { const p = new URL(r.url()).pathname; if (p.startsWith("/api/") && p !== "/api/whoami") gets.push(p); } });
      return gets;
    };

    const expect = {
      factory: { name: "Software Factory", menu: ["Home", "Projects", "Crew", "Settings"], page: "/crew", call: "/api/crew" },
      sre: { name: "AI SRE", menu: ["Home", "Timeline", "Settings"], page: "/explore", call: null },
    };

    // 1. each edition at 1440: header, menu, the pages the menu leads to
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 1440);
      await visit(page, e, "/projects");
      const htext = await page.getByTestId("edition-header").innerText();
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

      // every row of the menu leads to a page
      for (const label of expect[e].menu) {
        await page.locator('nav[aria-label="Main"]').getByRole("link", { name: label, exact: true }).click();
        await page.waitForLoadState("networkidle");
        ck(`${e}: ${label} lands on a page`, (await page.locator("main").innerText()).includes("does not exist") === false);
        if (label === "Timeline") ck("sre: Timeline is Explore for now", new URL(page.url()).pathname === "/explore", page.url());
        await shellWords(page, `${e} ${label}`);
      }
      await ctx.close();
    }

    // 2. closed and account menu open, at 1440 and 390; the drawer at 390
    for (const e of ["factory", "sre"]) {
      for (const w of [1440, 390]) {
        const { page, ctx } = await open(e, w);
        const tag = `${e} ${w}`;
        const reads = trackReads(page);
        await visit(page, e, expect[e].page);
        const atLoad = reads.filter((p) => p === "/api/capabilities").length;
        e === "factory" ? await checkWords(page, ck, `${tag} page`) : await shellWords(page, `${tag} page`);
        await fits(page, tag);
        await shot(page, `${e}-${w}.png`);

        // the account menu: an open cell has no sign-in, so there is no one to name
        await page.getByTestId("account-button").click();
        const menu = page.getByTestId("account-menu");
        await page.getByTestId("account-version").waitFor();
        ck(`${tag}: the account button opens the menu`, await menu.isVisible());
        ck(`${tag}: it has a Light or Dark mode row`, (await menu.getByRole("menuitem", { name: /(Light|Dark) mode/ }).count()) === 1);
        const ver = (await page.getByTestId("account-version").innerText()).replace(/\s+/g, " ");
        ck(`${tag}: it has a version row`, /^Version Tares \d/.test(ver), ver);
        ck(`${tag}: no key, no Sign out`, (await menu.getByText("Sign out").count()) === 0);
        ck(`${tag}: no workspace link on a self-hosted cell`, (await menu.getByText("Workspace").count()) === 0);
        const top = (await menu.innerText()).split("\n").map((t) => t.trim()).filter(Boolean);
        ck(`${tag}: a cell with no sign-in does not name a person`, !/no auth configured/i.test(top.join(" ")) && top[0] === "Account", top.join(" | "));
        ck(`${tag}: and says so`, top[1] === "This cell has no sign-in", top.join(" | "));
        await shellWords(page, `${tag} account menu`);
        await shot(page, `${e}-${w}-account.png`);
        ck(`${tag}: opening the account menu reads the version once`, reads.filter((p) => p === "/api/capabilities").length === atLoad + 1,
           `${atLoad} at load, ${reads.filter((p) => p === "/api/capabilities").length} after`);
        await menu.getByRole("menuitem").first().focus();
        await page.keyboard.press("Escape");
        ck(`${tag}: Escape closes it`, (await menu.count()) === 0);
        ck(`${tag}: and returns to the account button`, await page.getByTestId("account-button").evaluate((el) => el === document.activeElement));
        await page.getByTestId("account-button").click();
        await page.getByTestId("edition-name").click();
        ck(`${tag}: a click outside closes it`, (await menu.count()) === 0);
        ck(`${tag}: opening it again reads nothing more`, reads.filter((p) => p === "/api/capabilities").length === atLoad + 1);

        if (w === 390) {
          ck(`${tag}: the menu is hidden`, (await page.locator('nav[aria-label="Main"]').count()) === 0);
          ck(`${tag}: the search field is an icon button`, (await page.getByTestId("edition-search").count()) === 0
             && await page.getByTestId("edition-search-button").isVisible());
          await page.getByTestId("menu-button").click();
          const nav = page.locator('nav[aria-label="Main"]');
          await nav.waitFor();
          const items = (await labels(page)).map((t) => t.trim());
          ck(`${tag}: the drawer has the same entries`, items.join(",") === expect[e].menu.join(","), items.join(","));
          ck(`${tag}: the drawer is 260 wide`, Math.abs((await nav.boundingBox()).width - 260) < 1);
          await shellWords(page, `${tag} drawer`);
          await shot(page, `${e}-390-drawer.png`);
          await nav.getByRole("link").first().focus();
          await page.keyboard.press("Escape");
          ck(`${tag}: Escape closes the drawer`, (await nav.count()) === 0);
          ck(`${tag}: and returns to the Menu button`, await page.getByTestId("menu-button").evaluate((el) => el === document.activeElement));
          await page.getByTestId("menu-button").click();
          await page.getByTestId("edition-scrim").click({ position: { x: 340, y: 300 } });
          ck(`${tag}: the scrim closes the drawer`, (await nav.count()) === 0);
          await page.getByTestId("menu-button").click();
          await nav.getByRole("link", { name: "Settings" }).click();
          await page.waitForURL(/\/settings/);
          ck(`${tag}: choosing a link closes the drawer`, (await nav.count()) === 0);
          await shellWords(page, `${tag} settings`);
        }
        await ctx.close();
      }
    }

    // 3. narrower still: 360 wide, both editions
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 360);
      await visit(page, e, expect[e].page);
      await fits(page, `${e} 360`);
      await ctx.close();
    }

    // 4. a person's key names the person; a key shows Sign out
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 1440, { key: true, who: "Alex Kim" });
      await visit(page, e, "/projects");
      ck(`${e}: the initials button shows the name's initials`, (await page.getByTestId("account-button").innerText()).trim() === "AK",
         await page.getByTestId("account-button").innerText());
      await page.getByTestId("account-button").click();
      const top = (await page.getByTestId("account-menu").innerText()).split("\n").map((t) => t.trim()).filter(Boolean);
      ck(`${e}: the menu names the key and says it is an API key`, top[0] === "Alex Kim" && top[1] === "Signed in with an API key", top.join(" | "));
      ck(`${e}: with a key, Sign out is there`, (await page.getByTestId("account-menu").getByRole("menuitem", { name: "Sign out" }).count()) === 1);
      await shellWords(page, `${e} key account menu`);
      await ctx.close();
    }

    // 5. search and Enter prefill the Ask dialog; the question is not sent
    for (const e of ["factory", "sre"]) {
      const { page, ctx } = await open(e, 1440, { askKey: true });
      await visit(page, e, expect[e].page);
      const posts = [];
      page.on("request", (r) => { if (r.method() === "POST" && r.url().includes("/api/agent/chat")) posts.push(r.url()); });
      const search = page.getByTestId("edition-search");
      await search.fill("what changed today");
      await search.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Ask your data" });
      ck(`${e}: search and Enter open the Ask dialog`, await dialog.isVisible());
      ck(`${e}: and clear the field`, (await search.inputValue()) === "");
      await dialog.locator("textarea").waitFor();
      ck(`${e}: the question waits in the dialog's field`, (await dialog.locator("textarea").inputValue()) === "what changed today");
      await page.waitForTimeout(300);
      ck(`${e}: and is not sent`, posts.length === 0, posts.join(", "));
      await page.keyboard.press("Escape");
      ck(`${e}: Escape closes it`, (await dialog.count()) === 0);
      await search.press("Enter");
      ck(`${e}: Enter on an empty field opens nothing`, (await dialog.count()) === 0);
      await ctx.close();
    }

    // 6. the cloud's workspace switcher: in the header when wide, at the top of the menu when narrow
    const long = "glassflow-web-eu-central-production-readonly";
    const cloudPage = async (e, width) => {
      const { page, ctx } = await open(e, width);
      // The cloud's /health points at a workspace list; this one names this very host.
      await patch(page, e, "/health", (j) => ({ ...j, workspaces_url: "http://cp.test/workspaces" }));
      await page.route("http://cp.test/workspaces", (route) => route.fulfill({
        status: 200, contentType: "application/json", headers: { "access-control-allow-origin": base(e), "access-control-allow-credentials": "true" },
        body: JSON.stringify({ workspaces: [
          { slug: long, host: `127.0.0.1:${PORT + EDITIONS.indexOf(e)}`, url: "", role: "owner", owner_email: "", state: "active", is_default: true, open_url: "" },
          { slug: "second", host: "second.test", url: "", role: "member", owner_email: "", state: "active", is_default: false, open_url: "http://second.test/" },
        ], links: {} }),
      }));
      return { page, ctx };
    };
    for (const e of ["factory", "sre"]) {
      const plain = await open(e, 1440);
      await visit(plain.page, e, expect[e].page);
      const plainHeight = await plain.page.getByTestId("edition-header").evaluate((el) => el.getBoundingClientRect().height);
      await plain.ctx.close();
      let { page, ctx } = await cloudPage(e, 1440);
      await visit(page, e, expect[e].page);
      const sw = page.getByTestId("workspace-switcher");
      await sw.waitFor();
      ck(`${e}: wide, the workspace switcher is in the header`, (await sw.locator(".ws-switch").innerText()).includes(long));
      await sw.locator(".ws-switch").click();
      ck(`${e}: it lists the other workspaces`, await page.locator(".ws-pop").getByText("second").isVisible());
      await shot(page, `${e}-1440-switcher.png`);
      await page.keyboard.press("Escape");
      await fits(page, `${e} cloud 1440`);
      const swHeight = await page.getByTestId("edition-header").evaluate((el) => el.getBoundingClientRect().height);
      ck(`${e}: the header is as tall with the switcher as without (${plainHeight})`, Math.abs(swHeight - plainHeight) < 1, `${swHeight}`);
      ck(`${e}: the switcher keeps no margin of its own`, await sw.locator(".ws-wrap").evaluate((el) => getComputedStyle(el).marginBottom === "0px"));
      await ctx.close();

      ({ page, ctx } = await cloudPage(e, 390));
      await visit(page, e, expect[e].page);
      ck(`${e}: narrow, the header has no switcher`, (await page.locator('[data-testid="edition-header"] [data-testid="workspace-switcher"]').count()) === 0);
      if (e === "sre") {
        const cell = page.getByTestId("cell-name");
        await cell.waitFor();
        ck("sre: the cell name carries a title with the full name", (await cell.getAttribute("title")) === long);
        ck("sre: it is cut short at 390 wide", await cell.evaluate((el) => el.scrollWidth > el.clientWidth));
        ck("sre: the edition name is whole", await page.getByTestId("edition-name").evaluate((el) => el.scrollWidth <= el.clientWidth && el.clientWidth > 40));
      }
      await fits(page, `${e} cloud 390`);
      await page.getByTestId("menu-button").click();
      ck(`${e}: narrow, the switcher is at the top of the menu`, await page.locator('nav[aria-label="Main"] [data-testid="workspace-switcher"]').isVisible());
      ck(`${e}: and keeps no margin of its own`, await page.locator('nav[aria-label="Main"] .ws-wrap').evaluate((el) => getComputedStyle(el).marginBottom === "0px"));
      await shellWords(page, `${e} cloud drawer`);
      if (e === "sre") await shot(page, "sre-390-long-name.png");
      await ctx.close();
    }

    // 7. the platform edition keeps its sidebar
    for (const w of [1440, 390]) {
      const { page, ctx } = await open("platform", w);
      await page.goto(`${base("platform")}/projects`, { waitUntil: "networkidle" });
      if (w === 1440) {
        await page.locator(".sidebar").waitFor();
        ck("platform: the old sidebar is there with Sources", await page.getByRole("link", { name: "Sources", exact: true }).isVisible());
      }
      ck(`platform ${w}: no edition header`, (await page.getByTestId("edition-header").count()) === 0);
      await shot(page, `platform-${w}.png`);
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
