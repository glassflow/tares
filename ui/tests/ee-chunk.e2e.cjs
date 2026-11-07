/*
 * Browser e2e for TR-926: the enterprise part of the console is its own chunk (dist/assets/ee-<hash>.js)
 * and only a cell whose licence allows enterprise ever requests it.
 *
 * Starts scratch daemons through tests/licensed.cjs (the test licence root is trusted in that process
 * only), each in a fresh data directory, on TARES_E2E_PORT (default 8846) and the ports after it
 * (daemon + MCP, two each), and stops only those.
 *   - no licence (factory cell): /, /people and /settings/advanced never request an ee chunk.
 *   - valid licence: the chunk is requested exactly once, however many pages are opened.
 *   - expired licence: the chunk is still requested (the cell's ee_allowed stays true).
 *   - a licensed cell where the chunk fails to load: the console still renders, with a console warning.
 * Every page also loads with no page error.
 * Run:  cd ui && npm run build && TARES_E2E_PORT=9400 node tests/ee-chunk.e2e.cjs
 */
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8846);
const PATHS = ["/", "/people", "/settings/advanced"];
const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

// Opens PATHS in one page (a fresh load each) and says which ee chunk requests and page errors it saw.
async function visit(browser, base, { blockChunk = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const eeRequests = [], errors = [], warnings = [];
  page.on("request", (r) => { if (EE_CHUNK.test(r.url())) eeRequests.push(r.url()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "warning") warnings.push(m.text()); });
  if (blockChunk) await page.route(EE_CHUNK, (route) => route.abort());
  for (const p of PATHS) {
    await page.goto(`${base}${p}`, { waitUntil: "load" });
    await page.locator("main").first().waitFor();
    await page.waitForLoadState("networkidle");
  }
  const licence = await page.evaluate(() => fetch("/api/licence").then((r) => r.json()));
  await ctx.close();
  return { eeRequests, errors, warnings, licence };
}

(async () => {
  const daemons = [];
  let browser;
  try {
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const start = async (n, licence) => {
      const d = await startDaemon({ port: PORT + 2 * n, edition: "factory", licence });
      daemons.push(d);
      return d;
    };

    const none = await start(0, "none");
    const a = await visit(browser, none.base);
    ck("no licence: the cell says ee is not allowed", a.licence.ee_allowed === false, JSON.stringify(a.licence));
    ck("no licence: no ee chunk requested on /, /people, /settings/advanced", a.eeRequests.length === 0, a.eeRequests.join(" "));
    ck("no licence: no page error", a.errors.length === 0, a.errors.join(" | "));

    const valid = await start(1, "valid");
    const b = await visit(browser, valid.base);
    ck("valid licence: the cell says ee is allowed", b.licence.ee_allowed === true, JSON.stringify(b.licence));
    ck("valid licence: the ee chunk is requested", b.eeRequests.length >= 1, "none requested");
    ck("valid licence: the ee chunk is requested once per page load, not per slot", b.eeRequests.length === PATHS.length, String(b.eeRequests.length));
    ck("valid licence: no page error", b.errors.length === 0, b.errors.join(" | "));

    // One page load, several in-app moves: still one request.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      const reqs = [];
      page.on("request", (r) => { if (EE_CHUNK.test(r.url())) reqs.push(r.url()); });
      await page.goto(`${valid.base}/`, { waitUntil: "load" });
      await page.locator("main").first().waitFor();
      await page.waitForLoadState("networkidle");
      for (const href of ["/people", "/settings/advanced", "/"]) {
        await page.evaluate((h) => { window.history.pushState({}, "", h); window.dispatchEvent(new PopStateEvent("popstate")); }, href);
        await page.waitForLoadState("networkidle");
      }
      ck("valid licence: moving between pages in the console does not fetch the chunk again", reqs.length === 1, String(reqs.length));
      await ctx.close();
    }

    const expired = await start(2, "expired");
    const c = await visit(browser, expired.base);
    ck("expired licence: the cell still says ee is allowed", c.licence.ee_allowed === true && c.licence.expired === true, JSON.stringify(c.licence));
    ck("expired licence: the ee chunk is requested", c.eeRequests.length >= 1, "none requested");

    const d = await visit(browser, valid.base, { blockChunk: true });
    ck("chunk fails to load: the console still renders every page", d.errors.length === 0, d.errors.join(" | "));
    ck("chunk fails to load: it is said in the console, not thrown",
       d.warnings.some((w) => /enterprise part of the console did not load/i.test(w)), d.warnings.join(" | "));
  } catch (e) {
    ck("script ran to the end", false, String(e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    for (const d of daemons) d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
