/*
 * Browser e2e for the Connections page on /sources (TR-1394), which Advanced links to: the list on the edition
 * kit, its filters, the empty and failing states, light and dark, 1440 and 390. The list and the project names
 * are mocked at the network so each state is fixed; the rest is the real daemon. The page sits in the legacy
 * shell, whose menu still says Sources, so the words check reads the page's own text.
 * Run:  npm run build && node tests/sources.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots.
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9966);
const SHOTS = process.env.SHOTS_DIR;
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const { LIST, mock } = require("./sources.fixture.cjs");
// The page's own text, minus what a person named or a system returned.
const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=connections-page]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return el ? el.innerText : ""; } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  // list: the rows /api/sources answers, or a status code to fail with. docker: what capabilities say.
  const open = async ({ list = LIST, width = 1440, dark = false, docker = true } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mock(page, { list, docker });
    await page.goto(`${d.base}/sources`, { waitUntil: "load" });
    await page.getByRole("heading", { name: "Connections", exact: true }).waitFor();
    // Routes still in flight when the page closes would throw after the suite moved on.
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const rows = (page) => page.getByTestId("connection-row");
  const count = (page) => page.getByTestId("connections-count").innerText();
  const pick = async (page, testid, option) => {
    await page.getByTestId(testid).click();
    await page.getByRole("option", { name: option, exact: true }).click();
  };

  try {
    // The list, light, 1440.
    {
      const { ctx, page } = await open();
      await rows(page).first().waitFor();
      ck("list: one row per connection", await rows(page).count() === 4, String(await rows(page).count()));
      ck("list: the count says 4 of 4", (await count(page)) === "4 of 4", await count(page));
      const gh = rows(page).filter({ hasText: "acme/shop" });
      const ghText = (await gh.innerText()).replace(/\s+/g, " ");
      ck("row: the title from its settings, then its name", ghText.includes("acme/shop") && ghText.includes("gh_shop"), ghText);
      ck("row: kind, how often and its project", /GitHub.*every 60s.*in Shop/i.test(ghText), ghText);
      ck("row: healthy and the count of events", /healthy/i.test(ghText) && ghText.includes("12,840 events") && /last \d+ minutes? ago/.test(ghText), ghText);
      const push = (await rows(page).filter({ hasText: "claude_code" }).innerText()).replace(/\s+/g, " ");
      ck("row: a push connection says it receives pushes, no interval, no project", /receives pushes/i.test(push) && !push.includes("every") && push.includes("in no project"), push);
      const pg = rows(page).filter({ hasText: "orders" });
      ck("row: a failing connection shows its error", (await pg.getByTestId("connection-error").innerText()).includes("password authentication failed"));
      ck("row: and names both its projects", (await pg.innerText()).includes("in Shop, Billing"), await pg.innerText());
      ck("head: Add a connection goes to /sources/new", await page.getByRole("link", { name: "Add a connection" }).getAttribute("href") === "/sources/new");
      const hrefs = await page.locator("[data-testid=connections-page] header a").evaluateAll((as) => as.map((a) => a.getAttribute("href")));
      ck("head: Find in Docker, Import and Export", ["/sources/discover", "/sources/import", "/sources/export"].every((h) => hrefs.includes(h)), hrefs.join(","));
      await words(page, "list");
      await shot(page, "list-light-1440.png");

      // Filters.
      // acme/shop is only in the title: not the name (gh_shop), not the kind (GitHub).
      await page.getByTestId("connections-search").fill("ACME/SHOP");
      ck("search: matches the title, any case", (await count(page)) === "1 of 4" && (await rows(page).innerText()).includes("gh_shop"), await count(page));
      await page.getByTestId("connections-search").fill("loki");
      ck("search: matches the kind", (await count(page)) === "1 of 4", await count(page));
      await page.getByTestId("connections-search").fill("");
      await pick(page, "connections-status", "failing");
      ck("status: failing keeps the one with an error", (await count(page)) === "1 of 4" && (await rows(page).innerText()).includes("pg_orders"), await count(page));
      await pick(page, "connections-status", "Any status");
      await pick(page, "connections-kind", "GitHub");
      ck("kind: GitHub keeps the GitHub one", (await count(page)) === "1 of 4" && (await rows(page).innerText()).includes("gh_shop"), await count(page));
      await pick(page, "connections-kind", "Any kind");
      await pick(page, "connections-project", "Billing");
      ck("project: Billing keeps both of its connections", (await count(page)) === "2 of 4", await count(page));
      await pick(page, "connections-project", "In no project");
      ck("project: In no project keeps the push one", (await count(page)) === "1 of 4" && (await rows(page).innerText()).includes("claude_code"), await count(page));
      await page.getByTestId("connections-search").fill("nothing like this");
      ck("filters: no match says so", await page.getByTestId("connections-none").isVisible() && (await count(page)) === "0 of 4");
      await words(page, "no match");
      await page.getByTestId("connections-search").fill("");
      await pick(page, "connections-project", "Any project");

      await rows(page).filter({ hasText: "acme/shop" }).click();
      await page.waitForURL("**/sources/gh_shop");
      ck("row: opens the connection's page", new URL(page.url()).pathname === "/sources/gh_shop", page.url());
      await ctx.close();
    }

    // Dark, 1440.
    {
      const { ctx, page } = await open({ dark: true });
      await rows(page).first().waitFor();
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      ck("dark: the page is dark", /rgb\((\d+), (\d+), (\d+)\)/.test(bg) && bg.match(/\d+/g).slice(0, 3).every((n) => Number(n) < 80), bg);
      await shot(page, "list-dark-1440.png");
      await ctx.close();
    }

    // 390, light and dark.
    for (const dark of [false, true]) {
      const { ctx, page } = await open({ width: 390, dark });
      await rows(page).first().waitFor();
      ck(`390 ${dark ? "dark" : "light"}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const box = await page.getByTestId("connection-error").boundingBox();
      ck(`390 ${dark ? "dark" : "light"}: the error stays inside the screen`, box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
      await shot(page, `list-${dark ? "dark" : "light"}-390.png`);
      await ctx.close();
    }

    // Empty, with and without Docker.
    {
      const { ctx, page } = await open({ list: [] });
      await page.getByTestId("connections-empty").waitFor();
      const t = await page.getByTestId("connections-empty").innerText();
      ck("empty: says so, and Add a connection is the one way on", t.includes("No connections yet") && await page.getByRole("link", { name: "Add a connection" }).count() === 1, t);
      ck("empty: mentions Docker when it is reachable", t.includes("Docker"), t);
      ck("empty: no filters, no list", await page.getByTestId("connections-search").count() === 0 && await rows(page).count() === 0);
      await words(page, "empty");
      await shot(page, "empty-light-1440.png");
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ list: [], docker: false });
      await page.getByTestId("connections-empty").waitFor();
      // Shown until capabilities say false, so wait for the answer.
      await page.getByRole("link", { name: "Find in Docker" }).waitFor({ state: "detached" }).catch(() => {});
      ck("no Docker: no Find in Docker, and the empty state does not offer it",
         await page.getByRole("link", { name: "Find in Docker" }).count() === 0 && !(await page.getByTestId("connections-empty").innerText()).includes("Docker"));
      await ctx.close();
    }

    // Tares failing to answer.
    {
      const { ctx, page } = await open({ list: 500 });
      const alert = page.getByRole("alert").filter({ hasText: "not answering" });
      await alert.waitFor();
      ck("error: an alert says Tares is not answering", (await alert.innerText()).includes("Tares is not answering"), await alert.innerText());
      await words(page, "error");
      await shot(page, "error-light-1440.png");
      await ctx.close();
    }
  } catch (err) {
    ck("suite ran to the end", false, err.stack || String(err));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
