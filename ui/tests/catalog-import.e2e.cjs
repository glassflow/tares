/*
 * Browser e2e for Import connections on /sources/import (TR-1394): the page and its words, a merge
 * through the real daemon and what it says after, a file the daemon refuses, replace and its
 * confirmation (kept, escaped, confirmed), a refused (403) and a failed import, a read-only key, busy,
 * an empty file, the sre words, light and dark, 1440 and 390. The daemon is real; the import is
 * mocked only for 403, 500 and slow answers (catalog-import.fixture.cjs).
 * Run:  npm run build && node tests/catalog-import.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockImport, GOOD, BAD } = require("./catalog-import.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9980);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=import-page]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return el ? el.innerText : ""; } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;
const SOLO = "sources:\n  - name: solo\n    connector: docker_logs\n    poll: 30s\n    config: {container: solo-1}\n";

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const names = async () => (await (await fetch(`${d.base}/api/sources`)).json()).map((s) => s.name).sort().join(",");

  const open = async ({ width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await mockImport(page, mock);
    await page.goto(`${d.base}/sources/import`, { waitUntil: "load" });
    await page.getByTestId("import-page").waitFor({ timeout: 15000 });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const run = (page) => page.getByTestId("import-run");
  const area = (page) => page.getByTestId("import-yaml");
  const settle = (page) => Promise.race([
    page.getByTestId("import-done").waitFor({ timeout: 5000 }),
    page.getByTestId("import-error").waitFor({ timeout: 5000 }),
  ]).catch(() => {});
  const replaceOn = async (page) => { await page.getByTestId("import-replace").click(); };

  try {
    // ── the page, before anything is pasted ───────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open();
      const header = page.locator("[data-testid=import-page] > header");
      ck("page: titled in page words", (await page.getByRole("heading", { level: 1 }).innerText()) === "Import connections");
      ck("page: says what a file holds", /connections, rules, agents and MCP servers/.test(await flat(header)), await flat(header));
      ck("page: the file box is labelled", (await page.getByLabel("The file").count()) === 1);
      ck("page: the file box is system text", (await area(page).getAttribute("data-words-exempt")) !== null);
      ck("page: nothing pasted can't be pressed", await run(page).isDisabled());
      ck("page: and says why", (await run(page).innerText()) === "Paste a file to import", await run(page).innerText());
      ck("page: replace is off to start", (await page.getByTestId("import-replace").getAttribute("aria-checked")) === "false");
      ck("page: says what off keeps", /adds what is new and updates what has the same name\. The rest stays/.test(await flat(page.locator("[data-testid=import-page] section"))));
      ck("page: no read-only note for a key that can write", (await page.getByTestId("import-admin-only").count()) === 0);
      ck("page: offers export instead", (await page.getByRole("link", { name: "Export a file instead" }).getAttribute("href")) === "/sources/export");
      await area(page).fill("   \n  ");
      ck("page: blank lines can't be pressed", await run(page).isDisabled());
      await words(page, "page");
      ck("page: no page errors", errors.length === 0, errors.join(" | "));
      await ctx.close();
    }

    // ── merge through the daemon ───────────────────────────────────────────────────────────────────
    {
      const calls = [];
      const { ctx, page, errors } = await open({ calls });
      await area(page).fill(GOOD);
      ck("merge: the button names the step", (await run(page).innerText()) === "Import the file", await run(page).innerText());
      await run(page).click();
      await settle(page);
      ck("merge: no confirmation asked", (await page.getByTestId("import-confirm").count()) === 0);
      ck("merge: one call", calls.length === 1, JSON.stringify(calls));
      ck("merge: asks to merge", calls[0]?.mode === "merge", JSON.stringify(calls[0]));
      ck("merge: sends the file as pasted", calls[0]?.yaml === GOOD);
      ck("merge: says what came in", (await flat(page.getByTestId("import-done"))) === "Imported 2 connections, 1 rule, 0 agents and 0 MCP servers. See the connections",
         await flat(page.getByTestId("import-done")));
      ck("merge: links to the connections", (await page.getByRole("link", { name: "See the connections" }).getAttribute("href")) === "/sources");
      ck("merge: the box is emptied", (await area(page).inputValue()) === "");
      ck("merge: the daemon has them", (await names()) === "shop-db,shop-web", await names());
      await words(page, "merge");
      await area(page).fill("s");
      ck("merge: typing again clears what it said", (await page.getByTestId("import-done").count()) === 0);
      ck("merge: no page errors", errors.length === 0, errors.join(" | "));
      await ctx.close();
    }

    // ── a file the daemon refuses ──────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open();
      await area(page).fill(BAD);
      await run(page).click();
      await settle(page);
      const e = page.getByTestId("import-error");
      ck("bad file: says it could not import", /^Could not import the file:/.test(await flat(e)), await flat(e));
      ck("bad file: with the daemon's reason", /unknown connector 'no_such_kind'/.test(await flat(e)), await flat(e));
      ck("bad file: the reason is user text", (await e.locator("[data-user-text]").count()) === 1);
      ck("bad file: the file stays to fix", (await area(page).inputValue()) === BAD);
      ck("bad file: nothing claimed", (await page.getByTestId("import-done").count()) === 0);
      ck("bad file: merge wrote nothing", (await names()) === "shop-db,shop-web", await names());
      await words(page, "bad file");
      await ctx.close();
    }

    // ── replace: asks first ────────────────────────────────────────────────────────────────────────
    {
      const calls = [];
      const { ctx, page, errors } = await open({ calls });
      await area(page).fill(SOLO);
      await replaceOn(page);
      const section = await flat(page.locator("[data-testid=import-page] section"));
      ck("replace: says what goes", /Removes every connection, rule, agent and MCP server here, and each project's setup/.test(section), section);
      ck("replace: the button says replace", (await run(page).innerText()) === "Replace with the file", await run(page).innerText());
      await run(page).click();
      const dlg = page.getByTestId("import-confirm");
      await dlg.waitFor({ timeout: 3000 }).catch(() => {});
      ck("replace: asks first", (await dlg.count()) === 1);
      ck("replace: the question", /Replace everything\?/.test(await flat(dlg)), await flat(dlg));
      ck("replace: says it can't be undone", /can't be undone/.test(await flat(dlg)));
      ck("replace: nothing sent while asking", calls.length === 0, JSON.stringify(calls));
      await words(page, "replace dialog");
      await page.getByTestId("import-confirm-cancel").click();
      await dlg.waitFor({ state: "detached", timeout: 3000 }).catch(() => {});
      ck("replace: keep them closes it", (await dlg.count()) === 0);
      ck("replace: keep them sends nothing", calls.length === 0);
      await run(page).click();
      await dlg.waitFor({ timeout: 3000 }).catch(() => {});
      await page.keyboard.press("Escape");
      await dlg.waitFor({ state: "detached", timeout: 3000 }).catch(() => {});
      ck("replace: escape sends nothing", calls.length === 0 && (await dlg.count()) === 0);
      ck("replace: the daemon still has them", (await names()) === "shop-db,shop-web", await names());
      await run(page).click();
      await page.getByTestId("import-confirm-replace").click();
      await settle(page);
      ck("replace: confirmed sends one call", calls.length === 1, JSON.stringify(calls));
      ck("replace: asks to replace", calls[0]?.mode === "replace");
      ck("replace: says what it did", (await flat(page.getByTestId("import-done"))) === "Replaced everything with 1 connection, 0 rules, 0 agents and 0 MCP servers. See the connections",
         await flat(page.getByTestId("import-done")));
      ck("replace: the daemon has only the file's", (await names()) === "solo", await names());
      ck("replace: no page errors", errors.length === 0, errors.join(" | "));
      await ctx.close();
    }

    // ── refused, failed ────────────────────────────────────────────────────────────────────────────
    for (const [status, expect, label] of [[403, /^Could not import the file: This key can't import\. Importing needs a key that can write\.$/, "refused"],
                                           [500, /import failed: disk full/, "failed"]]) {
      const { ctx, page } = await open({ importStatus: status });
      await area(page).fill(GOOD);
      await run(page).click();
      await settle(page);
      ck(`${label}: says so`, expect.test(await flat(page.getByTestId("import-error"))), await flat(page.getByTestId("import-error")));
      ck(`${label}: nothing claimed`, (await page.getByTestId("import-done").count()) === 0);
      ck(`${label}: the file stays`, (await area(page).inputValue()) === GOOD);
      if (status === 403) ck("refused: no daemon words", !/scope|credential/.test(await flat(page.getByTestId("import-page"))));
      await ctx.close();
    }

    // ── a read-only key ────────────────────────────────────────────────────────────────────────────
    {
      const calls = [];
      const { ctx, page } = await open({ readOnly: true, calls });
      await page.getByTestId("import-admin-only").waitFor({ timeout: 5000 }).catch(() => {});
      ck("read-only: says the key can't import", /can read but not import/.test(await flat(page.getByTestId("import-admin-only"))));
      await area(page).fill(GOOD);
      ck("read-only: import can't be pressed", await run(page).isDisabled());
      ck("read-only: nothing sent", calls.length === 0);
      await words(page, "read-only");
      await ctx.close();
    }

    // ── busy ───────────────────────────────────────────────────────────────────────────────────────
    {
      const calls = [];
      const { ctx, page } = await open({ calls, slow: 1500, importStatus: 500 });
      await area(page).fill(GOOD);
      await run(page).click();
      await page.waitForTimeout(150);
      ck("busy: says it is importing", (await run(page).innerText()) === "Importing", await run(page).innerText());
      ck("busy: can't be pressed twice", await run(page).isDisabled());
      await run(page).click({ force: true }).catch(() => {});
      await settle(page);
      ck("busy: one call", calls.length === 1, String(calls.length));
      await ctx.close();
    }

    // ── an empty file ──────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open();
      await area(page).fill("sources: []\n");
      await run(page).click();
      await settle(page);
      ck("empty: says there was nothing", (await flat(page.getByTestId("import-done"))) === "The file had nothing to import. See the connections",
         await flat(page.getByTestId("import-done")));
      await ctx.close();
    }

    // ── sre words ──────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ edition: "sre" });
      const header = page.locator("[data-testid=import-page] > header");
      await page.waitForFunction(() => /check rules/.test(document.querySelector("[data-testid=import-page] > header")?.innerText || ""), null, { timeout: 5000 }).catch(() => {});
      ck("sre: says check rules", /connections, check rules, agents/.test(await flat(header)), await flat(header));
      await area(page).fill(GOOD);
      await run(page).click();
      await settle(page);
      ck("sre: counts check rules", /1 check rule,/.test(await flat(page.getByTestId("import-done"))), await flat(page.getByTestId("import-done")));
      await replaceOn(page);
      ck("sre: replace names check rules", /every connection, check rule, agent/.test(await flat(page.locator("[data-testid=import-page] section"))));
      await words(page, "sre");
      await ctx.close();
    }

    // ── dark, and 390 ──────────────────────────────────────────────────────────────────────────────
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const label = `${width}${dark ? " dark" : ""}`;
      const { ctx, page, errors } = await open({ width, dark });
      await area(page).fill(GOOD);
      await replaceOn(page);
      ck(`${label}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      if (dark) ck(`${label}: dark background`, (() => { const m = bg.match(/\d+/g).map(Number); return m[0] + m[1] + m[2] < 200; })(), bg);
      await run(page).click();
      await page.getByTestId("import-confirm").waitFor({ timeout: 3000 }).catch(() => {});
      const box = await page.getByTestId("import-confirm").boundingBox();
      ck(`${label}: the dialog fits`, !!box && box.x >= 0 && box.x + box.width <= width, JSON.stringify(box));
      await page.getByTestId("import-confirm-cancel").click();
      await words(page, label);
      ck(`${label}: no page errors`, errors.length === 0, errors.join(" | "));
      await ctx.close();
    }
  } catch (e) {
    fail++; console.log("  FAIL crashed: " + (e && e.stack || e));
  }
  await browser.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
