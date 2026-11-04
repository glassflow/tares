/*
 * Browser e2e for TR-765: the Start checklist on the AI SRE home page (/ on an AI SRE cell).
 *
 * The checklist is the `start` block of GET /api/home. A new cell shows five rows, none ticked, each with its
 * button: Add a provider, Connect, Try the demo service, Open services, Turn on watching, Choose a channel.
 * A connected cell ticks the rows its data answers (a model through ANTHROPIC_API_KEY, signals, services) and
 * leaves the others open. Hide this list removes it at once, calls the daemon, and it stays gone after a
 * reload. A failed Hide puts the list back with a line saying so. The sample states (?sample=<name>) show the
 * partial and demo variants, and the demo button does nothing on a sample. Every state shows only words from
 * words.json, makes one read call (GET /api/home), and does not scroll sideways at 390 px.
 *
 * Each live state seeds a scratch AI SRE cell (tests/seed-sre-start.py over tests/fixtures/sre_cell.py, invented
 * names), then runs the real daemon on it, serving the built console.
 * Run:  npm run build && node tests/sre-start.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px (default: a temporary directory).
 * TARES_E2E_PORT sets the daemon port (default 8877); the next port is its MCP endpoint.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8877);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-start-shots-"));
fs.mkdirSync(SHOTS, { recursive: true });

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

// The daemon's environment: the real one minus every key that could reach a model, Slack or another cell.
function cleanEnv(home, dist, extra = {}) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^(TARES|TARESD|ANTHROPIC|OPENAI)/.test(k) || /SLACK/.test(k)) continue;
    env[k] = v;
  }
  return {
    ...env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
    TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO,
    TARES_TRIGGER_DEBOUNCE_SECONDS: "0", TARES_SCHEDULE_CLOCK_SECONDS: "3600", TARES_WATCH_SECONDS: "3600", ...extra,
  };
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  const newPage = async (width, sampleFlag = false) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
    if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
    return { ctx, page: await ctx.newPage() };
  };
  const shot = async (page, n) => { await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
  const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const text = (loc) => loc.innerText().then((t) => t.replace(/\s+/g, " ").trim());
  const steps = (page) => page.getByTestId("start-step");
  const keysOf = (page) => steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-key")));
  const doneOf = (page) => steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-done") === "true"));
  const hrefOf = (row, name) => row.getByRole("link", { name }).getAttribute("href");

  async function common(page, state, width) {
    const t = await text(page.locator("body"));
    ck(`${state} ${width}: no trial card`, !/trial/i.test(t), t.match(/.{0,30}trial.{0,30}/i)?.[0]);
    await checkWords(page, ck, `${state} ${width}`);
    ck(`${state} ${width}: does not scroll sideways`, (await overflow(page)) <= 0, String(await overflow(page)));
  }

  // Seed a cell, run the daemon on it, run `body`, stop the daemon.
  async function live(state, extra, body) {
    console.log(`== ${state} ==`);
    const home = fs.mkdtempSync(path.join(os.tmpdir(), `tares-sre-start-e2e-${state}-`));
    execFileSync(python, [path.join(__dirname, "seed-sre-start.py"), home, state], { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } });
    const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env: cleanEnv(home, dist, extra) });
    try {
      ck(`${state}: daemon up`, await waitFor(`${BASE}/health`));
      await body();
    } finally {
      daemon.kill();
      await new Promise((r) => daemon.once("exit", r));
      fs.rmSync(home, { recursive: true, force: true });
    }
  }

  const openLive = async (page) => {
    const one = expectOneCall(page, "/api/home");
    await page.goto(`${BASE}/`, { waitUntil: "load" });
    await page.getByTestId("sre-home").waitFor();
    return one;
  };

  // ── a new cell: five rows, none ticked ───────────────────────────────────
  await live("new", {}, async () => {
    {
      const { ctx, page } = await newPage(390);
      const one = await openLive(page);
      await page.getByTestId("start-checklist").waitFor();
      await one(ck, "new @390");
      await shot(page, "sre-start-new-390.png");
      await common(page, "new", 390);
      await ctx.close();
    }
    const { ctx, page } = await newPage(1440);
    const posts = [];
    page.on("request", (r) => { if (new URL(r.url()).pathname === "/api/home/start/hide") posts.push(r.method()); });
    const one = await openLive(page);
    await page.getByTestId("start-checklist").waitFor();
    await one(ck, "new @1440");
    await shot(page, "sre-start-new-1440.png");
    const list = page.getByTestId("start-checklist");
    ck("new: the title is Get started", (await list.getByRole("heading", { name: "Get started" }).count()) === 1);
    ck("new: 0 of 5 done", (await text(page.getByTestId("start-count"))) === "0 of 5 done");
    ck("new: the five rows in order", (await keysOf(page)).join() === "model,signals,services,watching,slack", (await keysOf(page)).join());
    ck("new: none is ticked", (await doneOf(page)).every((d) => !d));
    ck("new: the checklist comes before the headline", await page.evaluate(() => {
      const a = document.querySelector("[data-testid=start-checklist]"), b = document.querySelector("[data-testid=home-headline]");
      return !!(a && b && a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    }));
    const row = (i) => steps(page).nth(i);
    ck("new: Add a provider goes to the advanced settings", (await hrefOf(row(0), "Add a provider")) === "/settings/advanced");
    ck("new: Connect goes to the tools", (await hrefOf(row(1), "Connect")) === "/settings#tools");
    ck("new: Try the demo service is a button, not a link", (await row(1).getByRole("button", { name: "Try the demo service" }).count()) === 1
       && (await row(1).getByRole("link", { name: "Try the demo service" }).count()) === 0);
    ck("new: Open services goes to the services", (await hrefOf(row(2), "Open services")) === "/services");
    ck("new: Turn on watching goes to the watching settings", (await hrefOf(row(3), "Turn on watching")) === "/settings#watching");
    ck("new: Choose a channel goes to the notes settings", (await hrefOf(row(4), "Choose a channel")) === "/settings#notes");
    await common(page, "new", 1440);

    // Hide that fails: the list comes back with a line saying why
    await page.route("**/api/home/start/hide", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }));
    await list.getByRole("button", { name: "Hide this list" }).click();
    await page.getByTestId("start-error").waitFor();
    ck("new: a refused Hide puts the list back", (await steps(page).count()) === 5);
    ck("new: and says so in plain words", (await text(page.getByTestId("start-error"))) === "Could not hide the list. Try again in a moment.");
    await page.unroute("**/api/home/start/hide");

    // Hide: gone at once, the daemon is called, and it stays gone after a reload
    posts.length = 0;
    await list.getByRole("button", { name: "Hide this list" }).click();
    await page.getByTestId("start-checklist").waitFor({ state: "detached" });
    ck("new: Hide removes the list at once", (await page.getByTestId("start-checklist").count()) === 0);
    ck("new: and calls the daemon once", posts.join() === "POST", posts.join());
    ck("new: the page itself is still there", (await page.getByTestId("home-headline").count()) === 1);
    await shot(page, "sre-start-hidden-1440.png");
    await page.reload({ waitUntil: "load" });
    await page.getByTestId("home-headline").waitFor();
    await page.waitForLoadState("networkidle");
    ck("new: after a reload it is still gone", (await page.getByTestId("start-checklist").count()) === 0);

    // the way back: a quiet line while the list is hidden and has open steps
    const hiddenLine = page.getByTestId("start-hidden");
    ck("new: a line says the list is hidden", (await hiddenLine.count()) === 1 && (await text(hiddenLine)).startsWith("The list \u201cGet started\u201d is hidden."),
       await hiddenLine.count() ? await text(hiddenLine) : "no line");
    ck("new: with one button named Show it again", (await hiddenLine.getByRole("button", { name: "Show it again" }).count()) === 1);
    await shot(page, "sre-start-hidden-line-1440.png");
    await checkWords(page, ck, "new hidden line");
    // a Show it again the cell refuses keeps the line and says so
    await page.route("**/api/home/start/hide", (r) => r.request().method() === "DELETE"
      ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }) : r.fallback());
    await hiddenLine.getByRole("button", { name: "Show it again" }).click();
    await page.getByTestId("start-show-error").waitFor();
    ck("new: a refused Show it again keeps the line", (await hiddenLine.count()) === 1 && (await page.getByTestId("start-checklist").count()) === 0);
    ck("new: and says so in plain words", (await text(page.getByTestId("start-show-error"))) === "Could not show the list. Try again in a moment.",
       await text(page.getByTestId("start-show-error")));
    await page.unroute("**/api/home/start/hide");
    // Show it again for real: one DELETE, the list is back with its five steps, the line is gone
    posts.length = 0;
    await hiddenLine.getByRole("button", { name: "Show it again" }).click();
    // short: the 30 s poll would bring the list back even if Show it again did not reload
    await page.getByTestId("start-checklist").waitFor({ timeout: 4000 });
    ck("new: Show it again calls the daemon once with DELETE", posts.join() === "DELETE", posts.join());
    ck("new: the list is back with its five steps", (await steps(page).count()) === 5);
    ck("new: and the line is gone", (await page.getByTestId("start-hidden").count()) === 0);
    await page.reload({ waitUntil: "load" });
    await page.getByTestId("start-checklist").waitFor();
    ck("new: after a reload the list is still shown", (await steps(page).count()) === 5);
    await ctx.close();
  });

  // ── a connected cell: the rows its data answers are ticked ───────────────
  await live("connected", { ANTHROPIC_API_KEY: "sk-ant-test-not-a-real-key" }, async () => {
    for (const width of [390, 1440]) {
      const { ctx, page } = await newPage(width);
      const one = await openLive(page);
      await page.getByTestId("start-checklist").waitFor();
      await one(ck, `connected @${width}`);
      await shot(page, `sre-start-connected-${width}.png`);
      ck(`connected @${width}: 3 of 5 done`, (await text(page.getByTestId("start-count"))) === "3 of 5 done");
      ck(`connected @${width}: model, signals and services are ticked`, (await doneOf(page)).join() === "true,true,true,false,false", (await doneOf(page)).join());
      if (width === 1440) {
        ck("connected: a done row has no button", (await steps(page).nth(0).locator("a, button").count()) === 0
           && (await steps(page).nth(1).locator("a, button").count()) === 0 && (await steps(page).nth(2).locator("a, button").count()) === 0);
        ck("connected: the open rows keep theirs", (await steps(page).nth(3).locator("a, button").count()) === 1
           && (await steps(page).nth(4).locator("a, button").count()) === 1);
      }
      await common(page, "connected", width);
      await ctx.close();
    }
  });

  // ── Try the demo service on a live cell (TR-831) ──────────────────────────
  await live("new", {}, async () => {
    const { ctx, page } = await newPage(1440);
    const creates = [];
    page.on("request", (r) => { if (r.method() === "POST" && new URL(r.url()).pathname === "/api/projects") creates.push(r.url()); });
    await openLive(page);
    await page.getByTestId("start-checklist").waitFor();
    const demoBtn = () => page.getByTestId("start-checklist").getByRole("button", { name: "Try the demo service" });

    // A refused create: the notice names the symptom and links to the steps; nothing is made
    await page.route("**/api/projects", (r) => (r.request().method() === "POST"
      ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }) : r.continue()));
    await demoBtn().click();
    await page.getByTestId("start-notice").waitFor();
    ck("demo: a refused create says so in plain words", (await text(page.getByTestId("start-notice"))) === "Could not start the demo. Try again in a moment, or set it up step by step.",
       await text(page.getByTestId("start-notice")));
    ck("demo: and the notice links to the steps", (await page.getByTestId("start-notice-link").getAttribute("href")) === "/projects/new/ai_sre_demo");
    ck("demo: the button works again after a refusal", (await demoBtn().isEnabled()));
    await checkWords(page, ck, "demo notice");
    await page.unroute("**/api/projects");

    // A create that goes through: the sixth row shows without waiting for the cell's cache
    creates.length = 0;
    const started = Date.now();
    await demoBtn().click();
    const shown = await page.locator("[data-testid=start-step][data-key=test_incident]").waitFor({ timeout: 4000 }).then(() => true, () => false);
    const took = Date.now() - started;
    ck("demo: the demo row shows up", shown);
    ck("demo: the click makes one project", creates.length === 1, String(creates.length));
    ck("demo: the home shows the demo row within 4 seconds (not after the cache wait)", took < 4000, `${took} ms`);
    ck("demo: six rows, the signals row ticked", (await steps(page).count()) === 6 && (await doneOf(page))[1] === true, (await keysOf(page)).join());
    ck("demo: the notice is gone", (await page.getByTestId("start-notice").count()) === 0);
    await ctx.close();
  });

  // ── the sample states (they need no data; the daemon only serves the console) ──
  await live("new", {}, async () => {
    for (const name of ["start-partial", "start-demo", "new-cell"]) {
      for (const width of [1440, 390]) {
        const { ctx, page } = await newPage(width, true);
        await page.goto(`${BASE}/?sample=${name}`, { waitUntil: "load" });
        await page.getByTestId("sre-home").waitFor();
        await page.getByTestId("start-checklist").waitFor();
        await page.waitForLoadState("networkidle");
        await shot(page, `sre-start-sample-${name}-${width}.png`);
        const done = (await doneOf(page)).filter(Boolean).length;
        if (name === "start-partial") ck(`${name} @${width}: some rows ticked, some open`, done > 0 && done < (await steps(page).count()), String(done));
        if (name === "start-demo") {
          ck(`${name} @${width}: the test incident row is there`, (await keysOf(page)).includes("test_incident"), (await keysOf(page)).join());
          if (width === 1440) {
            const calls = [];
            page.on("request", (r) => { if (r.method() !== "GET") calls.push(r.url()); });
            const open = page.getByTestId("start-do");
            if (await open.count()) {
              await open.first().click();
              await page.waitForTimeout(400);
              ck(`${name}: the demo button does nothing on a sample`, calls.length === 0, calls.join(", "));
            }
          }
        }
        if (name === "new-cell") ck(`${name} @${width}: 0 of 5 done`, (await text(page.getByTestId("start-count"))) === "0 of 5 done");
        await common(page, `sample ${name}`, width);
        await ctx.close();
      }
    }
  });

  await browser.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(`screenshots: ${SHOTS}`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
