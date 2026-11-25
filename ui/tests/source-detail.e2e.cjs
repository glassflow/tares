/*
 * Browser e2e for a connection's page on /sources/<name> (TR-1394): the head, the error, the labels, the Fields,
 * Recent events and Configuration tabs, pause, delete, a push connection, a missing one, light and dark, 1440
 * and 390. The connection is mocked at the network (sources.fixture.cjs mockDetail) so each state is fixed;
 * the rest is the real daemon. The words check reads the page's own text, not the legacy shell around it.
 * Run:  npm run build && node tests/source-detail.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots.
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { ORDERS, ALERTS, mockDetail } = require("./sources.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9968);
const SHOTS = process.env.SHOTS_DIR;
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

// The page's own text, minus what a person named or a system returned.
const PAGE_TEXT = () => {
  const els = [document.querySelector("[data-testid=connection-page]"), document.querySelector("[role=dialog]")].filter(Boolean);
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return els.map((el) => el.innerText).join("\n"); } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async ({ width = 1440, dark = false, ...detail } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const calls = [];
    const source = detail.source ?? ORDERS;
    await mockDetail(page, { ...detail, calls });
    await page.goto(`${d.base}/sources/${source.name}`, { waitUntil: "load" });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, calls };
  };
  const ready = (page) => page.getByTestId("connection-title").waitFor();
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const fieldRows = (page, section) => page.getByTestId(section).getByTestId("connection-field");

  try {
    // A failing connection, light, 1440.
    {
      const { ctx, page } = await open();
      await ready(page);
      ck("head: the title from its settings", (await page.getByTestId("connection-title").innerText()) === "orders");
      ck("head: its internal name under the title", (await page.getByTestId("connection-page").innerText()).includes("pg_orders"));
      // Project names resolve after the page draws; wait for them, not a fixed time.
      await page.getByTestId("connection-kind").getByRole("link", { name: "Billing", exact: true }).waitFor();
      const kind = await flat(page.getByTestId("connection-kind"));
      ck("head: kind, how often and its projects", /^Postgres table, checked every 5m, in Shop, Billing$/.test(kind), kind);
      ck("head: each project links to its page",
         (await page.getByTestId("connection-kind").getByRole("link", { name: "Billing" }).getAttribute("href")) === "/projects/p-billing");
      ck("head: the state in words", /^failing$/i.test(await page.getByTestId("connection-state").innerText()), await page.getByTestId("connection-state").innerText());
      ck("head: when it last received something", (await page.getByTestId("connection-last").innerText()) === "last received 10 hours ago",
         await page.getByTestId("connection-last").innerText());
      const err = await flat(page.getByTestId("connection-error"));
      ck("error: how many times in a row, then the error", err === 'Failing 3 times in a row: password authentication failed for user "reader"', err);
      const ev = await flat(page.getByTestId("connection-events"));
      ck("events kept: the total, and since the restart from how many checks", /^events kept 4 4 since Tares restarted, from 120 checks$/i.test(ev), ev);
      ck("not a push connection: no endpoint", await page.getByText("endpoint to post to").count() === 0);

      // The labels summary, shut and open.
      const sum = await page.getByTestId("connection-labels-summary").innerText();
      ck("labels: the count and the key", sum === "3 labels, the key is table", sum);
      await page.getByTestId("connection-labels-toggle").click();
      const lab = await flat(page.getByTestId("connection-labels"));
      ck("labels open: where each comes from", /table key from table/i.test(lab) && lab.includes("env always prod") && lab.includes("status from row.status"), lab);
      ck("labels open: the toggle says Hide them", (await page.getByTestId("connection-labels-toggle").innerText()) === "Hide them");

      // Fields, the tab open by default.
      await fieldRows(page, "connection-field-labels").first().waitFor();
      ck("fields: one row per label", await fieldRows(page, "connection-field-labels").count() === 3);
      const status = await flat(fieldRows(page, "connection-field-labels").filter({ hasText: "status" }));
      ck("fields: a label that rewrites its values says how", /rewrites: regex \+1/i.test(status), status);
      ck("fields: a label no event carries says so", status.includes("No sampled event carries this label"), status);
      const table = await flat(fieldRows(page, "connection-field-labels").filter({ hasText: "table" }));
      ck("fields: coverage shown when it varies", table.includes("in 50 of 50, 1 value") && table.includes("orders (50)"), table);
      ck("fields: a coverage bar per label", await page.getByTestId("connection-field-labels").getByRole("progressbar").count() === 3);
      ck("other fields: the twelve densest first", await fieldRows(page, "connection-fields").count() === 12);
      ck("other fields: the key is not repeated", await fieldRows(page, "connection-fields").filter({ hasText: /^table/ }).count() === 0);
      ck("other fields: densest first", (await fieldRows(page, "connection-fields").first().innerText()).startsWith("row.id"));
      await page.getByTestId("connection-fields-more").click();
      ck("other fields: Show all shows all fourteen", await fieldRows(page, "connection-fields").count() === 14);
      ck("other fields: the toggle says Show fewer", (await page.getByTestId("connection-fields-more").innerText()) === "Show fewer");
      await words(page, "fields");
      await shot(page, "fields-light-1440.png");

      // Recent events.
      await page.getByRole("tab", { name: "Recent events" }).click();
      await page.getByTestId("connection-event").first().waitFor();
      ck("recent: one row per event", await page.getByTestId("connection-event").count() === 2);
      const first = await page.getByTestId("connection-event").first().innerText();
      ck("recent: key, type, when, and the text whole", first.includes("orders/1002 · row") && first.includes("10 hours ago") && first.includes("customer: c-77"), first);
      await words(page, "recent");
      await shot(page, "events-light-1440.png");

      // Edit opens Configuration.
      await page.getByTestId("connection-labels").getByRole("button", { name: "Edit" }).click();
      ck("edit: opens the Configuration tab", (await page.getByRole("tab", { name: "Configuration" }).getAttribute("aria-selected")) === "true");
      ck("edit: the form is there, its name locked", await page.getByRole("button", { name: "Save changes" }).isVisible());
      await ctx.close();
    }

    // Pause and resume.
    {
      const { ctx, page, calls } = await open();
      await ready(page);
      await page.getByRole("button", { name: "Pause" }).click();
      await page.waitForTimeout(300);
      ck("pause: asks the daemon to pause it", calls.includes("POST /pause"), calls.join(" | "));
      await ctx.close();
      const r = await open({ source: { ...ORDERS, paused: true, health: { ...ORDERS.health, status: "paused" } } });
      await ready(r.page);
      ck("paused: the state says so", /^paused$/i.test(await r.page.getByTestId("connection-state").innerText()));
      await r.page.getByRole("button", { name: "Resume" }).click();
      await r.page.waitForTimeout(300);
      ck("resume: asks the daemon to resume it", r.calls.includes("POST /resume"), r.calls.join(" | "));
      await r.ctx.close();
    }

    // Delete, taking a dependent along and removing the events.
    {
      const { ctx, page, calls } = await open({ dependents: [{ kind: "trigger", name: "orders-watch" }] });
      await ready(page);
      await page.getByTestId("connection-delete").click();
      const dlg = page.getByTestId("connection-delete-dialog");
      await dlg.getByText("orders-watch").waitFor();
      const t = await flat(dlg);
      ck("delete: names the connection by its title", t.startsWith("Delete orders?"), t);
      ck("delete: says the events stay unless removed", t.includes("The events it kept stay unless you remove them too"), t);
      ck("delete: offers to remove the 4 events", t.includes("Also remove the events it kept (4)"), t);
      ck("delete: names what depends on it in page words", t.includes("rule orders-watch") && t.includes("Delete it as well"), t);
      await words(page, "delete");
      await shot(page, "delete-light-1440.png");
      await page.getByTestId("connection-purge").click();
      await page.getByTestId("connection-delete-confirm").click();
      await page.waitForURL(/\/sources$/);
      ck("delete: removes the events and what depends on it", calls.includes("DELETE /?purge_events=true&cascade=true"), calls.join(" | "));
      ck("delete: back on the list", page.url().endsWith("/sources"));
      await ctx.close();
    }
    {
      const { ctx, page, calls } = await open({ del: 409 });
      await ready(page);
      await page.getByTestId("connection-delete").click();
      await page.getByTestId("connection-delete-confirm").click();
      const e = page.getByTestId("connection-action-error");
      await e.waitFor();
      ck("delete refused: says why, stays on the page, keeps the events",
         (await e.innerText()).includes("in use by a running agent") && page.url().endsWith("/sources/pg_orders")
         && calls.includes("DELETE /?purge_events=false&cascade=true"), `${await e.innerText()} ${calls.join(" | ")}`);
      ck("delete refused: the dialog closes", await page.getByTestId("connection-delete-dialog").count() === 0);
      await ctx.close();
    }

    // A push connection that has received nothing, and nothing to sample.
    {
      const { ctx, page } = await open({ source: ALERTS, fields: { sampled: 0, fields: [], labels: [] }, events: [] });
      await ready(page);
      const kind = await flat(page.getByTestId("connection-kind"));
      ck("push: no interval, in no project", kind === "Alertmanager alerts, in no project", kind);
      ck("push: the state says it receives pushes", /^receives pushes$/i.test(await page.getByTestId("connection-state").innerText()));
      ck("push: nothing received yet", (await page.getByTestId("connection-last").innerText()) === "nothing received yet");
      ck("push: its endpoint to post to", await page.getByText("endpoint to post to").isVisible()
         && (await page.getByTestId("connection-page").innerText()).includes("/ingest/k-am"));
      ck("no labels: says what that costs", (await page.getByTestId("connection-labels").innerText()).includes("agents cannot slice this connection"));
      ck("no fields: says so", await page.getByText("No events to sample yet").isVisible());
      await page.getByRole("tab", { name: "Recent events" }).click();
      await page.getByText("Nothing received from this connection yet.").waitFor();
      ck("no events: says so", await page.getByText("Nothing received from this connection yet.").isVisible());
      await words(page, "push");
      await ctx.close();
    }

    // Sampling and recent events failing.
    {
      const { ctx, page } = await open({ fields: 500, events: 500 });
      await ready(page);
      await page.getByText("Tares could not sample the fields").waitFor();
      ck("fields failing: says so with the reason", (await page.getByText("Tares could not sample the fields").innerText()).includes("sampling failed"));
      await page.getByRole("tab", { name: "Recent events" }).click();
      ck("recent failing: says so with the reason", (await page.getByText("Tares could not load the recent events").innerText()).includes("read failed"));
      await ctx.close();
    }

    // A connection that is not there.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.goto(`${d.base}/sources/no_such_one`, { waitUntil: "load" });
      const e = page.getByText("Tares could not load this connection");
      await e.waitFor();
      ck("missing: says it could not load the connection", await e.isVisible());
      await ctx.close();
    }

    // Dark, then 390 light and dark.
    {
      const { ctx, page } = await open({ dark: true });
      await ready(page);
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      ck("dark: the page is dark", /rgb\((\d+), (\d+), (\d+)\)/.test(bg) && bg.match(/\d+/g).slice(0, 3).every((n) => Number(n) < 80), bg);
      await fieldRows(page, "connection-fields").first().waitFor();
      await shot(page, "fields-dark-1440.png");
      await ctx.close();
    }
    for (const dark of [false, true]) {
      const { ctx, page } = await open({ width: 390, dark });
      await ready(page);
      await fieldRows(page, "connection-fields").first().waitFor();
      const label = `390 ${dark ? "dark" : "light"}`;
      ck(`${label}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const box = await page.getByTestId("connection-error").boundingBox();
      ck(`${label}: the error stays on screen`, box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
      await shot(page, `fields-${dark ? "dark" : "light"}-390.png`);
      await page.getByRole("tab", { name: "Recent events" }).click();
      await page.getByTestId("connection-event").first().waitFor();
      ck(`${label}: recent events, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
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
