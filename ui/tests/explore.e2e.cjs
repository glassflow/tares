/*
 * Browser e2e for Explore (/explore, TR-1395): the picker groups what the connections label their
 * events by, primary labels first; the page lands on the richest value and reads its timeline from
 * every connection; picking, searching, adding and removing a filter, the project and the window each
 * change what is read; the timeline says when in the one time grammar; "What the agent gets" shows the
 * payload with a copy button; nothing, a failed read, a failed load and a slow load each say so.
 * Light and dark, 1440 and 390. The daemon is real; /api/entities and /read are mocked
 * (explore.fixture.cjs), except one check that reads the real, empty daemon.
 * Run:  npm run build && node tests/explore.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockExplore } = require("./explore.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9986);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const MAIN_TEXT = () => (document.querySelector("main") || document.body).innerText;
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;
const J = JSON.stringify;

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async (to, { width = 1440, dark = false, wait = true, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}),
      permissions: ["clipboard-read", "clipboard-write"] });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const calls = await mockExplore(page, mock);
    await page.goto(`${d.base}${to}`);
    if (wait) await page.getByTestId("explore-row").first().waitFor({ timeout: 10000 }).catch(() => {});
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors, calls };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(MAIN_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const rows = (page) => page.getByTestId("explore-row");
  const value = (page, v) => page.getByTestId("explore-value").filter({ hasText: v });
  const lastRead = (calls) => calls.reads[calls.reads.length - 1] ?? {};
  const settle = (page) => page.waitForTimeout(300);
  const choose = async (page, testid, option) => {
    await page.getByTestId(testid).click();
    await page.getByRole("option", { name: option, exact: true }).click();
    await settle(page);
  };

  try {
    // ── landing ───────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors, calls } = await open("/explore");
      ck("page: titled Explore", (await page.getByRole("heading", { level: 1 }).innerText()) === "Explore");
      ck("page: says what it is for in page words", (await page.evaluate(MAIN_TEXT)).includes("Everything about it, from every connection, on one timeline."));
      await words(page, "landing");
      ck("landing: opens on the value with the most events", (await flat(page.getByTestId("explore-title"))) === "acme/shop");
      ck("landing: says its label, events and connections",
         (await flat(page.getByTestId("explore-meta"))) === "key, 1,234 events, from 2 connections: gh-shop, pg-logs", await flat(page.getByTestId("explore-meta")));
      ck("landing: reads it from every connection over the last hour",
         J(lastRead(calls)) === J({ selector: { key_value: "acme/shop" }, window: "1h", client: "ui" }), J(lastRead(calls)));
      ck("landing: the picked value is pressed", (await value(page, "acme/shop").getAttribute("aria-pressed")) === "true");
      ck("landing: one term, shown as label=value", J(await page.getByTestId("explore-term").allInnerTexts()) === J(["key=acme/shop"]));

      // ── the picker ─────────────────────────────────────────────────────────────────────────────
      const aside = page.getByRole("complementary", { name: "What to explore" });
      const asideText = (await aside.innerText()).replace(/\s+/g, " ");
      ck("picker: a label with no values is left out", (await page.getByTestId("explore-group").count()) === 3 && !asideText.includes("region"), asideText);
      ck("picker: primary labels first, then the rest",
         asideText.indexOf("READ AND ALERT BY") < asideText.indexOf("key") && asideText.indexOf("service") < asideText.indexOf("OTHER WAYS TO SLICE")
         && asideText.indexOf("OTHER WAYS TO SLICE") < asideText.indexOf("user"), asideText);
      ck("picker: counts each value's events", (await flat(value(page, "acme/shop"))) === "acme/shop 1,234");
      ck("picker: a sampled label says so", (await page.getByTestId("explore-group").filter({ hasText: "user" }).innerText()).includes("sample"));

      // ── the timeline ───────────────────────────────────────────────────────────────────────────
      ck("timeline: one row per event", (await rows(page).count()) === 2);
      ck("timeline: counts them", (await flat(page.getByTestId("explore-count"))) === "2 events");
      ck("timeline: when, in the one time grammar", J(await page.getByTestId("explore-when").allInnerTexts()) === J(["3 minutes ago", "2 hours ago"]),
         J(await page.getByTestId("explore-when").allInnerTexts()));
      ck("timeline: which connection each came from", J(await page.getByTestId("explore-from").allInnerTexts()) === J(["gh-shop", "pg-logs"]));
      ck("timeline: keeps an event's own line breaks", (await rows(page).nth(1).innerText()).includes("deadlock detected\n  at checkout"));
      ck("timeline: each event's labels", (await flat(rows(page).nth(0))).includes("key=acme/shop user=kim"), await flat(rows(page).nth(0)));
      ck("timeline: no raw offsets", !(await page.evaluate(MAIN_TEXT)).includes("T-180s"));

      // ── pick another ───────────────────────────────────────────────────────────────────────────
      await value(page, "checkout").click(); await settle(page);
      ck("pick: the title follows", (await flat(page.getByTestId("explore-title"))) === "checkout");
      ck("pick: reads by that label", J(lastRead(calls).selector) === J({ service: "checkout" }), J(lastRead(calls)));
      ck("pick: the press moves", (await value(page, "checkout").getAttribute("aria-pressed")) === "true"
         && (await value(page, "acme/shop").getAttribute("aria-pressed")) === "false");

      // ── add a filter, then remove it ───────────────────────────────────────────────────────────
      await choose(page, "explore-add", "user (1)");
      ck("filter: asks for a value of the label", (await page.getByText("Filter by user", { exact: true }).count()) === 1);
      await choose(page, "explore-add-value", "kim (50)");
      ck("filter: shown as a second term", J(await page.getByTestId("explore-term").allInnerTexts()) === J(["service=checkout", "user=kim"]));
      ck("filter: both must hold", J(lastRead(calls).selector) === J({ service: "checkout", user: "kim" }), J(lastRead(calls)));
      ck("filter: the timeline narrows", (await rows(page).count()) === 1);
      ck("filter: a used label is not offered again", await (async () => {
        await page.getByTestId("explore-add").click();
        const opts = await page.getByRole("option").allInnerTexts();
        await page.keyboard.press("Escape");
        return !opts.some((o) => o.startsWith("user") || o.startsWith("service"));
      })());
      await page.getByRole("button", { name: "Remove user=kim" }).click(); await settle(page);
      ck("remove: the term goes and the read widens", J(lastRead(calls).selector) === J({ service: "checkout" }) && (await rows(page).count()) === 2);
      await choose(page, "explore-add", "user (1)");
      await page.getByRole("button", { name: "Cancel" }).click();
      ck("cancel: back to adding a filter", (await page.getByTestId("explore-add").count()) === 1 && (await page.getByTestId("explore-add-value").count()) === 0);

      // ── search ─────────────────────────────────────────────────────────────────────────────────
      await page.getByLabel("Search", { exact: true }).fill("pay");
      ck("search: by value", J(await page.getByTestId("explore-value").allInnerTexts().then((t) => t.map((s) => s.split("\n")[0]))) === J(["payments"]));
      await page.getByLabel("Search", { exact: true }).fill("user");
      ck("search: by label shows all its values", (await page.getByTestId("explore-value").count()) === 1 && (await value(page, "kim").count()) === 1);
      await page.getByLabel("Search", { exact: true }).fill("zzz");
      ck("search: nothing matches says so", (await flat(page.getByTestId("explore-nomatch"))) === "Nothing matches “zzz”.");
      await page.getByLabel("Search", { exact: true }).fill("");

      // ── window and project ─────────────────────────────────────────────────────────────────────
      await choose(page, "explore-window", "Last 24 hours");
      ck("window: reads that window", lastRead(calls).window === "24h", J(lastRead(calls)));
      await choose(page, "explore-window", "Last 30 days");
      ck("window: nothing in it says so, and how to widen",
         (await flat(page.getByTestId("explore-nothing"))) === "Nothing matches this in the last 30 days. Try a longer window, or remove a filter.",
         await flat(page.getByTestId("explore-nothing")));
      await choose(page, "explore-window", "Last hour");
      await choose(page, "explore-scope", "Project Shop");
      ck("project: reads that project's connections", lastRead(calls).project === "shop", J(lastRead(calls)));
      ck("project: nothing there says to read every connection",
         (await flat(page.getByTestId("explore-nothing"))).startsWith("This project's connections hold nothing for this."));
      await words(page, "project empty");
      await choose(page, "explore-scope", "Every connection");
      ck("every connection: no project is sent", !("project" in lastRead(calls)) && (await rows(page).count()) === 2, J(lastRead(calls)));

      // ── what the agent gets ────────────────────────────────────────────────────────────────────
      await page.getByRole("tab", { name: "What the agent gets" }).click(); await settle(page);
      const agent = page.getByTestId("explore-agent");
      ck("agent: says what it is, for the window", (await agent.innerText()).includes("Exactly what an agent receives over MCP for the last hour"));
      ck("agent: the payload as the agent reads it", (await flat(page.getByTestId("explore-payload"))).startsWith("# service=checkout, last 1h"),
         await flat(page.getByTestId("explore-payload")));
      ck("agent: no timeline rows behind it", (await rows(page).count()) === 0);
      await agent.getByRole("button", { name: "Copy" }).click();
      const copiedSeen = await agent.getByRole("button", { name: "Copied" }).waitFor({ timeout: 1000 }).then(() => true, () => false);
      ck("agent: copy says it copied", copiedSeen);
      ck("agent: the clipboard holds the payload", (await page.evaluate(() => navigator.clipboard.readText())).startsWith("# service=checkout"));
      await words(page, "agent");
      await value(page, "acme/shop").click(); await settle(page);
      ck("agent: picking returns to the timeline", (await page.getByRole("tab", { name: "Timeline" }).getAttribute("aria-selected")) === "true" && (await rows(page).count()) === 2);

      await ctx.close();
      ck("page: no page errors", !errors.length, errors.join(" | "));
    }

    // ── failed read ───────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/explore", { readFail: true, wait: false });
      const alert = page.getByTestId("explore-read-error");
      await alert.waitFor({ timeout: 10000 }).catch(() => {});
      ck("read failed: says Tares could not read it", (await flat(alert)).startsWith("Tares could not read this"), await flat(alert));
      ck("read failed: no rows, no endless loading", (await rows(page).count()) === 0 && (await page.locator("[data-testid=explore-detail] [aria-busy=true]").count()) === 0);
      await ctx.close();
    }
    // ── failed load, retried ──────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, calls } = await open("/explore", { fail: { until: 1 }, wait: false });
      const alert = page.getByRole("alert");
      await alert.first().waitFor({ timeout: 10000 }).catch(() => {});
      ck("failed: says Tares is not answering", (await flat(alert)).startsWith("Tares is not answering"), await flat(alert));
      ck("failed: does not claim there is nothing", (await page.getByTestId("explore-empty").count()) === 0);
      const retry = page.getByRole("button", { name: "Try again" });
      ck("failed: offers to try again", (await retry.count()) === 1);
      if (await retry.count()) await retry.click();
      await rows(page).first().waitFor({ timeout: 5000 }).catch(() => {});
      ck("retry: asks again and shows the timeline", calls.entities >= 2 && (await rows(page).count()) === 2, `${calls.entities} calls`);
      await ctx.close();
    }
    // ── loading ───────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/explore", { slow: 2500, wait: false });
      await page.waitForTimeout(800);
      ck("slow: shows it is loading", (await page.locator("main [aria-busy=true]").count()) > 0);
      ck("slow: does not claim there is nothing", (await page.getByTestId("explore-empty").count()) === 0);
      await ctx.close();
    }
    // ── nothing yet: the real daemon, and a mocked empty one ──────────────────────────────────────
    for (const [label, mock] of [["real daemon", { real: true }], ["empty", { empty: true }]]) {
      const { ctx, page, errors } = await open("/explore", { ...mock, wait: false });
      const empty = page.getByTestId("explore-empty");
      await empty.waitFor({ timeout: 10000 }).catch(() => {});
      ck(`${label}: titled nothing to explore yet`, (await page.getByRole("heading", { name: "Nothing to explore yet" }).count()) === 1, (await page.evaluate(MAIN_TEXT)).slice(0, 200));
      const toSources = empty.getByRole("link", { name: "See the connections" });
      ck(`${label}: offers the connections`, (await toSources.count()) === 1 && (await toSources.getAttribute("href")) === "/sources");
      await words(page, label);
      await ctx.close();
      ck(`${label}: no page errors`, !errors.length, errors.join(" | "));
    }
    // ── the old address ───────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/entities");
      ck("old /entities: opens Explore", new URL(page.url()).pathname === "/explore" && (await rows(page).count()) === 2);
      await ctx.close();
    }

    // ── widths and themes ─────────────────────────────────────────────────────────────────────────
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const tag = `${width} ${dark ? "dark" : "light"}`;
      const { ctx, page } = await open("/explore", { width, dark });
      ck(`${tag}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const pick = await page.getByRole("complementary", { name: "What to explore" }).boundingBox();
      const tl = await page.getByTestId("explore-detail").boundingBox();
      ck(`${tag}: ${width < 768 ? "the picker sits above the timeline" : "the picker sits beside the timeline"}`,
         !!pick && !!tl && (width < 768 ? pick.y + pick.height <= tl.y + 1 : pick.x + pick.width <= tl.x), J({ pick, tl }));
      const box = await rows(page).nth(1).boundingBox();
      ck(`${tag}: a row fits the width`, !!box && box.x >= 0 && box.x + box.width <= width + 0.5, J(box));
      await page.getByRole("tab", { name: "What the agent gets" }).click(); await settle(page);
      ck(`${tag}: the payload, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      await ctx.close();
    }
  } catch (e) {
    fail++;
    console.log("  FAIL crashed: " + (e && e.stack || e));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
