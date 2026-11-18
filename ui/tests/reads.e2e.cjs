/*
 * Browser e2e for Reads (/reads, TR-1395): the read log, newest first. It opens on agents' reads (MCP);
 * the client picker offers agents, every other client the log holds, and every client. Each row says
 * when, which client, what was read, a read or stats, over what window in words, and what it returned.
 * Search matches what was read or the client, in any case. Empty, filtered to nothing, loading, failed
 * and retried each say so. The old /activity?tab=queries address lands here. Light and dark, 1440 and
 * 390. The daemons are real; the read log is mocked (reads.fixture.cjs) except on the real-daemon check.
 * Run:  npm run build && node tests/reads.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockReads } = require("./reads.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9981);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

// The page's own words: what was read is the caller's selector (data-user-text).
const PAGE_WORDS = () => {
  const main = (document.querySelector("main") || document.body).cloneNode(true);
  main.querySelectorAll("[data-words-exempt], [data-user-text]").forEach((n) => n.remove());
  document.body.appendChild(main);
  const t = main.innerText;
  main.remove();
  return t;
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const text = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async (to, { width = 1440, dark = false, wait = "reads-row", mock = true, ...opts } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const calls = mock ? await mockReads(page, opts) : { reads: 0 };
    await page.goto(`${d.base}${to}`);
    await page.getByTestId("reads-page").waitFor({ timeout: 10000 }).catch(() => {});
    if (wait) await page.getByTestId(wait).first().waitFor({ timeout: 10000 }).catch(() => {});
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors, calls };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_WORDS));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const rows = (page) => page.getByTestId("reads-row");
  const col = (page, id) => page.getByTestId(id).allInnerTexts().then((a) => a.map((s) => s.replace(/\s+/g, " ").trim()));
  const pick = async (page, name) => {
    await page.getByTestId("reads-client").click();
    await page.getByRole("option", { name, exact: true }).click();
    await page.waitForTimeout(200);
  };
  const search = async (page, q) => { await page.getByLabel("Search the reads").fill(q); await page.waitForTimeout(200); };

  try {
    // ── landing: agents' reads, newest first ──────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/reads");
      ck("landing: the header", (await text(page.locator("main h1"))) === "Reads");
      ck("landing: says what it lists and how many", (await text(page.getByTestId("reads-page").locator("header p"))).includes("The last 150."));
      ck("landing: opens on agents over MCP", (await text(page.getByTestId("reads-client"))).includes("Agents over MCP"), await text(page.getByTestId("reads-client")));
      ck("landing: only agents' reads", (await rows(page).count()) === 3, String(await rows(page).count()));
      ck("landing: the count of the whole log", (await text(page.getByTestId("reads-count"))) === "3 of 5", await text(page.getByTestId("reads-count")));
      ck("landing: newest first", JSON.stringify(await col(page, "reads-what")) === JSON.stringify(["service=checkout", "repo=glassflow/tares", "team=infra"]), (await col(page, "reads-what")).join(" | "));
      const when = await col(page, "reads-when");
      ck("row: when, in the one time grammar", when[0] === "2 minutes ago" && when[1] === "10 minutes ago", when.join(" | "));
      ck("row: no raw timestamps", !when.some((w) => /\d{4}-\d{2}-\d{2}T/.test(w)), when.join(" | "));
      const stamp = await page.getByTestId("reads-when").first().getAttribute("title");
      ck("row: the UTC stamp on hover", /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z$/.test(stamp || ""), String(stamp));
      ck("row: the client in words", (await col(page, "reads-client-name"))[0] === "Agent (MCP)", (await col(page, "reads-client-name")).join(" | "));
      const meta = await col(page, "reads-meta");
      ck("row: a read, over its window in words", meta[0] === "A read over 1 hour", meta[0]);
      ck("row: a stats call says so", meta[1] === "Stats over 1 day", meta[1]);
      ck("row: a window it cannot read stays as sent", meta[2] === "A read over PT1H", meta[2]);
      const ret = await col(page, "reads-returned");
      ck("row: what it returned, counted", ret[0] === "42 returned" && ret[1] === "1,200 returned", ret.join(" | "));
      await words(page, "landing");
      ck("landing: no page errors", !errors.length, errors.join("; "));
      await ctx.close();
    }

    // ── the client picker ────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/reads");
      await page.getByTestId("reads-client").click();
      const opts = (await page.getByRole("option").allInnerTexts()).map((s) => s.trim());
      ck("client: agents first, then each client in the log, then every client",
        JSON.stringify(opts) === JSON.stringify(["Agents over MCP", "The REST API", "This console", "Every client"]), opts.join(" | "));
      await page.keyboard.press("Escape");
      await pick(page, "Every client");
      ck("client: every client lists the whole log", (await rows(page).count()) === 5 && (await text(page.getByTestId("reads-count"))) === "5 of 5");
      await pick(page, "The REST API");
      ck("client: the REST API alone", (await rows(page).count()) === 1 && (await col(page, "reads-client-name"))[0] === "REST API", (await col(page, "reads-client-name")).join(" | "));
      ck("client: its window in words", (await col(page, "reads-meta"))[0] === "A read over 15 minutes", (await col(page, "reads-meta")).join(" | "));
      await pick(page, "This console");
      ck("client: this console alone", (await rows(page).count()) === 1 && (await col(page, "reads-client-name"))[0] === "This console");
      ck("client: nothing returned says 0", (await col(page, "reads-returned"))[0] === "0 returned");
      await ctx.close();
    }

    // ── search ───────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/reads");
      await pick(page, "Every client");
      await search(page, "checkout");
      ck("search: what was read, any case", JSON.stringify(await col(page, "reads-what")) === JSON.stringify(["service=checkout", "Checkout"]), (await col(page, "reads-what")).join(" | "));
      ck("search: the count follows", (await text(page.getByTestId("reads-count"))) === "2 of 5");
      await search(page, "rest api");
      ck("search: by the client", (await rows(page).count()) === 1 && (await col(page, "reads-what"))[0] === "service=payments", (await col(page, "reads-what")).join(" | "));
      await search(page, "zzz");
      ck("search: nothing matches says so", (await text(page.getByTestId("reads-none"))) === "No reads from any client matching “zzz” in the last 5.", await text(page.getByTestId("reads-none")));
      ck("search: no rows left behind", (await rows(page).count()) === 0);
      await pick(page, "Agents over MCP");
      await search(page, "payments");
      const none = page.getByTestId("reads-none");
      ck("search: agents only, it says which and how to widen", (await text(none)).startsWith("No reads from agents over MCP matching “payments” in the last 5.") && (await text(none)).includes("or pick every client"), await text(none));
      ck("search: and links to Connect", (await none.locator('a[href="/connect"]').count()) === 1);
      await words(page, "nothing matches");
      await ctx.close();
    }

    // ── no agent reads: the default filter is empty, not the log ─────────────────────────────
    {
      const only = [{ id: "r_9", key: "service=payments", window: "1h", rows_returned: 1, client: "http", ago: 60_000 }];
      const { ctx, page } = await open("/reads", { rows: only, wait: "reads-none" });
      ck("no agent reads: says so", (await text(page.getByTestId("reads-none"))).startsWith("No reads from agents over MCP in the last 1."), await text(page.getByTestId("reads-none")));
      ck("no agent reads: does not claim the log is empty", (await page.getByTestId("reads-empty").count()) === 0);
      ck("no agent reads: the picker still offers the client that has reads", (await page.getByTestId("reads-client").count()) === 1);
      await ctx.close();
    }

    // ── empty, loading, failed ───────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/reads", { rows: [], wait: "reads-empty" });
      const e = page.getByTestId("reads-empty");
      ck("empty: says there are no reads yet", (await text(e)).includes("No reads yet"), await text(e));
      ck("empty: offers Connect", (await e.locator('a[href="/connect"]').count()) === 1);
      ck("empty: no picker over nothing", (await page.getByTestId("reads-client").count()) === 0);
      await words(page, "empty");
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/reads", { rows: "slow", wait: null });
      await page.waitForTimeout(600);
      ck("slow: shows it is loading", (await page.locator("main [aria-busy=true]").count()) > 0);
      ck("slow: does not claim there are none", (await page.getByTestId("reads-empty").count()) === 0);
      await ctx.close();
    }
    {
      const { ctx, page, calls } = await open("/reads", { rows: "fail", failUntil: 1, wait: "reads-failed" });
      const b = page.getByTestId("reads-failed");
      ck("failed: says Tares could not list them", (await text(b)).includes("Tares could not list its reads: database is locked"), await text(b));
      ck("failed: does not claim there are none", (await page.getByTestId("reads-empty").count()) === 0);
      const retry = b.getByRole("button", { name: "Try again" });
      ck("failed: offers to try again", (await retry.count()) === 1);
      if (await retry.count()) await retry.click();
      await rows(page).first().waitFor({ timeout: 5000 }).catch(() => {});
      ck("retry: asks again and lists them", calls.reads >= 2 && (await rows(page).count()) === 3, `${calls.reads} calls, ${await rows(page).count()} rows`);
      await ctx.close();
    }

    // ── the real daemon, and the old address ─────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/reads", { mock: false, wait: null });
      await page.waitForFunction(() => document.querySelector("[data-testid=reads-empty], [data-testid=reads-row], [data-testid=reads-none], [data-testid=reads-failed]"), null, { timeout: 10000 }).catch(() => {});
      ck("real daemon: lists or says there are none, without failing", (await page.getByTestId("reads-failed").count()) === 0
        && (await page.locator("[data-testid=reads-empty], [data-testid=reads-row], [data-testid=reads-none]").count()) > 0);
      ck("real daemon: no page errors", !errors.length, errors.join("; "));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/activity?tab=queries");
      ck("old address: /activity?tab=queries lands on Reads", new URL(page.url()).pathname === "/reads" && (await rows(page).count()) === 3, page.url());
      await ctx.close();
    }

    // ── layout ───────────────────────────────────────────────────────────────────────────────
    // The nav link to /reads sits under Advanced; factory-settings.e2e.cjs clicks it through.
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const label = `${width}${dark ? " dark" : ""}`;
      const { ctx, page } = await open("/reads", { width, dark });
      await pick(page, "Every client");
      ck(`layout ${label}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const [a, b] = await Promise.all(["reads-when", "reads-what"].map((id) => page.getByTestId(id).first().boundingBox()));
      const stacked = a && b && b.y > a.y + a.height - 1;
      ck(`layout ${label}: ${width < 600 ? "a row stacks" : "a row reads across"}`, width < 600 ? !!stacked : !stacked, JSON.stringify({ a, b }));
      await ctx.close();
    }
  } catch (e) {
    ck("suite ran without an exception", false, String(e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
