/*
 * Browser e2e for the old addresses and the not-found page (TR-1394): /agents/<name>[?run=],
 * /triggers/<name> and /dispatches/<id> land on the same thing inside its project; when Tares no longer
 * knows it, or the lookup fails, the page says so in the edition's words with a way on; while it looks,
 * it says what it is opening. Any other address is "Page not found". Light and dark, 1440 and 390.
 * The daemon is real; the three lookups are mocked (redirects.fixture.cjs).
 * Run:  npm run build && node tests/redirects.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockLookups } = require("./redirects.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9982);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const MAIN_TEXT = () => {
  const el = document.querySelector("main") || document.body;
  return el.innerText;
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.count()) === 1 ? (await loc.innerText()).replace(/\s+/g, " ").trim() : `<${await loc.count()} found>`;
const where = (page) => { const u = new URL(page.url()); return u.pathname + u.search; };

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async (to, { width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await mockLookups(page, mock);
    await page.goto(`${d.base}${to}`, { waitUntil: "commit" });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors };
  };
  // Where an address lands: the first /projects/… it reaches, before the project page does anything.
  const lands = async (page, re) => page.waitForURL(re, { timeout: 10000, waitUntil: "commit" }).then(() => where(page), () => where(page));
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(MAIN_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const gone = (page) => page.getByTestId("redirect-gone");
  const goneSays = async (page, label, { title, line }) => {
    await gone(page).waitFor({ timeout: 10000 }).catch(() => {});
    ck(`${label}: titled`, (await page.getByRole("heading", { name: title, exact: true }).count()) === 1, await flat(gone(page)));
    ck(`${label}: says what is missing`, (await flat(gone(page))).includes(line), await flat(gone(page)));
    const link = page.getByRole("link", { name: "See the projects" });
    ck(`${label}: offers the projects`, (await link.count()) === 1 && (await link.getAttribute("href")) === "/projects");
    await words(page, label);
  };

  try {
    // ── each old address lands inside its project ─────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/agents/watcher");
      ck("agent: lands on the agent in its project", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=agent%3Awatcher", where(page));
      await ctx.close();
      ck("agent: no page errors", !errors.length, errors.join(" | "));
    }
    {
      const { ctx, page } = await open("/agents/watcher?run=r-42");
      ck("agent with a session: keeps the session", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=agent%3Awatcher&run=r-42", where(page));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/triggers/shop-errors");
      ck("rule: lands on the rule in its project", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=trigger%3Ashop-errors", where(page));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/dispatches/d-1");
      ck("notification: lands on its thread in the project's activity", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=activity&thread=d-1", where(page));
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/firings?dispatch=d-1");
      ck("old firings link: lands on the thread too", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=activity&thread=d-1", where(page));
      await ctx.close();
    }

    // ── while it looks ────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/triggers/shop-errors", { slow: 2500 });
      const opening = page.getByTestId("redirect-opening");
      await opening.waitFor({ timeout: 5000 }).catch(() => {});
      ck("looking: says what it opens, in page words", (await flat(opening)) === "Opening the rule", await flat(opening));
      ck("looking: is a status for screen readers", (await opening.getAttribute("role")) === "status");
      await words(page, "looking");
      ck("looking: then lands", await lands(page, /\/projects\/shop\?/) === "/projects/shop?view=trigger%3Ashop-errors", where(page));
      await ctx.close();
      ck("looking: no page errors", !errors.length, errors.join(" | "));
    }

    // ── what Tares no longer knows ────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/agents/gone-agent");
      await goneSays(page, "agent gone", { title: "No such agent", line: "Tares has no agent by that name any more." });
      ck("agent gone: stays on its address", where(page) === "/agents/gone-agent", where(page));
      await ctx.close();
      ck("agent gone: no page errors", !errors.length, errors.join(" | "));
    }
    {
      const { ctx, page } = await open("/agents/loose");
      await goneSays(page, "agent with no project", { title: "No such agent", line: "Tares has no agent by that name any more." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/triggers/gone-rule");
      await goneSays(page, "rule gone", { title: "No such rule", line: "Tares has no rule by that name any more." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/dispatches/d-404");
      await goneSays(page, "notification gone", { title: "No such notification", line: "Tares has no record of that notification." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/dispatches/d-1", { known: { triggers: [] } });
      await goneSays(page, "notification whose rule is gone", { title: "No such notification", line: "Tares has no record of that notification." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/triggers/shop-errors", { fail: true });
      await goneSays(page, "lookup fails", { title: "No such rule", line: "Tares has no rule by that name any more." });
      await ctx.close();
    }

    // ── an address the console does not know ──────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open("/no/such/page");
      const nf = page.getByTestId("not-found");
      await nf.waitFor({ timeout: 10000 }).catch(() => {});
      ck("not found: titled", (await page.getByRole("heading", { name: "Page not found", exact: true }).count()) === 1);
      ck("not found: keeps the sentence other suites look for", (await flat(nf)).includes("This page does not exist."), await flat(nf));
      ck("not found: says why it may be", (await flat(nf)).includes("The address may be old, or mistyped."), await flat(nf));
      const link = page.getByRole("link", { name: "See the projects" });
      ck("not found: offers the projects", (await link.count()) === 1 && (await link.getAttribute("href")) === "/projects");
      ck("not found: stays on its address", where(page) === "/no/such/page", where(page));
      await words(page, "not found");
      await link.click();
      ck("not found: the way on goes to the projects", await lands(page, /\/projects$/) === "/projects", where(page));
      await ctx.close();
      ck("not found: no page errors", !errors.length, errors.join(" | "));
    }

    // ── a connection's page, straight from one to another ───────────────────────────────────────────
    {
      const yaml = ["alpha-conn", "beta-conn"].map((n) => `  - name: ${n}\n    connector: docker_logs\n    poll: 30s\n    config: {container: ${n}-1}\n`).join("");
      const r = await fetch(`${d.base}/api/catalog/import`, { method: "POST", headers: { "content-type": "application/json" },
                                                              body: JSON.stringify({ yaml: `sources:\n${yaml}`, mode: "merge" }) });
      ck("connection: two connections to move between", r.ok, String(r.status));
      const { ctx, page, errors } = await open("/sources/alpha-conn");
      const h1 = page.getByRole("heading", { level: 1 });
      await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("alpha-conn"), null, { timeout: 10000 }).catch(() => {});
      ck("connection: opens by name", (await h1.innerText()).includes("alpha-conn"), await h1.innerText());
      await page.evaluate(() => { history.pushState({}, "", "/sources/beta-conn"); dispatchEvent(new PopStateEvent("popstate")); });
      await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("beta-conn"), null, { timeout: 10000 }).catch(() => {});
      ck("connection: moving to another shows the other", (await h1.innerText()).includes("beta-conn"), await h1.innerText());
      const text = await page.evaluate(MAIN_TEXT);
      ck("connection: and nothing of the first", !text.includes("alpha-conn"), text.slice(0, 200));
      // What the page itself holds (the open tab) starts over too: the raw tab buttons mark the open one
      // with .active, the kit's Tabs with aria-selected.
      const active = (label) => page.evaluate((l) => [...document.querySelectorAll("main button, main [role=tab]")]
        .some((b) => b.textContent.trim() === l && (b.classList.contains("active") || b.getAttribute("aria-selected") === "true")), label);
      await page.evaluate(() => { history.pushState({}, "", "/sources/alpha-conn"); dispatchEvent(new PopStateEvent("popstate")); });
      await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("alpha-conn"), null, { timeout: 10000 }).catch(() => {});
      await page.getByText("Configuration", { exact: true }).click();
      ck("connection: a tab can be opened", await active("Configuration"));
      await page.evaluate(() => { history.pushState({}, "", "/sources/beta-conn"); dispatchEvent(new PopStateEvent("popstate")); });
      await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("beta-conn"), null, { timeout: 10000 }).catch(() => {});
      ck("connection: the other opens on its first tab, not the one left open", !(await active("Configuration")) && await active("Fields"));
      await ctx.close();
      ck("connection: no page errors", !errors.length, errors.join(" | "));
    }

    // ── sre words ─────────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open("/triggers/gone-rule", { edition: "sre" });
      await goneSays(page, "sre rule gone", { title: "No such check rule", line: "Tares has no check rule by that name any more." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/dispatches/d-404", { edition: "sre" });
      await goneSays(page, "sre notification gone", { title: "No such alert", line: "Tares has no record of that alert." });
      await ctx.close();
    }
    {
      const { ctx, page } = await open("/triggers/shop-errors", { edition: "sre", slow: 2500 });
      const opening = page.getByTestId("redirect-opening");
      await opening.waitFor({ timeout: 5000 }).catch(() => {});
      ck("sre looking: says check rule", (await flat(opening)) === "Opening the check rule", await flat(opening));
      await ctx.close();
    }

    // ── light and dark, 1440 and 390 ─────────────────────────────────────────────────────────────
    for (const [width, dark] of [[1440, true], [390, false], [390, true]]) {
      const tag = `${width}${dark ? " dark" : ""}`;
      {
        const { ctx, page } = await open("/triggers/gone-rule", { width, dark });
        await gone(page).waitFor({ timeout: 10000 }).catch(() => {});
        ck(`${tag}: gone shows`, (await gone(page).count()) === 1);
        ck(`${tag}: gone has no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
        await ctx.close();
      }
      {
        const { ctx, page } = await open("/no/such/page", { width, dark });
        await page.getByTestId("not-found").waitFor({ timeout: 10000 }).catch(() => {});
        ck(`${tag}: not found shows`, (await page.getByTestId("not-found").count()) === 1);
        ck(`${tag}: not found has no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
        await ctx.close();
      }
    }
  } catch (e) {
    fail++;
    console.log("  FAIL crashed: " + (e && e.stack || e));
  }
  await browser.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
