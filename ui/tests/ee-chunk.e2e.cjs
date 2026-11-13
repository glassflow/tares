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
 *   - the chunk failed and a team Waiting row's Agree is pressed: the row says the part did not load, not that a
 *     licence is needed (#502 F3). Home has no Projects box at all (TR-1275), with the chunk loaded or not.
 *   - a licensed cell whose licence read fails: Agree and Answer say the cell did not answer, never that a licence is
 *     needed (#507 F1), at 1440 and 390.
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

    // The built files: the ee chunk is reached only through a dynamic import. If the entry chunk imported
    // it statically or index.html preloaded it, every cell would fetch it, licensed or not.
    {
      const fs = require("fs");
      const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
      const assets = fs.readdirSync(path.join(dist, "assets"));
      const eeFiles = assets.filter((f) => /^ee-[^/]*\.js$/.test(f));
      ck("build: exactly one ee chunk is emitted", eeFiles.length === 1, eeFiles.join(" "));
      const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
      ck("build: index.html does not mention the ee chunk", !/\bee-[\w-]+\.js/.test(html), "found in index.html");
      const statics = [];
      for (const f of assets.filter((f) => f.endsWith(".js") && !eeFiles.includes(f))) {
        const text = fs.readFileSync(path.join(dist, "assets", f), "utf8");
        // static imports are `from"./ee-x.js"` or `import"./ee-x.js"`; the lazy one is `import("./ee-x.js")`
        if (/(?:from|import)\s*["'][^"']*\/?ee-[\w-]+\.js["']/.test(text)) statics.push(f);
      }
      ck("build: no other chunk imports the ee chunk statically", statics.length === 0, statics.join(" "));
    }

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

    // A team home read from the cell, with the waiting row and the project rows swapped in by the page.
    const teamHome = async (page, { waiting = [], projects = [] }) => {
      await page.route(/\/api\/home(\?|$)/, async (route) => {
        const res = await route.fetch();
        const data = await res.json();
        return route.fulfill({ response: res, json: { ...data, summary: "", counts: { mine: 0, team: projects.length },
          waiting_for_you: waiting, waiting_total: waiting.length, projects, you_are_waiting_on: [], crews: [], crews_quiet: [], areas: [] } });
      });
    };
    const grant = { id: "grant-1", kind: "grant", label: "Grant", title: "Agree to merge to acme/web", detail: "Kim asks to merge.",
                    meta: "", project: null, repo: null, from: { name: "Kim" }, since: null, waited: "1 minute",
                    actions: [{ label: "Agree", primary: true, do: "agree", grant: "g1" }, { label: "Decline", primary: false, do: "decline", grant: "g1" }] };
    const stub = { hidden: true, lead: { name: "Lee" } };
    const failedWords = /This part of the console did not load\. Reload the page to try again\./;
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.route(EE_CHUNK, (route) => route.abort());
      await teamHome(page, { waiting: [grant], projects: [stub] });
      await page.goto(`${valid.base}/`, { waitUntil: "load" });
      await page.getByTestId("waiting-row").first().waitFor();
      await page.waitForLoadState("networkidle");
      ck("chunk fails to load: the core Waiting row still draws", (await page.getByTestId("waiting-row").count()) === 1);
      await page.getByRole("button", { name: "Agree" }).click();
      const err = page.getByTestId("row-error");
      await err.waitFor();
      const words = (await err.innerText()).trim();
      ck("chunk fails to load: Agree says the part did not load", failedWords.test(words), words);
      ck("chunk fails to load: Agree does not say a licence is needed", !/licence/i.test(words), words);
      await ctx.close();
    }
    // The answer to a spec question is the team's part too: with the chunk blocked, Answer says the part did not load (no box opens).
    const spec = { id: "spec-1", kind: "question", label: "Question", title: "Which database?", detail: "Lee asks which database to use.",
                   meta: "", project: null, repo: null, from: { name: "Lee" }, since: null, waited: "1 minute",
                   actions: [{ label: "Answer", primary: true, do: "answer", project: "p1", question: "q1" }] };
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.route(EE_CHUNK, (route) => route.abort());
      await teamHome(page, { waiting: [spec], projects: [stub] });
      await page.goto(`${valid.base}/`, { waitUntil: "load" });
      await page.getByTestId("waiting-row").first().waitFor();
      await page.waitForLoadState("networkidle");
      await page.getByRole("button", { name: "Answer" }).click();
      const err = page.getByTestId("row-error");
      await err.waitFor();
      const words = (await err.innerText()).trim();
      ck("chunk fails to load: Answer says the part did not load", failedWords.test(words), words);
      ck("chunk fails to load: Answer does not say a licence is needed", !/licence/i.test(words), words);
      ck("chunk fails to load: Answer opens no box", (await page.getByTestId("answer-panel").count()) === 0);
      await ctx.close();
    }
    // The licence read fails on a licensed cell: whether it is licensed is not known, so a refused team action says the
    // cell did not answer, not that a licence is needed.
    for (const w of [1440, 390]) {
      for (const [name, row, button] of [["Agree", grant, "Agree"], ["Answer", spec, "Answer"]]) {
        const ctx = await browser.newContext({ viewport: { width: w, height: 900 } });
        const page = await ctx.newPage();
        await page.route(/\/api\/licence(\?|$)/, (route) => route.abort());
        await teamHome(page, { waiting: [row], projects: [stub] });
        await page.goto(`${valid.base}/`, { waitUntil: "load" });
        await page.getByTestId("waiting-row").first().waitFor();
        await page.waitForLoadState("networkidle");
        await page.getByRole("button", { name: button }).click();
        const err = page.getByTestId("row-error");
        await err.waitFor();
        const words = (await err.innerText()).trim();
        ck(`licence read fails @${w}: ${name} says the cell did not answer`, /The cell did not answer\./.test(words), words);
        ck(`licence read fails @${w}: ${name} does not say a licence is needed`, !/needs an enterprise licence/i.test(words), words);
        ck(`licence read fails @${w}: ${name} opens no box`, (await page.getByTestId("answer-panel").count()) === 0);
        await ctx.close();
      }
    }
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await teamHome(page, { waiting: [], projects: [stub] });
      await page.goto(`${valid.base}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
      ck("chunk loaded: Home draws no Projects box and no hidden row, whatever the projects (TR-1275)",
         (await page.getByTestId("projects").count()) === 0 && (await page.getByTestId("hidden-row").count()) === 0);
      await ctx.close();
    }
  } catch (e) {
    ck("script ran to the end", false, String(e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    for (const d of daemons) d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
