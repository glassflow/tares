/*
 * Browser e2e for Export connections on /sources/export (TR-1394): the list and its words, picking,
 * what each export asks the daemon for, the file (copy with and without a clipboard, download), a file
 * that no longer matches the picks, secrets, a refused and a failed export, a read-only key, nothing to
 * export, the list failing, the sre words, light and dark, 1440 and 390. The daemon is real; the list,
 * the export and the key are mocked at the network (catalog-export.fixture.cjs).
 * Run:  npm run build && node tests/catalog-export.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const fs = require("fs");
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockExport } = require("./catalog-export.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9978);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=export-page]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return el ? el.innerText : ""; } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  // clipboard: "ok" records what was copied, "none" has no clipboard at all (a page over plain http).
  const open = async ({ width = 1440, dark = false, clipboard = "ok", ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, acceptDownloads: true,
                                           ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript((mode) => {
      window.__copied = [];
      const value = mode === "none" ? undefined : { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } };
      Object.defineProperty(navigator, "clipboard", { value, configurable: true });
    }, clipboard);
    await mockExport(page, mock);
    await page.goto(`${d.base}/sources/export`, { waitUntil: "load" });
    await page.getByTestId("export-page").waitFor({ timeout: 15000 });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const row = (page, name) => page.locator(`[data-testid=export-connection][data-name="${name}"]`);
  const box = (page, name) => row(page, name).getByRole("checkbox");
  const isOn = async (loc) => (await loc.count()) === 1 && (await loc.getAttribute("aria-checked")) === "true";
  const run = (page) => page.getByTestId("export-run");
  const exportNow = async (page) => {
    await run(page).click();
    await Promise.race([page.getByTestId("export-result").waitFor({ timeout: 5000 }), page.getByTestId("export-error").waitFor({ timeout: 5000 })]).catch(() => {});
  };
  const yamlText = async (page) => (await page.getByTestId("export-yaml").count()) === 1 ? page.getByTestId("export-yaml").textContent() : "";

  try {
    // ── the list, all picked ───────────────────────────────────────────────────────────────────────
    {
      const calls = [];
      const { ctx, page, errors } = await open({ calls });
      await page.getByTestId("export-connections").waitFor({ timeout: 5000 });
      ck("list: titled in page words", (await page.getByRole("heading", { level: 1 }).innerText()) === "Export connections");
      ck("list: says what comes with them", /with the rules, agents and projects/.test(await flat(page.locator("[data-testid=export-page] > header"))), await flat(page.locator("[data-testid=export-page] > header")));
      ck("list: one row per connection", (await page.getByTestId("export-connection").count()) === 3);
      ck("list: all are picked to start", (await isOn(box(page, "shop-web"))) && (await isOn(box(page, "gh-main"))) && (await isOn(box(page, "metrics"))));
      ck("list: the kind's label, not its key", /Docker logs/.test(await flat(row(page, "shop-web"))), await flat(row(page, "shop-web")));
      ck("list: the names are user text", (await row(page, "shop-web").locator("[data-user-text]").first().count()) === 1);
      ck("list: the one with a secret says so", (await row(page, "gh-main").getByTestId("export-has-secret").count()) === 1);
      ck("list: the others don't", (await page.getByTestId("export-has-secret").count()) === 1);
      const hint = await flat(page.getByTestId("export-secrets"));
      ck("secrets: left out, and said so", /Left out by default/.test(hint), hint);
      ck("secrets: counts the picked ones that have one", /1 picked connection has one\./.test(hint), hint);
      ck("secrets: no warning while off", (await page.getByTestId("export-secrets-warn").count()) === 0);
      ck("export: the button counts the picks", (await flat(run(page))) === "Export 3 connections", await flat(run(page)));
      ck("export: no file yet", (await page.getByTestId("export-result").count()) === 0);
      await words(page, "list");

      // all picked: the same call as the agent's and MCP's (no sources, no secrets)
      await exportNow(page);
      ck("export all: asks without a list", calls.length === 1 && calls[0].sources === null, JSON.stringify(calls));
      ck("export all: asks without secrets", calls.length === 1 && calls[0].include_secrets === null, JSON.stringify(calls));
      const all = await yamlText(page);
      ck("file: shows what came back", /name: shop-web/.test(all) && /name: metrics/.test(all), all);
      ck("file: is system text", (await page.getByTestId("export-yaml").getAttribute("data-words-exempt")) !== null);
      await words(page, "file");

      // copy
      await page.getByTestId("export-copy").click();
      await page.getByRole("button", { name: "Copied" }).waitFor({ timeout: 3000 }).catch(() => {});
      ck("copy: the whole file", JSON.stringify(await page.evaluate(() => window.__copied)) === JSON.stringify([all]));
      ck("copy: the button says so", (await flat(page.getByTestId("export-copy"))) === "Copied");
      await page.getByRole("button", { name: "Copy", exact: true }).waitFor({ timeout: 3000 }).catch(() => {});
      ck("copy: the button goes back", (await flat(page.getByTestId("export-copy"))) === "Copy");

      // download
      const dl = await Promise.all([page.waitForEvent("download", { timeout: 5000 }), page.getByTestId("export-download").click()])
        .then(([x]) => x, () => null);
      ck("download: a .yaml file", !!dl && dl.suggestedFilename() === "tares-connections.yaml", dl ? dl.suggestedFilename() : "no download");
      ck("download: holds the file shown", !!dl && fs.readFileSync(await dl.path(), "utf8") === all);

      // picking one out: the file no longer matches, so it goes
      await box(page, "gh-main").click();
      ck("pick: unticked", !(await isOn(box(page, "gh-main"))));
      ck("pick: the old file goes", (await page.getByTestId("export-result").count()) === 0);
      ck("pick: the button counts again", (await flat(run(page))) === "Export 2 connections", await flat(run(page)));
      ck("pick: no picked one has a secret now", !/has one|have one/.test(await flat(page.getByTestId("export-secrets"))), await flat(page.getByTestId("export-secrets")));
      ck("pick: the toggle offers all again", (await flat(page.getByTestId("export-all"))) === "Pick all");
      await exportNow(page);
      ck("export some: asks for the picked ones only", calls.length === 2 && calls[1].sources === "shop-web,metrics", JSON.stringify(calls));
      ck("export some: the file has only them", !/gh-main/.test(await yamlText(page)), await yamlText(page));

      // secrets on
      await page.getByTestId("export-include-secrets").click();
      ck("secrets on: a warning", /plain text/.test(await flat(page.getByTestId("export-secrets-warn"))));
      ck("secrets on: the old file goes", (await page.getByTestId("export-result").count()) === 0);
      await exportNow(page);
      ck("secrets on: asks for them", calls.length === 3 && calls[2].include_secrets === "true", JSON.stringify(calls));
      await words(page, "secrets on");

      // none picked
      await page.getByTestId("export-all").click();   // all
      ck("pick all: all picked", (await isOn(box(page, "gh-main"))) && (await flat(page.getByTestId("export-all"))) === "Pick none");
      await page.getByTestId("export-all").click();   // none
      ck("pick none: none picked", !(await isOn(box(page, "shop-web"))) && !(await isOn(box(page, "metrics"))));
      ck("pick none: the button says what to do", (await flat(run(page))) === "Pick a connection to export", await flat(run(page)));
      ck("pick none: and can't be pressed", await run(page).isDisabled());
      ck("list: nothing threw", errors.length === 0, errors.join("; "));
      await ctx.close();
    }

    // ── busy ───────────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ slow: 1500 });
      await run(page).click();
      await page.waitForTimeout(300);
      ck("busy: says it is exporting", (await flat(run(page))) === "Exporting", await flat(run(page)));
      ck("busy: can't be pressed twice", await run(page).isDisabled());
      await ctx.close();
    }

    // ── no clipboard ───────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open({ clipboard: "none" });
      await exportNow(page);
      await page.getByTestId("export-copy").click();
      await page.getByTestId("export-copy-failed").waitFor({ timeout: 3000 }).catch(() => {});
      ck("no clipboard: says it could not copy", /Could not copy/.test(await flat(page.getByTestId("export-copy-failed"))));
      ck("no clipboard: the button does not claim it copied", (await flat(page.getByTestId("export-copy"))) === "Copy");
      ck("no clipboard: nothing threw", errors.length === 0, errors.join("; "));
      await ctx.close();
    }

    // ── refused, and failed ────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ exportStatus: 403 });
      await exportNow(page);
      const said = await flat(page.getByTestId("export-error"));
      ck("refused: says the key can't export", /This key can't export/.test(said), said);
      ck("refused: no file shown", (await page.getByTestId("export-result").count()) === 0);
      await words(page, "refused");
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ exportStatus: 500 });
      await exportNow(page);
      const said = await flat(page.getByTestId("export-error"));
      ck("failed: says why", /export failed: disk full/.test(said), said);
      ck("failed: the answer is not shown as the file", (await page.getByTestId("export-result").count()) === 0);
      await ctx.close();
    }

    // ── a read-only key ────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ readOnly: true });
      await page.getByTestId("export-admin-only").waitFor({ timeout: 5000 }).catch(() => {});
      ck("read-only: says the key can't export", /can read but not export/.test(await flat(page.getByTestId("export-admin-only"))));
      await page.getByTestId("export-connections").waitFor({ timeout: 5000 }).catch(() => {});
      ck("read-only: the list still shows", (await page.getByTestId("export-connection").count()) === 3);
      ck("read-only: export can't be pressed", (await run(page).count()) === 1 && (await run(page).isDisabled()));
      await ctx.close();
    }

    // ── nothing to export; the list failing ────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ connections: [] });
      await page.getByTestId("export-empty").waitFor({ timeout: 5000 }).catch(() => {});
      ck("empty: says there is nothing to export", /Nothing to export/.test(await flat(page.getByTestId("export-empty"))));
      const add = page.getByTestId("export-empty").getByRole("link", { name: "Add a connection" });
      ck("empty: offers to add one", (await add.count()) === 1 && (await add.getAttribute("href")) === "/sources/new");
      ck("empty: no export button", (await run(page).count()) === 0);
      await words(page, "empty");
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ connections: "fails" });
      await page.getByTestId("export-load-error").waitFor({ timeout: 5000 }).catch(() => {});
      ck("list fails: says so, with why", /Could not load the connections/.test(await flat(page.getByTestId("export-load-error")))
         && /not reachable/.test(await flat(page.getByTestId("export-load-error"))), await flat(page.getByTestId("export-load-error")));
      ck("list fails: no endless loading", (await page.locator("[data-testid=export-page] [aria-busy=true]").count()) === 0);
      await ctx.close();
    }

    // ── the sre edition's words ────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ edition: "sre" });
      await page.getByTestId("export-connections").waitFor({ timeout: 5000 });
      await page.waitForFunction(() => /check rules/.test(document.querySelector("[data-testid=export-page] > header")?.textContent ?? ""), null, { timeout: 5000 }).catch(() => {});
      ck("sre: says check rules", /with the check rules, agents/.test(await flat(page.locator("[data-testid=export-page] > header"))), await flat(page.locator("[data-testid=export-page] > header")));
      await words(page, "sre");
      await ctx.close();
    }

    // ── dark, and 390 ──────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ dark: true });
      await exportNow(page);
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      ck("dark: the page is dark", /rgb\((\d+), (\d+), (\d+)\)/.test(bg) && Number(bg.match(/\d+/)[0]) < 80, bg);
      await ctx.close();
    }
    for (const dark of [false, true]) {
      const { ctx, page } = await open({ width: 390, dark });
      await exportNow(page);
      ck(`390 ${dark ? "dark" : "light"}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      await ctx.close();
    }
  } catch (e) {
    fail++;
    console.log("  FAIL the suite stopped: " + (e && e.stack ? e.stack.split("\n").slice(0, 3).join(" | ") : e));
  } finally {
    await browser.close();
    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail ? 1 : 0);
  }
})();
