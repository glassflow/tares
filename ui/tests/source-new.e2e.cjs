/*
 * Browser e2e for adding a connection on /sources/new (TR-1394): the kind picker and its words, a kind Docker
 * can't run, Claude Code's own page, the project a connection joins, creating a connection that Tares reads
 * and one that sends to Tares (with its key on a cell that requires one, the key failing, an open cell), the
 * YAML box, light and dark, 1440 and 390. The daemon is real; auth, Docker and every write are mocked at the
 * network (source-new.fixture.cjs mockNew). The words check reads the page's own text, not the shell around it.
 * Run:  npm run build && node tests/source-new.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots.
 */
const path = require("path");
const { chromium } = require("./browser.cjs");
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { SECRET, SHOP, kindButton, mockNew } = require("./source-new.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9972);
const SHOTS = process.env.SHOTS_DIR;
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

// The page's own text, minus what a person named or a system returned.
const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=add-connection-page]");
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

  const open = async ({ width = 1440, dark = false, query = "", ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const calls = [];
    await mockNew(page, { ...mock, calls });
    await page.goto(`${d.base}/sources/new${query}`, { waitUntil: "load" });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, calls };
  };
  const ready = (page) => kindButton(page, "loki").waitFor({ timeout: 15000 });
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };
  const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
  const create = async (page, key, name, fill = async () => {}) => {
    await kindButton(page, key).click();
    await page.getByTestId("connection-form").waitFor();
    await fill(page);
    await page.getByLabel("Name (required)").fill(name);
    await page.getByRole("button", { name: "Create connection" }).click();
  };
  const fillLoki = async (page) => {
    await page.getByLabel("URL (required)").fill("http://loki:3100");
    await page.getByLabel("Query (required)").fill('{app="shop"}');
  };

  // What the daemon offers, read from the daemon itself, so a kind added later can't skip the checks.
  const specs = await (await fetch(`${d.base}/api/connectors`)).json();
  const offered = Object.entries(specs).filter(([, s]) => !s.internal && !s.credential_managed).map(([k]) => k);
  const hidden = Object.entries(specs).filter(([, s]) => s.internal || s.credential_managed).map(([k]) => k);

  try {
    // The picker, light, 1440.
    {
      const { ctx, page } = await open();
      await ready(page);
      ck("picker: titled in page words", (await page.getByRole("heading", { level: 1 }).innerText()) === "Add a connection");
      const rows = page.getByTestId("connection-kind");
      const keys = await rows.evaluateAll((els) => els.map((e) => e.dataset.kind));
      ck("picker: every offered kind has a row", offered.every((k) => keys.includes(k)), `missing ${offered.filter((k) => !keys.includes(k))}`);
      ck("picker: kinds Tares makes itself are not offered", hidden.every((k) => !keys.includes(k)), `shown ${hidden.filter((k) => keys.includes(k))}`);
      const daemonHelp = await page.locator("[data-kind] [data-help=daemon]").evaluateAll((els) => els.map((e) => e.closest("[data-kind]").dataset.kind));
      ck("picker: every offered kind is described in page words", daemonHelp.length === 0, `falls back to the daemon's text: ${daemonHelp}`);
      for (const [mode, sample] of [["poll", "loki"], ["push", "webhook"], ["reference", "reference"]]) {
        ck(`picker: ${sample} is under the ${mode} heading`, await page.getByTestId(`kinds-${mode}`).locator(`[data-kind=${sample}]`).count() === 1);
      }
      ck("picker: a kind is a link to its form", (await kindButton(page, "loki").getAttribute("href")) === "/sources/new?kind=loki");
      ck("picker: Claude Code goes to its own page", (await kindButton(page, "claude_code").getAttribute("href")) === "/sources/claude-code");
      ck("picker: Docker reachable, docker_logs can be picked", (await kindButton(page, "docker_logs").getAttribute("href")) === "/sources/new?kind=docker_logs");
      ck("picker: no project line without a project", await page.getByTestId("add-connection-project").count() === 0);
      await words(page, "picker");
      await shot(page, "kinds-light-1440.png");

      // Pick, back, pick another.
      await kindButton(page, "loki").click();
      await page.getByTestId("connection-form").waitFor();
      ck("form: titled with the kind", (await page.getByTestId("add-connection-kind").locator("h3").innerText()) === specs.loki.label);
      ck("form: the kind's words above the form", (await page.getByTestId("add-connection-kind").locator("[data-help=page]").count()) === 1);
      ck("form: the picker is gone", await page.getByTestId("connection-kind").count() === 0);
      ck("form: no YAML toggle while filling a form", await page.getByTestId("yaml-toggle").count() === 0);
      await words(page, "form");
      await shot(page, "form-light-1440.png");
      await page.goBack();
      await ready(page);
      ck("form: the back button returns to the picker", new URL(page.url()).search === "");
      await kindButton(page, "loki").click();
      await page.getByTestId("pick-another").click();
      await ready(page);
      ck("form: Pick another kind returns to the picker", new URL(page.url()).search === "");
      await ctx.close();
    }

    // Docker unreachable: docker_logs says why and can't be picked, not even by its address.
    {
      const { ctx, page } = await open({ docker: false });
      await ready(page);
      await page.getByTestId("kind-unavailable").waitFor();
      const row = kindButton(page, "docker_logs");
      ck("no Docker: the row is not a link", (await row.evaluate((e) => e.tagName)) !== "A");
      ck("no Docker: the row is marked unavailable", (await row.getAttribute("aria-disabled")) === "true");
      ck("no Docker: the row says why", /Needs Docker/.test(await row.innerText()));
      ck("no Docker: only docker_logs is affected", await page.getByTestId("kind-unavailable").count() === 1);
      await page.goto(`${d.base}/sources/new?kind=docker_logs`, { waitUntil: "load" });
      await page.getByTestId("kind-unknown").waitFor();
      ck("no Docker: its address shows the picker, not the form", await page.getByTestId("connection-form").count() === 0);
      await ctx.close();
    }

    // Kinds the page does not add: unknown, and one Tares makes itself.
    for (const kind of ["nope", hidden[0]].filter(Boolean)) {
      const { ctx, page } = await open({ query: `?kind=${kind}` });
      await ready(page);
      ck(`?kind=${kind}: says it cannot add that kind`, (await page.getByTestId("kind-unknown").count()) === 1);
      ck(`?kind=${kind}: no form`, await page.getByTestId("connection-form").count() === 0);
      await ctx.close();
    }

    // A connection Tares reads: saved, then its page.
    {
      const { ctx, page, calls } = await open();
      await ready(page);
      await create(page, "loki", "shop-logs", fillLoki);
      await page.waitForURL(/\/sources\/shop-logs$/);
      const saved = calls.find((c) => c.call === "POST /api/sources");
      ck("poll create: saved with its kind and name", saved?.body.connector === "loki" && saved?.body.name === "shop-logs", JSON.stringify(saved));
      ck("poll create: no project unless one was asked for", saved && !("project" in saved.body), JSON.stringify(saved?.body));
      ck("poll create: no key is made", !calls.some((c) => c.call === "POST /api/keys"));
      await ctx.close();
    }

    // From a project's page: the connection joins it.
    {
      const { ctx, page, calls } = await open({ project: true, query: `?project=${SHOP.id}` });
      await ready(page);
      await page.getByTestId("add-connection-project").getByRole("link", { name: SHOP.name }).waitFor();
      ck("project: says which project it joins", (await flat(page.getByTestId("add-connection-project"))) === `It joins ${SHOP.name}.`);
      ck("project: links to the project", (await page.getByTestId("add-connection-project").getByRole("link").getAttribute("href")) === `/projects/${SHOP.id}`);
      ck("project: the kind links keep it", (await kindButton(page, "webhook").getAttribute("href")) === `/sources/new?project=${SHOP.id}&kind=webhook`);
      await create(page, "webhook", "deploys");
      await page.getByTestId("connection-created").waitFor();
      const saved = calls.find((c) => c.call === "POST /api/sources");
      ck("project: the save carries it", saved?.body.project === SHOP.id, JSON.stringify(saved?.body));
      await page.getByTestId("created-another").click();
      await ready(page);
      ck("project: Add another keeps it", new URL(page.url()).search === `?project=${SHOP.id}`, page.url());
      await ctx.close();
    }

    // A connection that sends to Tares, on a cell that requires keys.
    {
      const { ctx, page, calls } = await open({ auth: true });
      await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: d.base });
      await ready(page);
      await create(page, "webhook", "deploys");
      await page.getByTestId("connection-created").waitFor();
      const panel = page.getByTestId("connection-created");
      ck("push create: says what was added", (await flat(panel.locator("h2"))) === "Added deploys");
      ck("push create: the address to send to", (await page.getByTestId("created-url").innerText()) === `${d.base}/ingest/k-deploys`);
      const key = calls.find((c) => c.call === "POST /api/keys");
      ck("push create: a key is made that can only send data", JSON.stringify(key?.body.scopes) === '["ingest"]', JSON.stringify(key?.body));
      ck("push create: the key is named for the connection", key?.body.name === "ingest: deploys", key?.body.name);
      ck("push create: the key is shown", (await page.getByTestId("created-key-secret").innerText()) === SECRET);
      ck("push create: it says the key is shown once", /not shown again/.test(await page.getByTestId("created-key").innerText()));
      ck("push create: no open-cell note", await page.getByTestId("created-open").count() === 0);
      ck("push create: no key error", await page.getByTestId("created-key-error").count() === 0);
      ck("push create: the test snippet carries the key", (await panel.innerText()).includes(`Bearer ${SECRET}`));
      await page.getByTestId("created-key").getByRole("button", { name: "Copy key" }).click();
      await page.getByTestId("created-key").getByRole("button", { name: "Copied" }).waitFor();
      ck("push create: Copy key copies the key", (await page.evaluate(() => navigator.clipboard.readText())) === SECRET);
      ck("push create: opens the connection", (await page.getByTestId("created-open-link").getAttribute("href")) === "/sources/deploys");
      await words(page, "created");
      await shot(page, "created-light-1440.png");
      await page.getByTestId("created-another").click();
      await ready(page);
      ck("push create: Add another returns to the picker", new URL(page.url()).search === "");
      await ctx.close();
    }

    // The key can't be made: the connection exists, so say so and where to make one.
    {
      const { ctx, page } = await open({ auth: true, keyFails: true });
      await ready(page);
      await create(page, "webhook", "deploys");
      await page.getByTestId("created-key-error").waitFor();
      const err = await flat(page.getByTestId("created-key-error"));
      ck("key fails: says the connection was added", /^The connection was added/.test(err), err);
      ck("key fails: carries the reason", err.includes("the key store is read-only"), err);
      ck("key fails: points to Settings", (await page.getByTestId("created-key-error").getByRole("link", { name: "Settings" }).count()) === 1);
      ck("key fails: no key box", await page.getByTestId("created-key").count() === 0);
      await ctx.close();
    }

    // An open cell: the address alone takes events.
    {
      const { ctx, page, calls } = await open();
      await ready(page);
      await create(page, "webhook", "deploys");
      await page.getByTestId("created-open").waitFor();
      ck("open cell: says no key is needed", /no key is needed/.test(await page.getByTestId("created-open").innerText()));
      ck("open cell: no key is made", !calls.some((c) => c.call === "POST /api/keys"));
      ck("open cell: no key box", await page.getByTestId("created-key").count() === 0);
      await words(page, "open cell");
      await ctx.close();
    }

    // The YAML box: added, nothing to add, refused.
    {
      const { ctx, page, calls } = await open({ imported: { sources: ["metrics"], triggers: ["metrics-watch"] } });
      await ready(page);
      const toggle = page.getByTestId("yaml-toggle");
      ck("yaml: closed at first", (await toggle.getAttribute("aria-expanded")) === "false" && await page.getByTestId("yaml-add").count() === 0);
      await toggle.click();
      ck("yaml: the toggle says it is open", (await toggle.getAttribute("aria-expanded")) === "true");
      ck("yaml: nothing to add while empty", await page.getByTestId("yaml-submit").isDisabled());
      await page.getByTestId("yaml-text").fill("sources:\n  - name: metrics\n    connector: prometheus\n");
      await page.getByTestId("yaml-submit").click();
      await page.getByTestId("yaml-added").waitFor();
      const imp = calls.find((c) => c.call === "POST /api/catalog/import");
      ck("yaml: always merges", imp?.body.mode === "merge", JSON.stringify(imp?.body));
      ck("yaml: names what was added in page words", (await flat(page.getByTestId("yaml-added"))) === "Added connection metrics, rule metrics-watch.",
         await flat(page.getByTestId("yaml-added")));
      ck("yaml: links each to its page",
         (await page.getByTestId("yaml-added").getByRole("link", { name: "metrics", exact: true }).getAttribute("href")) === "/sources/metrics" &&
         (await page.getByTestId("yaml-added").getByRole("link", { name: "metrics-watch" }).getAttribute("href")) === "/triggers/metrics-watch");
      ck("yaml: the box empties", (await page.getByTestId("yaml-text").inputValue()) === "");
      await words(page, "yaml");
      await shot(page, "yaml-light-1440.png");
      await toggle.click();
      ck("yaml: closes again", await page.getByTestId("yaml-add").count() === 0);
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ imported: {} });
      await ready(page);
      await page.getByTestId("yaml-toggle").click();
      await page.getByTestId("yaml-text").fill("agents: []\n");
      await page.getByTestId("yaml-submit").click();
      await page.getByTestId("yaml-error").waitFor();
      ck("yaml: nothing in it says so", /nothing to add/.test(await page.getByTestId("yaml-error").innerText()));
      await words(page, "yaml empty");
      await ctx.close();
    }
    {
      const { ctx, page } = await open({ imported: "line 2: connector 'prometheuss' is not known" });
      await ready(page);
      await page.getByTestId("yaml-toggle").click();
      await page.getByTestId("yaml-text").fill("sources:\n  - name: metrics\n    connector: prometheuss\n");
      await page.getByTestId("yaml-submit").click();
      await page.getByTestId("yaml-error").waitFor();
      const err = await flat(page.getByTestId("yaml-error"));
      ck("yaml: a refusal carries the reason", err.includes("prometheuss") && /^Tares did not add it/.test(err), err);
      ck("yaml: a refusal keeps the text to fix", (await page.getByTestId("yaml-text").inputValue()).includes("prometheuss"));
      await ctx.close();
    }

    // The kinds can't be listed.
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.route("**/api/connectors", (r) => r.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"registry offline"}' }));
      await page.goto(`${d.base}/sources/new`, { waitUntil: "load" });
      await page.getByTestId("kinds-error").waitFor();
      ck("no kinds: says so with the reason", (await page.getByTestId("kinds-error").innerText()).includes("registry offline"));
      await words(page, "no kinds");
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await ctx.close();
    }

    // Dark, and phone width.
    {
      const { ctx, page } = await open({ dark: true });
      await ready(page);
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      ck("dark: the page is dark", /rgb\((\d+), (\d+), (\d+)\)/.test(bg) && bg.match(/\d+/g).slice(0, 3).every((n) => Number(n) < 80), bg);
      await shot(page, "kinds-dark-1440.png");
      await ctx.close();
    }
    for (const dark of [false, true]) {
      const label = `390 ${dark ? "dark" : "light"}`;
      const { ctx, page } = await open({ width: 390, dark, auth: true });
      await ready(page);
      ck(`${label}: picker, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      await shot(page, `kinds-${dark ? "dark" : "light"}-390.png`);
      await kindButton(page, "loki").click();
      await page.getByTestId("connection-form").waitFor();
      ck(`${label}: form, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      if (!dark) await shot(page, "form-light-390.png");
      await page.getByTestId("pick-another").click();
      await ready(page);
      await create(page, "webhook", "deploys");
      await page.getByTestId("created-key").waitFor();
      ck(`${label}: created, no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      const box = await page.getByTestId("created-key-secret").boundingBox();
      ck(`${label}: the key stays on screen`, box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
      if (!dark) await shot(page, "created-light-390.png");
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
