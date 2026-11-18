/*
 * Browser e2e for Discover on /sources/discover (TR-1394): the proposals and their words, picking, adding
 * (all at once, and a partial failure retried without adding twice), Docker unreachable, nothing found,
 * looking again, the kinds list failing, light and dark, 1440 and 390. The daemon is real; the Docker scan
 * and every create are mocked at the network (source-discover.fixture.cjs). The words check reads the
 * page's own text, minus what Docker or the daemon returned.
 * Run:  npm run build && node tests/source-discover.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { DOCKER_DOWN, EMPTY, SCAN, mockDiscover } = require("./source-discover.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9974);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=discover-page]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return el ? el.innerText : ""; } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const open = async ({ width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const m = await mockDiscover(page, mock);
    await page.goto(`${d.base}/sources/discover`, { waitUntil: "load" });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, ...m };
  };
  const row = (page, name) => page.locator(`[data-testid=discover-proposal][data-name="${name}"]`);
  const box = (page, name) => row(page, name).getByRole("checkbox");
  const ready = (page) => page.getByTestId("discover-proposed").waitFor({ timeout: 15000 });
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const add = (page) => page.getByTestId("discover-add");

  try {
    // ── the proposals ──────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, calls } = await open();
      await ready(page);
      ck("scan: titled in page words", (await page.getByRole("heading", { level: 1 }).innerText()) === "Discover connections");
      ck("scan: says what it found", (await flat(page.getByTestId("discover-summary"))) === "5 containers found, 3 connections proposed.",
         await flat(page.getByTestId("discover-summary")));
      ck("scan: one row per proposal", (await page.getByTestId("discover-proposal").count()) === SCAN.proposed_sources.length);
      ck("scan: the daemon's picks are ticked",
         (await box(page, "web_logs").isChecked()) && !(await box(page, "worker_logs").isChecked()) && (await box(page, "metrics").isChecked()));
      const about = await flat(row(page, "web_logs").locator("p").first());
      ck("scan: a Docker logs proposal is described in page words",
         about === "Docker logs. Every log line of the web service in shop, from container shop-web-1.", about);
      ck("scan: a Docker logs proposal does not show the daemon's text", (await row(page, "web_logs").locator("[data-help=daemon]").count()) === 0);
      ck("scan: any other kind shows the daemon's text, marked as such",
         (await row(page, "metrics").locator("[data-help=daemon][data-words-exempt]").innerText()).startsWith("1204 metrics"));
      ck("scan: the kind is named by its label", (await flat(row(page, "metrics").locator("p").first())).startsWith("Prometheus."));
      ck("scan: the description is tied to its checkbox",
         (await box(page, "web_logs").getAttribute("aria-describedby")) === "proposal-web_logs-about");
      ck("scan: the button counts the picks", (await add(page).innerText()) === "Add 2 connections");
      ck("scan: what has nothing to connect is listed", (await page.getByTestId("discover-skip").count()) === 2);
      ck("scan: why is the daemon's text, marked as such",
         (await page.getByTestId("discover-skip").first().locator("[data-help=daemon][data-words-exempt]").count()) === 1);
      ck("scan: nothing is added before a pick", calls.length === 0);
      await words(page, "scan");

      // picking
      await box(page, "web_logs").click();
      await box(page, "metrics").click();
      ck("pick: none picked says so", (await add(page).innerText()) === "Pick a connection to add");
      ck("pick: and can't be pressed", await add(page).isDisabled());
      await box(page, "worker_logs").click();
      ck("pick: one picked, singular", (await add(page).innerText()) === "Add 1 connection");
      await ctx.close();
    }

    // ── adding the picks ───────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, calls } = await open();
      await ready(page);
      await add(page).click();
      await page.getByTestId("discover-added").waitFor();
      ck("add: only the picks are added", JSON.stringify(calls.map((c) => c.name)) === JSON.stringify(["web_logs", "metrics"]),
         JSON.stringify(calls.map((c) => c.name)));
      const web = calls.find((c) => c.name === "web_logs");
      ck("add: with the proposal's kind and settings",
         web && web.connector === "docker_logs" && JSON.stringify(web.config) === JSON.stringify(SCAN.proposed_sources[0].config), JSON.stringify(web));
      ck("add: says how many", (await flat(page.getByTestId("discover-added"))).startsWith("Added 2 connections. Tares starts on them at once."),
         await flat(page.getByTestId("discover-added")));
      ck("add: each added row says so", (await page.getByTestId("discover-proposal-added").count()) === 2);
      ck("add: an added row can't be picked again", await box(page, "web_logs").isDisabled());
      ck("add: the add button is gone", (await add(page).count()) === 0);
      await words(page, "added");
      await page.waitForTimeout(2000);
      ck("add: the page stays put (no timed redirect)", new URL(page.url()).pathname === "/sources/discover", page.url());
      await page.getByTestId("discover-open").click();
      await page.waitForURL((u) => u.pathname === "/sources");
      ck("add: Open connections goes to the list", true);
      await ctx.close();
    }

    // ── a partial failure, retried ─────────────────────────────────────────────────────────────────
    {
      const { ctx, page, calls } = await open({ failOnce: ["metrics"] });
      await ready(page);
      await add(page).click();
      await page.getByTestId("discover-failed").waitFor();
      ck("fail: says how many made it", (await flat(page.getByTestId("discover-failed"))).startsWith("Added 1 of 2."),
         await flat(page.getByTestId("discover-failed")));
      ck("fail: the failed row says why",
         (await flat(row(page, "metrics").getByTestId("discover-proposal-error"))) === "Not added: a connection named metrics already exists");
      ck("fail: the added row says so", (await row(page, "web_logs").getByTestId("discover-proposal-added").count()) === 1);
      ck("fail: the button offers the rest", (await add(page).innerText()) === "Add 1 connection");
      await words(page, "failed");
      await add(page).click();
      await page.getByTestId("discover-added").waitFor();
      ck("retry: only the failed one is sent again", JSON.stringify(calls.map((c) => c.name)) === JSON.stringify(["web_logs", "metrics", "metrics"]),
         JSON.stringify(calls.map((c) => c.name)));
      ck("retry: then all are added", (await flat(page.getByTestId("discover-added"))).startsWith("Added 2 connections."));
      ck("retry: the error is gone", (await page.getByTestId("discover-proposal-error").count()) === 0);
      await ctx.close();
    }

    // ── Docker unreachable ─────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ scan: DOCKER_DOWN });
      await page.getByTestId("discover-error").waitFor({ timeout: 15000 });
      const text = await flat(page.getByTestId("discover-error"));
      ck("docker: says Tares could not read Docker", text.startsWith("Tares could not read Docker on the machine it runs on:"), text);
      ck("docker: with the reason", text.includes("Cannot connect to the Docker daemon"), text);
      ck("docker: no proposals", (await page.getByTestId("discover-proposed").count()) === 0);
      await words(page, "docker");
      await page.getByRole("link", { name: "Add one by hand" }).click();
      await page.waitForURL((u) => u.pathname === "/sources/new");
      ck("docker: Add one by hand goes to the picker", true);
      await ctx.close();
    }

    // ── nothing found ──────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ scan: EMPTY });
      await page.getByTestId("discover-empty").waitFor({ timeout: 15000 });
      ck("empty: says what it looks for", (await flat(page.getByTestId("discover-empty"))).includes("each container started by Docker Compose"));
      ck("empty: no add button", (await add(page).count()) === 0);
      ck("empty: offers adding by hand", (await page.getByTestId("discover-empty").getByRole("link", { name: "Add one by hand" }).count()) === 1);
      await words(page, "empty");
      await ctx.close();
    }

    // ── looking again ──────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, scans } = await open();
      await ready(page);
      await add(page).click();
      await page.getByTestId("discover-added").waitFor();
      const before = scans.n;
      await page.getByTestId("discover-rescan").click();
      await ready(page);
      await page.getByTestId("discover-added").waitFor({ state: "detached" });
      ck("again: scans once more", scans.n === before + 1, `${before} -> ${scans.n}`);
      ck("again: the results are cleared", (await page.getByTestId("discover-proposal-added").count()) === 0);
      ck("again: the picks are the daemon's again", (await add(page).innerText()) === "Add 2 connections");
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ scan: [SCAN, DOCKER_DOWN] });
      await ready(page);
      await page.getByTestId("discover-rescan").click();
      await page.getByTestId("discover-error").waitFor({ timeout: 15000 });
      ck("again, Docker gone: the error shows", true);
      ck("again, Docker gone: the last proposals are cleared", (await page.getByTestId("discover-proposal").count()) === 0);
      ck("again, Docker gone: so is the count", (await page.getByTestId("discover-summary").count()) === 0);
      await ctx.close();
    }

    // ── the kinds list failing ─────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ connectorsFail: true });
      await ready(page);
      ck("no kinds: proposals still show", (await page.getByTestId("discover-proposal").count()) === 3);
      ck("no kinds: the kind falls back to its name, as system text",
         (await row(page, "metrics").locator("p").first().locator("[data-user-text]").first().innerText()) === "prometheus");
      await ctx.close();
    }

    // ── dark, and a phone ──────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ dark: true });
      await ready(page);
      await words(page, "dark");
      await ctx.close();
    }
    for (const dark of [false, true]) {
      const label = `390 ${dark ? "dark" : "light"}`;
      const { ctx, page } = await open({ width: 390, dark, failOnce: ["metrics"] });
      await ready(page);
      ck(`${label}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      await add(page).click();
      await page.getByTestId("discover-failed").waitFor();
      ck(`${label}: after a failure, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
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
