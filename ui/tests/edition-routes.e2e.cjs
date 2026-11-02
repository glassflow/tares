/*
 * Browser e2e for TR-768: the route table of the factory and sre editions and Settings, Advanced.
 *
 * Factory: /settings/advanced is today's Settings page with an Advanced tab that links Connections
 * (/sources) and Explore your data (/explore); /reviews opens the Reviews page (TR-735), /docs lands
 * on /projects, a ticket address on its project with ?view=ticket:<ref>; /sources still opens today's
 * page; the menu is Home, Projects, Tickets, Reviews, Crew, Docs and memory, Settings;
 * ?sample=busy without the browser flag renders the same as /.
 * AI SRE: /timeline is today's Explore; /services and /incidents land on /; /runbooks is its own page.
 * Platform: /settings/advanced lands on /settings, /reviews on /, Settings has no Advanced tab.
 * Every edition: /welcome/edition lands on /. The Advanced tab passes the words check, and is shot at
 * 1440 and 390 wide. useSample (src/editions/sample.ts) is run as written, with a fake browser, for
 * the flag rule: the address alone never shows sample data.
 *
 * Starts three scratch daemons with an empty cell each (TARES_EDITION factory, sre, platform) on
 * TARES_E2E_PORT (default 8837), +1 and +2, MCP port +20, each with a fresh data directory, and stops only those.
 * Run:  npm run test:edition-routes     (builds first: the daemon serves ui/dist; tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const ts = require(path.join(UI_DIR, "node_modules", "typescript"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8837);
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

// Loads src/editions/sample.ts as written: the TypeScript is compiled to CommonJS and run with a fake
// window and a stand-in for the router's useLocation, which reads the address set on `env`.
function loadSample(env) {
  const src = fs.readFileSync(path.join(UI_DIR, "src", "editions", "sample.ts"), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  const window = { get localStorage() { if (env.throws) throw new Error("blocked"); return { getItem: (k) => env.store[k] ?? null }; } };
  vm.runInNewContext(js, {
    module: mod, exports: mod.exports, window, URLSearchParams,
    require: (name) => { if (name === "react-router-dom") return { useLocation: () => ({ search: env.search }) }; throw new Error(name); },
  });
  return mod.exports;
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const homes = {}, daemons = {};
  let browser;
  try {
    // 0. the sample flag, without a browser
    {
      const env = { store: {}, search: "?sample=busy", throws: false };
      const { useSample, SAMPLE_FLAG } = loadSample(env);
      ck("the flag is named taresSamples", SAMPLE_FLAG === "taresSamples", String(SAMPLE_FLAG));
      ck("sample: ?sample=busy alone gives no sample", useSample() === null, String(useSample()));
      env.store = { taresSamples: "0" };
      ck("sample: a flag that is not 1 gives no sample", useSample() === null);
      env.store = { taresSamples: "1" };
      ck("sample: the flag and ?sample=busy give busy", useSample() === "busy", String(useSample()));
      env.search = "";
      ck("sample: the flag without ?sample= gives no sample", useSample() === null);
      env.search = "?sample=busy"; env.throws = true;
      ck("sample: a browser that blocks storage gives no sample", useSample() === null);
    }

    EDITIONS.forEach((e, i) => {
      homes[e] = fs.mkdtempSync(path.join(os.tmpdir(), `tares-edition-routes-${e}-`));
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
    const open = async (width = 1440) => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      return { ctx, page: await ctx.newPage() };
    };
    // Goes to an address and waits until the console has settled (redirects included).
    const go = async (page, e, to) => {
      await page.goto(`${base(e)}${to}`, { waitUntil: "load" });
      await page.waitForLoadState("networkidle");
      await page.locator("main, .main").first().waitFor();
    };
    // Moves inside the console without a page load.
    const goInApp = async (page, to) => {
      await page.evaluate((t) => { history.pushState({}, "", t); window.dispatchEvent(new PopStateEvent("popstate")); }, to);
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300);
    };
    // Where "/" ends up: Home opens Start on a cell with no project of its own, else Projects.
    const homeOf = async (page, e) => { await go(page, e, "/"); return where(page); };
    const where = (page) => { const u = new URL(page.url()); return u.pathname + u.search; };
    const settingsNav = (page) => page.locator('nav[aria-label="Settings"]');
    const advancedTab = (page) => settingsNav(page).getByRole("button", { name: "Advanced", exact: true });
    const menuLabels = async (page) => (await page.locator('nav[aria-label="Main"] a').allInnerTexts()).map((t) => t.trim());

    // 1. factory
    {
      const e = "factory";
      const { page, ctx } = await open();
      await go(page, e, "/settings/advanced");
      ck("factory: /settings/advanced stays on its address", where(page).startsWith("/settings/advanced"), where(page));
      ck("factory: it is today's Settings page", await page.getByRole("heading", { name: "Settings", exact: true }).isVisible());
      ck("factory: with its other tabs", await settingsNav(page).getByRole("button", { name: "Access and API keys" }).isVisible());
      ck("factory: and an Advanced tab, the one open", (await advancedTab(page).count()) === 1
         && (await advancedTab(page).getAttribute("aria-current")) === "page");
      const panel = page.getByTestId("advanced-panel");
      ck("factory: Advanced links Connections to /sources", (await panel.locator('a[href="/sources"]').innerText()).includes("Connections"));
      ck("factory: and Explore your data to /explore", (await panel.locator('a[href="/explore"]').innerText()).includes("Explore your data"));
      const hrefs = await panel.locator("a").evaluateAll((a) => a.map((x) => x.getAttribute("href")));
      ck("factory: and All resources, Connect and Reads", ["/resources", "/connect", "/reads"].every((h) => hrefs.includes(h)), hrefs.join(","));
      ck("factory: the agents line is there", (await panel.innerText()).includes("Agents and rules live in each project, under Advanced setup."));
      await checkWords(page, ck, "factory Advanced tab");
      await shot(page, "advanced-factory-1440.png");

      await panel.locator('a[href="/sources"]').click();
      await page.waitForLoadState("networkidle");
      ck("factory: Connections opens today's Sources page", where(page) === "/sources"
         && await page.getByRole("heading", { name: "Sources", exact: true }).isVisible(), where(page));

      await go(page, e, "/settings");
      await page.getByTestId("section-advanced").waitFor();
      ck("factory: /settings is the new Settings page, six sections", where(page) === "/settings" && (await page.locator("section[id]").count()) === 6 && (await advancedTab(page).count()) === 0);
      await go(page, e, "/sources");
      ck("factory: /sources still opens today's Sources page", where(page) === "/sources"
         && await page.getByRole("heading", { name: "Sources", exact: true }).isVisible(), where(page));
      await go(page, e, "/explore");
      ck("factory: /explore still opens today's Explore page", where(page) === "/explore"
         && await page.getByRole("heading", { name: "Explore", exact: true }).isVisible(), where(page));

      await go(page, e, "/reviews");
      ck("factory: /reviews opens the Reviews page", where(page) === "/reviews"
         && await page.getByRole("heading", { name: "Reviews", exact: true }).isVisible()
         && (await page.getByTestId("factory-reviews").count()) === 1, where(page));
      await go(page, e, "/docs");
      ck("factory: /docs is the Docs and memory page, on a full page load", where(page) === "/docs"
         && await page.getByRole("heading", { name: "Docs and memory", exact: true }).isVisible(), where(page));
      await go(page, e, "/projects/x/tickets/T1");
      ck("factory: a ticket address lands on its project", where(page) === "/projects/x?view=ticket:T1", where(page));
      const home = await homeOf(page, e);
      ck("factory: / is Home, which opens Start on a cell with no project", home === "/" || home === "/start", home);
      for (const p of ["/welcome/edition", "/timeline", "/services", "/services/service-a", "/incidents", "/incidents/i-1", "/runbooks"]) {
        await go(page, e, p);
        ck(`factory: ${p} lands on /`, where(page) === home && !(await page.locator("main").innerText()).includes("does not exist"), where(page));
      }

      await go(page, e, "/projects");
      const items = await menuLabels(page);
      ck("factory: the menu is Home, Projects, Tickets, Reviews, Crew, Docs and memory, Settings", items.join(",") === "Home,Projects,Tickets,Reviews,Crew,Docs and memory,Settings", items.join(","));
      ck("factory: the Reviews entry links to /reviews", (await page.getByTestId("menu-reviews").getAttribute("href")) === "/reviews");
      ck("factory: the Docs and memory entry opens /docs", (await page.locator('nav[aria-label="Main"] a', { hasText: "Docs and memory" }).getAttribute("href")) === "/docs");

      // the sample address alone changes nothing on a live cell
      await go(page, e, "/");
      const plain = await page.locator("main").innerText();
      await go(page, e, "/?sample=busy");
      const sampled = await page.locator("main").innerText();
      ck("factory: /?sample=busy without the flag renders the same as /", plain.trim().length > 0 && sampled === plain,
         `${plain.length} vs ${sampled.length} characters`);
      await ctx.close();

      const narrow = await open(390);
      await go(narrow.page, e, "/settings/advanced");
      await narrow.page.getByTestId("advanced-panel").waitFor();
      ck("factory 390: the Advanced tab is the one shown", (await narrow.page.locator(".pnav-select select").inputValue()) === "advanced");
      ck("factory 390: the page does not scroll sideways", await narrow.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      await checkWords(narrow.page, ck, "factory 390 Advanced tab");
      await shot(narrow.page, "advanced-factory-390.png");
      await narrow.ctx.close();
    }

    // 2. AI SRE
    {
      const e = "sre";
      const { page, ctx } = await open();
      await go(page, e, "/timeline");
      ck("sre: /timeline stays on its address", where(page) === "/timeline", where(page));
      ck("sre: and shows today's Explore", await page.getByRole("heading", { name: "Explore", exact: true }).isVisible());
      const home = await homeOf(page, e);
      for (const p of ["/services", "/services/service-a", "/incidents", "/incidents/i-1", "/reviews", "/tickets", "/welcome/edition"]) {
        await go(page, e, p);
        ck(`sre: ${p} lands on /`, where(page) === home && !(await page.locator("main").innerText()).includes("does not exist"), where(page));
      }
      await goInApp(page, "/docs");
      ck("sre: /docs lands on /", where(page) === home, where(page));
      await go(page, e, "/settings");
      ck("sre: /settings stays today's page, not the factory Settings", where(page) === "/settings" && (await page.getByTestId("section-crew").count()) === 0
         && (await page.locator("section[id]").count()) !== 6 && (await advancedTab(page).count()) === 1);
      await go(page, e, "/settings/advanced");
      ck("sre: /settings/advanced has the Advanced tab open", (await advancedTab(page).getAttribute("aria-current")) === "page"
         && (await page.getByTestId("advanced-panel").locator('a[href="/sources"]').count()) === 1);
      await go(page, e, "/projects");
      const items = await menuLabels(page);
      ck("sre: the menu is Home, Timeline, Runbooks and memory, Settings", items.join(",") === "Home,Timeline,Runbooks and memory,Settings", items.join(","));
      await ctx.close();
    }

    // 3. platform: today's console, no new path
    {
      const e = "platform";
      const { page, ctx } = await open();
      await go(page, e, "/settings/advanced");
      ck("platform: /settings/advanced lands on /settings", where(page) === "/settings", where(page));
      await page.getByRole("heading", { name: "Settings", exact: true }).waitFor();
      ck("platform: Settings has no Advanced tab", (await advancedTab(page).count()) === 0);
      await go(page, e, "/settings?tab=advanced");
      ck("platform: ?tab=advanced shows no Advanced panel", (await page.getByTestId("advanced-panel").count()) === 0
         && (await advancedTab(page).count()) === 0);
      const home = await homeOf(page, e);
      for (const p of ["/reviews", "/tickets", "/services", "/incidents", "/timeline", "/runbooks", "/welcome/edition"]) {
        await go(page, e, p);
        ck(`platform: ${p} lands on /`, where(page) === home && !(await page.locator("main, .main").first().innerText()).includes("does not exist"), where(page));
      }
      await goInApp(page, "/docs");
      ck("platform: /docs lands on /", where(page) === home, where(page));
      await go(page, e, "/projects/x/tickets/T1");
      ck("platform: a ticket address stays and says the page does not exist", where(page) === "/projects/x/tickets/T1"
         && (await page.locator("main, .main").first().innerText()).includes("does not exist"), where(page));
      await go(page, e, "/sources");
      ck("platform: /sources is Sources", await page.getByRole("heading", { name: "Sources", exact: true }).isVisible());
      await shot(page, "settings-platform-1440.png");
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
