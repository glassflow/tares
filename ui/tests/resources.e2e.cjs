/*
 * Browser e2e for All resources (/resources, TR-1395): one tab per kind, in the address; each row names
 * the part, says what it is, its state in page words with the tone from the one table, when it last did
 * something, and the projects that use it, each a link; search and "only those in no project" narrow it;
 * an empty kind, a failed load (with a retry) and a slow load each say so. The SRE edition's words.
 * Light and dark, 1440 and 390. The daemon is real; /api/resources is mocked (resources.fixture.cjs),
 * except one check that reads a connection imported into the real daemon.
 * Run:  npm run build && node tests/resources.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockResources } = require("./resources.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9986);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const MAIN_TEXT = () => (document.querySelector("main") || document.body).innerText;
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;
const where = (page) => { const u = new URL(page.url()); return u.pathname + u.search; };

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async (to, { width = 1440, dark = false, wait = true, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const calls = await mockResources(page, mock);
    await page.goto(`${d.base}${to}`);
    if (wait) await page.getByRole("tablist").waitFor({ timeout: 10000 }).catch(() => {});
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors, calls };
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(MAIN_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const tabs = (page) => page.getByRole("tab").allInnerTexts().then((t) => t.map((s) => s.trim()));
  const selected = (page) => flat(page.locator("[role=tab][aria-selected=true]"));
  const rows = (page) => page.getByTestId("resource-row");
  const row = (page, name) => rows(page).filter({ has: page.getByTestId("resource-name").getByText(name, { exact: true }) });
  const nameHref = (r) => r.getByTestId("resource-name").evaluate((e) => (e.tagName === "A" ? e.getAttribute("href") : null));
  const state = async (r) => ({ text: (await r.getByTestId("resource-state").textContent()).trim(), tone: await r.getByTestId("resource-state").getAttribute("data-tone") });
  const usedBy = (r) => r.getByTestId("resource-used-by").evaluate((e) => ({
    text: e.innerText.trim(), links: [...e.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]) }));
  const pick = async (page, label) => { await page.getByRole("tab", { name: label }).click(); await page.waitForTimeout(150); };

  try {
    // ── the page and its tabs ─────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/resources");
      ck("page: titled All resources", (await page.getByRole("heading", { level: 1 }).innerText()) === "All resources");
      ck("page: says what it holds, with rules", (await page.evaluate(MAIN_TEXT)).includes("Every connection, rule and agent on this Tares, and the projects that use each."));
      ck("page: one tab per kind, in page words, with counts",
         JSON.stringify(await tabs(page)) === JSON.stringify(["Connections (2)", "Rules (2)", "Agents (2)", "Tools (2)", "Skills (0)", "Keys (2)"]), (await tabs(page)).join(" | "));
      ck("page: the tabs are named", (await page.getByRole("tablist", { name: "Kinds of resource" }).count()) === 1);
      ck("page: opens on connections", (await selected(page)) === "Connections (2)", await selected(page));
      ck("page: says what a connection is", (await flat(page.getByTestId("resources-what"))) === "Where events come from.");
      ck("page: counts what it shows", (await flat(page.getByTestId("resources-count"))) === "2 of 2", await flat(page.getByTestId("resources-count")));
      ck("page: no raw table or second sidebar", (await page.locator("main table, main .pnav, main .pnav-select").count()) === 0);
      await words(page, "connections");

      // ── a connection with a title, two projects and a recent event ─────────────────────────────
      const gh = row(page, "acme/shop");
      ck("connection: one row by its title", (await gh.count()) === 1);
      ck("connection: the title opens its page", (await nameHref(gh)) === "/sources/gh-shop", String(await nameHref(gh)));
      ck("connection: and keeps its internal name beside", (await flat(gh)).includes("gh-shop"), await flat(gh));
      ck("connection: says what it is, no detail while receiving", (await flat(gh)).includes("every 30s") === false && (await flat(gh)).includes("GitHub"), await flat(gh));
      const ghUsed = await usedBy(gh);
      ck("connection: names every project, each a link",
         ghUsed.text === "In Billing, Shop" && JSON.stringify(ghUsed.links) === JSON.stringify([["Billing", "/projects/billing"], ["Shop", "/projects/shop"]]), JSON.stringify(ghUsed));
      ck("connection: receiving reads ready", JSON.stringify(await state(gh)) === JSON.stringify({ text: "receiving", tone: "ready" }), JSON.stringify(await state(gh)));
      ck("connection: when it last got an event", /^last event 3 min(ute)?s? ago$/.test(await flat(gh.getByTestId("resource-last"))), await flat(gh.getByTestId("resource-last")));

      // ── a failing connection in no project ─────────────────────────────────────────────────────
      const pg = row(page, "pg-logs");
      ck("failing: shows why", (await flat(pg)).includes("Postgres. Connection refused"), await flat(pg));
      ck("failing: reads failing, bad", JSON.stringify(await state(pg)) === JSON.stringify({ text: "failing", tone: "bad" }), JSON.stringify(await state(pg)));
      ck("failing: in no project", (await usedBy(pg)).text === "In no project", (await usedBy(pg)).text);
      ck("failing: never had an event", (await flat(pg.getByTestId("resource-last"))) === "last event never");

      // ── search and loose ───────────────────────────────────────────────────────────────────────
      const search = page.getByLabel("Search", { exact: true });
      await search.fill("billing");
      ck("search: by project name", (await rows(page).count()) === 1 && (await row(page, "acme/shop").count()) === 1);
      ck("search: counts what it shows", (await flat(page.getByTestId("resources-count"))) === "1 of 2");
      await search.fill("zzz");
      ck("search: nothing matches says so", (await flat(page.getByTestId("resources-none"))) === "Nothing matches.", await flat(page.getByTestId("resources-none")));
      await search.fill("");
      await page.getByRole("checkbox", { name: "Only those in no project" }).click();
      ck("loose: only those in no project", (await rows(page).count()) === 1 && (await row(page, "pg-logs").count()) === 1);
      await search.fill("pg");

      // ── rules, by tab: the address follows, and the filters start over ─────────────────────────
      await pick(page, "Rules (2)");
      ck("rules: the address follows the tab", where(page) === "/resources/triggers", where(page));
      ck("rules: the tab is open", (await selected(page)) === "Rules (2)");
      ck("rules: the filters start over", (await rows(page).count()) === 2 && (await page.getByLabel("Search", { exact: true }).inputValue()) === ""
         && !(await page.getByRole("checkbox", { name: "Only those in no project" }).isChecked()));
      ck("rules: says what a rule is", (await flat(page.getByTestId("resources-what"))) === "What happens that wakes the agents.");
      const se = row(page, "shop-errors");
      ck("rule: opens its page", (await nameHref(se)) === "/triggers/shop-errors", String(await nameHref(se)));
      ck("rule: active reads on, ready", JSON.stringify(await state(se)) === JSON.stringify({ text: "on", tone: "ready" }), JSON.stringify(await state(se)));
      ck("rule: when it last woke the agents", /^last woke the agents 2 h(ou)?rs? ago$/.test(await flat(se.getByTestId("resource-last"))), await flat(se.getByTestId("resource-last")));
      ck("rule paused: reads paused, waiting", JSON.stringify(await state(row(page, "old-rule"))) === JSON.stringify({ text: "paused", tone: "waiting" }));
      await words(page, "rules");
      await page.goBack();
      ck("back: returns to connections", where(page) === "/resources" && (await selected(page)) === "Connections (2)", where(page));
      await ctx.close();
      ck("page: no page errors", !errors.length, errors.join(" | "));
    }

    // ── each kind by its address ──────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/resources/agents");
      ck("agents: opens on its tab from the address", (await selected(page)) === "Agents (2)", await selected(page));
      const w = row(page, "watcher");
      ck("agent: opens its page", (await nameHref(w)) === "/agents/watcher");
      ck("agent: when it last worked", /^last worked 1 day ago$/.test(await flat(w.getByTestId("resource-last"))), await flat(w.getByTestId("resource-last")));
      const h = row(page, "helper");
      ck("agent on handoff: reads on handoff, quiet", JSON.stringify(await state(h)) === JSON.stringify({ text: "on handoff", tone: "quiet" }), JSON.stringify(await state(h)));
      ck("agent in no project: still opens its page", (await nameHref(h)) === "/agents/helper");
      await words(page, "agents");
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/resources/tools");
      const a = rows(page).nth(0), b = rows(page).nth(1);
      ck("tool: shown by its service, internal name beside", (await flat(a)).includes("mcp.example.com") && (await flat(a)).includes("tool-a"), await flat(a));
      ck("tool: opens the tools of the project using it", (await nameHref(a)) === "/projects/shop?view=settings:mcp", String(await nameHref(a)));
      ck("tool: credentials set reads ready", JSON.stringify(await state(a)) === JSON.stringify({ text: "credentials set", tone: "ready" }), JSON.stringify(await state(a)));
      ck("tool in no project: not a link", (await nameHref(b)) === null);
      ck("tool: no credentials reads bad", JSON.stringify(await state(b)) === JSON.stringify({ text: "no credentials", tone: "bad" }));
      ck("tool: no last-used line", (await page.getByTestId("resource-last").count()) === 0);
      await words(page, "tools");
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/resources/skills");
      ck("skills empty: titled", (await page.getByRole("heading", { name: "No skills yet", exact: true }).count()) === 1);
      ck("skills empty: says so", (await flat(page.getByTestId("resources-empty"))).includes("Nothing of this kind is on this Tares yet."), await flat(page.getByTestId("resources-empty")));
      ck("skills empty: no filters for nothing", (await page.getByLabel("Search", { exact: true }).count()) === 0);
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/resources/keys");
      const s = row(page, "Console sign-in for a workspace member");
      ck("key: shown by its title, internal name beside", (await s.count()) === 1 && (await flat(s)).includes("user:42"), await flat(rows(page).nth(0)));
      ck("key in no project: opens settings", (await nameHref(s)) === "/settings");
      ck("key: when it was last used", /^last used 5 min(ute)?s? ago$/.test(await flat(s.getByTestId("resource-last"))), await flat(s.getByTestId("resource-last")));
      const c = row(page, "ci");
      ck("project key: opens the project's keys", (await nameHref(c)) === "/projects/shop?view=settings:keys");
      ck("project key: never used reads quiet", JSON.stringify(await state(c)) === JSON.stringify({ text: "never used", tone: "quiet" }));
      ck("project key: last used never", (await flat(c.getByTestId("resource-last"))) === "last used never");
      await words(page, "keys");
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/resources/nope");
      ck("unknown kind: opens on connections", (await selected(page)) === "Connections (2)", await selected(page));
      await ctx.close();
    }

    // ── loading, failed, retried ──────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/resources", { slow: 2500, wait: false });
      await page.waitForTimeout(800);
      ck("slow: shows it is loading", (await page.locator("main [aria-busy=true], main [data-testid^=loading]").count()) > 0,
         (await page.evaluate(MAIN_TEXT)).slice(0, 120));
      ck("slow: no tabs before there is anything", (await page.getByRole("tab").count()) === 0);
      await ctx.close();
    }
    {
      const { ctx, page, calls } = await open("/resources", { fail: { until: 1 }, wait: false });
      const alert = page.getByRole("alert");
      await alert.waitFor({ timeout: 10000 }).catch(() => {});
      ck("failed: says Tares is not answering", (await flat(alert)).startsWith("Tares is not answering"), await flat(alert));
      const retry = page.getByRole("button", { name: "Try again" });
      ck("failed: offers to try again", (await retry.count()) === 1);
      await retry.click().catch(() => {});
      await page.getByRole("tablist").waitFor({ timeout: 5000 }).catch(() => {});
      ck("retry: asks again and shows the list", calls.resources >= 2 && (await rows(page).count()) === 2, `${calls.resources} calls, ${await rows(page).count()} rows`);
      ck("retry: the alert goes", (await page.getByRole("alert").count()) === 0);
      await ctx.close();
    }

    // ── the real daemon's rows ────────────────────────────────────────────────────────────────────
    {
      const r = await fetch(`${d.base}/api/catalog/import`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ yaml: "sources:\n  - name: real-conn\n    connector: docker_logs\n    poll: 30s\n    config: {container: real-1}\n", mode: "merge" }) });
      ck("real: a connection imported", r.ok, String(r.status));
      const { ctx, page, errors } = await open("/resources", { real: true });
      const real = rows(page).filter({ hasText: "real-conn" });
      await real.waitFor({ timeout: 10000 }).catch(() => {});
      ck("real: the daemon's connection is listed", (await real.count()) === 1, (await page.evaluate(MAIN_TEXT)).slice(0, 300));
      ck("real: what it is, then why, as sentences", /Docker logs\. \S/.test(await flat(real)), await flat(real));
      { const u = await usedBy(real);
        ck("real: names the project the daemon put it in, as a link", /^In \S/.test(u.text) && u.links.length >= 1 && u.links.every(([, h]) => h.startsWith("/projects/")), JSON.stringify(u)); }
      ck("real: its state has a tone from the table", ["ready", "waiting", "quiet", "bad"].includes((await state(real)).tone), JSON.stringify(await state(real)));
      await ctx.close();
      ck("real: no page errors", !errors.length, errors.join(" | "));
    }

    // ── sre words ─────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/resources", { edition: "sre" });
      await page.waitForFunction(() => [...document.querySelectorAll("[role=tab]")].some((t) => t.textContent.startsWith("Check rules")), null, { timeout: 10000 }).catch(() => {});
      ck("sre: the rules tab says check rules", (await tabs(page)).includes("Check rules (2)"), (await tabs(page)).join(" | "));
      ck("sre: the page says check rule", (await page.evaluate(MAIN_TEXT)).includes("Every connection, check rule and agent on this Tares"));
      await pick(page, "Check rules (2)");
      ck("sre: the list is titled check rules", (await page.getByRole("heading", { name: "Check rules", exact: true }).count()) === 1);
      await words(page, "sre");
      await ctx.close();
    }

    // ── widths and themes ─────────────────────────────────────────────────────────────────────────
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const tag = `${width} ${dark ? "dark" : "light"}`;
      const { ctx, page } = await open("/resources", { width, dark });
      ck(`${tag}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      ck(`${tag}: every tab is reachable`, (await page.getByRole("tab").count()) === 6);
      await pick(page, "Keys (2)");
      ck(`${tag}: keys, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const box = await rows(page).nth(0).boundingBox();
      ck(`${tag}: a row fits the width`, !!box && box.x >= 0 && box.x + box.width <= width + 0.5, JSON.stringify(box));
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
