/*
 * Browser e2e for TR-730 and TR-732: the factory home page, drawn from its samples and then live.
 *
 * Starts one scratch daemon with an empty factory cell (TARES_EDITION=factory) on TARES_E2E_PORT
 * (default 8833), MCP port +1, in a clean environment and a fresh data directory, and stops only it.
 * With the browser flag taresSamples=1 each sample (busy, empty, crewQuiet, nothingWaiting, manyWaiting,
 * longGoals, loading, error) is opened at 1440 and 390 wide and shot as factory-home-<sample>-<width>.png.
 * Checks: the rows and sections of each state, "15 more" expanding to all 20, the copy action and its
 * Toast, goals clamped to two lines with the full goal in the title, no horizontal scroll at 390, the
 * words check in every state, and that without the flag ?sample=busy shows no sample.
 * Run:  npm run test:factory-home     (builds first: the daemon serves ui/dist; tares installed in ../.venv or on PATH)
 * Live mode (TR-732) comes last: tests/seed-factory-home.py fills the same daemon through its API (calls to confirm,
 * permission requests, a spec not handed over, a project ready to close, one that is not, one finished), and the page
 * is opened without the sample flag. Checks: the one read call, the rows and links from the server, Copy, Keep (its
 * calls, the busy button, the row gone at once and for good), the Allow dialog (words, scope, Cancel, a refusal), the
 * Close dialog (confirm, a refusal), a failed read that keeps the old data under a warning, a first read that fails,
 * and the words check in each state. The daemon offers the Close row of Link counter itself; the script adds one
 * more to the answer of GET /api/home, for Retry uploads, which the daemon would not offer, to prove a refusal shows the
 * daemon's sentence. Closing itself is the daemon's real POST /api/projects/<id>/finish. Needs python3 for the seed
 * (TARES_E2E_PYTHON to choose another). Shots: factory-home-live-<state>-<width>.png.
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8833);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["busy", "empty", "crewQuiet", "nothingWaiting", "manyWaiting", "longGoals", "loading", "error"];
const READY = { loading: "factory-home-loading", error: "factory-home-error" };

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

/** The filled (primary) buttons and button links in the page, by their text: Home has one, on the
 *  top decision, and none when nothing waits (TR-1275). Read by colour, so any filled button counts,
 *  wherever it came from. */
async function filledButtons(page) {
  return page.evaluate(() => {
    const probe = document.createElement("i"); probe.className = "bg-primary"; document.body.appendChild(probe);
    const fill = getComputedStyle(probe).backgroundColor; probe.remove();
    return [...document.querySelectorAll("main a, main button")]
      .filter((e) => e.getBoundingClientRect().width > 0 && getComputedStyle(e).backgroundColor === fill)
      .map((e) => e.innerText.trim());
  });
}

async function waitFor(url, tries = 150) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

// A clean environment: the daemon gets only what it needs plus the variables this script sets.
function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-home-e2e-"));
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                         TARES_UI_DIST: dist, PYTHONPATH: REPO });   // the venv installs the main checkout; this runs the code under test
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    // A context with the sample flag set and a stand-in clipboard that records what is written to it.
    const flagged = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript(() => {
        try { window.localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ }
        window.__copied = [];
        Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } } });
      });
      return { ctx, page: await ctx.newPage() };
    };
    const open = async (page, sample) => {
      await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
      await page.getByTestId(READY[sample] || "factory-home").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const shot = async (page, sample, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-home-${sample}-${width}.png`), fullPage: true });
    };

    for (const width of [1440, 390]) {
      const { ctx, page } = await flagged(width);
      const rows = page.getByTestId("waiting-row");
      const tag = (s) => `${s} @${width}`;
      for (const sample of SAMPLES) {
        await open(page, sample);
        await shot(page, sample, width);
        await checkWords(page, ck, tag(sample));
        ck(`${tag(sample)}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        ck(`${tag(sample)}: nothing on the page is wider than the window`, await page.evaluate(() => {
          const w = document.documentElement.clientWidth;
          return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1);
        }));
        const hello = (await page.locator("h1").first().innerText()).trim();
        ck(`${tag(sample)}: a greeting by the time of day`, /^Good (morning|afternoon|evening)$/.test(hello), hello);
        ck(`${tag(sample)}: Talk to the crew and New project in the top bar`,
           (await page.getByRole("link", { name: "Talk to the crew" }).getAttribute("href")) === "/crew"
           && (await page.getByRole("link", { name: "New project" }).first().getAttribute("href")) === "/projects/new");
      }

      // busy
      await open(page, "busy");
      ck(`busy @${width}: 4 waiting rows`, (await rows.count()) === 4, String(await rows.count()));
      ck(`busy @${width}: no Projects section on Home (TR-1275)`, (await page.getByRole("region", { name: "Projects" }).count()) === 0
         && (await page.getByTestId("project-row").count()) === 0);
      const lead = await filledButtons(page);
      ck(`busy @${width}: one filled primary on the page (TR-1275)`, lead.length === 1, JSON.stringify(lead));
      ck(`busy @${width}: and it is the top row's main action`, (await rows.first().getByRole("link", { name: "Open Reviews" }).getAttribute("data-primary")) === "true"
         && lead[0] === "Open Reviews");
      ck(`busy @${width}: the other rows' main actions keep the dark outline`,
         (await rows.nth(1).getByRole("button", { name: "Close project" }).getAttribute("data-primary")) === null);
      ck(`busy @${width}: no "more" button with 4 waiting`, (await page.getByRole("button", { name: /\d+ more/ }).count()) === 0);
      ck(`busy @${width}: the caption says what comes first`, await page.getByText("what holds the crew up first").isVisible());
      ck(`busy @${width}: the headline is the data's`, (await page.getByTestId("home-headline").innerText()).startsWith("4 things wait for you."));
      const second = rows.nth(1).getByRole("button");
      ck(`busy @${width}: a row's outline button comes before its primary one`,
         (await rows.nth(1).locator("a, button").allInnerTexts()).join("|") === "Open project|Close project");
      ck(`busy @${width}: a link action is a link`, (await rows.nth(0).getByRole("link", { name: "Open Reviews" }).getAttribute("href")) === "/reviews");
      ck(`busy @${width}: a do action is a button`, (await second.count()) === 1 && (await second.innerText()) === "Close project");
      ck(`busy @${width}: the crew card lists 3 members with their memory`, (await page.getByTestId("crew-member").count()) === 3
         && (await page.getByText("memory 82% full").count()) === 1);
      ck(`busy @${width}: only the member at 80% or more has the amber memory bar`, (await page.getByTestId("memory-warn").count()) === 1
         && (await page.getByTestId("memory").count()) === 2);
      const fillOf = (loc) => loc.evaluate((e) => {
        const probe = document.createElement("i"); document.body.appendChild(probe);
        const c = (v) => { probe.style.backgroundColor = v; return getComputedStyle(probe).backgroundColor; };
        probe.className = "bg-foreground";   // the same utility an ink bar's fill uses
        const ink = getComputedStyle(probe).backgroundColor; probe.className = "";
        const out = { fill: getComputedStyle(e.querySelector(":scope > div")).backgroundColor, ink, amber: c("var(--kit-accent)") };
        probe.remove(); return out;
      });
      for (const theme of ["light", "dark"]) {
        await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
        await page.waitForTimeout(600);   // colours ease between themes
        const warn = await fillOf(page.getByTestId("memory-warn").first());
        const calm = await fillOf(page.getByTestId("memory").first());
        const plan = await fillOf(page.getByRole("progressbar", { name: "Current 5 hours" }));
        ck(`busy @${width} (${theme}): the memory bar at 80% or more is amber`, warn.fill === warn.amber, `${warn.fill} vs ${warn.amber}`);
        ck(`busy @${width} (${theme}): a calmer memory bar is ink`, calm.fill === calm.ink, `${calm.fill} vs ${calm.ink}`);
        ck(`busy @${width} (${theme}): a plan bar is ink`, plan.fill === plan.ink, `${plan.fill} vs ${plan.ink}`);
      }
      await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
      ck(`busy @${width}: Autonomy links to Settings`, (await page.getByRole("link", { name: "Autonomy" }).getAttribute("href")) === "/settings#crew");
      ck(`busy @${width}: the plan card shows both limits`, (await page.getByText("1% used · resets 21:50").count()) === 1
         && (await page.getByText("11% used · resets Fri").count()) === 1);
      ck(`busy @${width}: connected tools and the dashed Add a tool link`, (await page.getByText("GitHub").count()) === 1
         && (await page.getByRole("link", { name: "Add a tool" }).getAttribute("href")) === "/settings#tools");
      const copy = page.getByRole("button", { name: "Copy the hand-over command" });
      ck(`busy @${width}: the copy action is a button`, (await copy.count()) === 1);
      await copy.click();
      await page.getByText("Copied. Paste it in Claude Code to hand the project over.", { exact: true }).waitFor();
      ck(`busy @${width}: copying shows the Toast`, true);
      ck(`busy @${width}: and the text it copies is the action's`, JSON.stringify(await page.evaluate(() => window.__copied)) === JSON.stringify(["/tares:ship project-z"]));
      await checkWords(page, ck, `busy @${width} with the Toast`);

      // empty
      await open(page, "empty");
      ck(`empty @${width}: Nothing waits for you.`, await page.getByText("Nothing waits for you.", { exact: true }).isVisible());
      ck(`empty @${width}: no waiting rows`, (await rows.count()) === 0);
      ck(`empty @${width}: no filled primary when nothing waits (TR-1275)`, (await filledButtons(page)).length === 0, JSON.stringify(await filledButtons(page)));
      ck(`empty @${width}: plan hidden for one line`, await page.getByText("Plan use shows once the crew reports it.", { exact: true }).isVisible()
         && (await page.getByText(/% used/).count()) === 0);

      // crewQuiet
      await open(page, "crewQuiet");
      ck(`crewQuiet @${width}: the crew says it was not heard from`, (await page.getByText("Not heard from in 1 hour").count()) === 4, String(await page.getByText("Not heard from in 1 hour").count()));
      ck(`crewQuiet @${width}: members are in the not heard state`, (await page.getByRole("img", { name: "not heard" }).count()) === 3);

      // nothingWaiting
      await open(page, "nothingWaiting");
      ck(`nothingWaiting @${width}: Nothing waits for you.`, await page.getByText("Nothing waits for you.", { exact: true }).isVisible());
      ck(`nothingWaiting @${width}: and no filled primary`, (await filledButtons(page)).length === 0, JSON.stringify(await filledButtons(page)));

      // manyWaiting
      await open(page, "manyWaiting");
      ck(`manyWaiting @${width}: 5 rows`, (await rows.count()) === 5, String(await rows.count()));
      ck(`manyWaiting @${width}: a primary action listed first still comes last`,
         (await rows.first().locator("a, button").allInnerTexts()).join("|") === "Open project|Close project");
      const more = page.getByRole("button", { name: "15 more" });
      ck(`manyWaiting @${width}: a "15 more" button`, (await more.count()) === 1);
      await more.click();
      ck(`manyWaiting @${width}: 20 rows after it`, (await rows.count()) === 20, String(await rows.count()));
      ck(`manyWaiting @${width}: and the button is gone`, (await page.getByRole("button", { name: /\d+ more/ }).count()) === 0);
      ck(`manyWaiting @${width}: no horizontal scroll with all 20`, (await overflow(page)) <= 0);

      // longGoals
      await open(page, "longGoals");
      const detail = rows.first().locator("[title]").first();
      const dbox = await detail.evaluate((e) => ({ h: e.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(e).lineHeight), sh: e.scrollHeight }));
      ck(`longGoals @${width}: a waiting detail shows 3 lines at most`, dbox.h <= dbox.lh * 3 + 1 && dbox.sh > dbox.h, JSON.stringify(dbox));

      // loading, error
      await open(page, "loading");
      ck(`loading @${width}: placeholder rows, no real rows`, (await page.getByRole("status", { name: "Loading" }).count()) >= 3 && (await rows.count()) === 0);
      await open(page, "error");
      ck(`error @${width}: the banner says exactly what failed`,
         (await page.getByRole("alert").innerText()).replace(/\s+/g, " ").startsWith("Home did not load. The cell did not answer, so nothing here is current. Try again in a moment."));
      ck(`error @${width}: with a Retry button`, (await page.getByRole("button", { name: "Retry" }).count()) === 1);
      await ctx.close();
    }

    // ── live: the same daemon, seeded through its API, the page opened without the sample flag ──
    const seeded = spawnSync(process.env.TARES_E2E_PYTHON || "python3", [path.join(__dirname, "seed-factory-home.py"), String(PORT)],
                             { cwd: REPO, encoding: "utf8", env: cleanEnv({ PYTHONPATH: REPO }) });
    ck("seed ran", seeded.status === 0, (seeded.stderr || seeded.stdout || "").slice(-300));
    const station = "billing-export-build-t2-api"; // the seed's builder
    const ids = seeded.status === 0 ? JSON.parse(seeded.stdout.trim().split("\n").pop()) : {};
    const api = async (method, p, body) => {
      const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    };

    // A context without the sample flag, a stand-in clipboard, and GET /api/home answered by the daemon with one Close
    // row added: Retry uploads, which is not ready, so the daemon refuses it. `ctl.fail` makes the read fail, `ctl.hold` keeps it waiting.
    const liveContext = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript(() => {
        window.__copied = [];
        Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } } });
      });
      const page = await ctx.newPage();
      const ctl = { fail: false, hold: null, reads: 0, finishes: [], keeps: [], allows: [], extra: [] };
      await page.route("**/api/home", async (route) => {
        ctl.reads++;
        if (ctl.hold) await ctl.hold;
        if (ctl.fail) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) });
        const res = await route.fetch();
        const data = await res.json();
        const close = [];
        for (const name of ["Retry uploads"]) {
          const p = data.projects.find((x) => x.name === name);
          if (!p) continue;
          close.push({ id: `close:${p.id}`, kind: "signoff", label: "Ready to close", title: name, detail: "Every ticket of this project is finished.",
                       meta: "Waiting less than a minute", project: { id: p.id, name }, repo: null, from: null, since: new Date().toISOString(), waited: "just now",
                       actions: [{ label: "Open project", primary: false, href: `/projects/${p.id}` },
                                 { label: "Close project", primary: true, do: "close", project: p.id }] });
        }
        data.waiting_for_you = [...close, ...ctl.extra, ...data.waiting_for_you];
        await route.fulfill({ response: res, json: data });
      });
      page.on("request", (req) => {
        const u = new URL(req.url());
        if (req.method() !== "POST") return;
        if (/\/finish$/.test(u.pathname)) ctl.finishes.push(u.pathname);
        if (/\/assumptions\/[^/]+\/state$/.test(u.pathname)) ctl.keeps.push({ path: u.pathname, body: req.postDataJSON() });
        if (/\/api\/desk\/[^/]+\/allow$/.test(u.pathname)) ctl.allows.push({ path: u.pathname, body: req.postDataJSON() });
      });
      return { ctx, page, ctl };
    };
    const liveShot = async (page, name, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-home-live-${name}-${width}.png`), fullPage: true });
    };
    const reread = (page) => page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    const row = (page, text) => page.getByTestId("waiting-row").filter({ hasText: text });

    if (seeded.status === 0) {
      // the first read, held: placeholders, then the page; and exactly one read call
      {
        const { ctx, page, ctl } = await liveContext(1440);
        let release; ctl.hold = new Promise((r) => { release = r; });
        const oneRead = expectOneCall(page, "/api/home");
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home-loading").waitFor();
        ck("live: placeholders while the first read is out", (await page.getByTestId("waiting-row").count()) === 0
           && (await page.getByRole("status", { name: "Loading" }).count()) >= 3);
        await checkWords(page, ck, "live loading");
        release(); ctl.hold = null;
        await page.getByTestId("factory-home").waitFor();
        await oneRead(ck, "live");
        await ctx.close();
      }

      // the greeting names the person, not the key they signed in with (TR-832)
      for (const [label, person, want] of [["a person", { id: "p1", name: "Dana Roe", email: "dana@example.com" }, "Dana"],
                                           ["no person", null, "Ci"],
                                           ["the unnamed local person", { id: "local", name: "You" }, "Ci"]]) {
        const { ctx, page } = await liveContext(1440);
        await page.route("**/api/whoami", async (route) => {
          const res = await route.fetch();
          const w = await res.json();
          await route.fulfill({ response: res, json: { ...w, id: "key:k1", name: "ci-key", scopes: ["admin"], person } });
        });
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        const hello = (await page.locator("h1").first().innerText()).trim();
        ck(`greeting, ${label}: the first name is ${want}`, new RegExp(`^Good (morning|afternoon|evening), ${want}$`).test(hello), hello);
        await ctx.close();
      }

      // 390 first: it only looks, so the seeded state is whole for 1440, which acts on it
      for (const width of [390, 1440]) {
        const { ctx, page, ctl } = await liveContext(width);
        const rows = page.getByTestId("waiting-row");
        const t = (s) => `live @${width}: ${s}`;
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home").waitFor();
        await page.waitForLoadState("networkidle");
        await liveShot(page, "start", width);
        await checkWords(page, ck, t("page"));
        ck(t("no horizontal scroll"), (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        ck(t("the headline is the daemon's"), /^The crew is not running\. 6 things wait for you/.test(await page.getByTestId("home-headline").innerText()), await page.getByTestId("home-headline").innerText());
        // what holds the crew up (the three permissions) always shows; the rest shows its first 5 (TR-1021)
        const nowRows = await page.getByTestId("waiting-now").count() ? 3 : 0;
        const moreBtn = page.getByRole("button", { name: /^\d+ more$/ });
        ck(t("the three permissions under The crew is waiting on you, then the rest"), nowRows === 3
           && (await rows.count()) === 7 && (await moreBtn.count()) === 0, `${await rows.count()} rows, ${await moreBtn.count()} more`);
        if (await moreBtn.count()) await moreBtn.click();
        ck(t("7 waiting rows: two Close, a call, a spec, three permissions"), (await rows.count()) === 7, String(await rows.count()));
        ck(t("no Projects section (TR-1275)"), (await page.getByRole("region", { name: "Projects" }).count()) === 0);
        const lead = await filledButtons(page);
        ck(t("one filled primary, the top row's main action (TR-1275)"), lead.length === 1
           && (await rows.first().locator("[data-primary]").count()) === 1, JSON.stringify(lead));
        ck(t("a link action keeps the daemon's address"),
           (await row(page, "2 calls to confirm").getByRole("link", { name: "Read them" }).getAttribute("href")) === `/projects/${ids["Billing export"]}/calls`
           && (await row(page, "Docs search").getByRole("link", { name: "Read the spec" }).getAttribute("href")) === `/projects/${ids["Docs search"]}`
           && (await row(page, "reviewer").getByRole("link", { name: "Answer" }).getAttribute("href")) === `/crew#desk-${ids["allow-crew"]}`);
        ck(t("the permission names its command in a monospace span"), (await row(page, "npm run test").locator("code").innerText()) === "npm run test -- --watch=false");
        if (width === 390) { await ctx.close(); continue; }

        // Copy
        const copy = page.getByRole("button", { name: "Copy the hand-over command" });
        await copy.click();
        await page.getByText("Copied. Paste it in Claude Code to hand the project over.", { exact: true }).waitFor();
        ck(t("Copy: the toast, and the project's own command on the clipboard"), JSON.stringify(await page.evaluate(() => window.__copied)) === JSON.stringify(["/tares:ship Docs search"]));
        ck(t("Copy: the button says Copied"), (await row(page, "Docs search").getByRole("button", { name: "Copied" }).count()) === 1);
        await row(page, "Docs search").getByRole("button", { name: "Copy the hand-over command" }).waitFor({ timeout: 3500 });
        ck(t("Copy: and the label comes back after 2 seconds"), true);

        // Keep: a refusal first, then the real thing, slowed so the busy button can be seen
        const calls = row(page, "2 calls to confirm");
        await page.route("**/assumptions/*/state", (r) => r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "A1 is gone." }) }));
        await calls.getByRole("button", { name: "Keep both" }).click();
        await calls.getByTestId("row-error").waitFor();
        ck(t("Keep refused: the row says why and stays"), (await calls.getByTestId("row-error").innerText()) === "A1 is gone."
           && (await calls.getByRole("button", { name: "Keep both" }).isEnabled()));
        await page.unroute("**/assumptions/*/state");
        await page.route("**/assumptions/*/state", async (r) => { await new Promise((res) => setTimeout(res, 800)); await r.continue(); });
        ctl.keeps.length = 0;
        // let the daemon's 5 second answer expire, then read the page: that answer is new, so the read right after Keep
        // gets the old one and only the later read shows the change
        await page.waitForTimeout(5300);
        const fresh = ctl.reads;
        await reread(page);
        while (ctl.reads === fresh) await page.waitForTimeout(50);
        await page.waitForTimeout(300);
        const before = ctl.reads;
        await calls.getByRole("button", { name: "Keep both" }).click();
        ck(t("Keep: the button says Keeping... and is off"), (await calls.getByRole("button", { name: "Keeping..." }).isDisabled()));
        await calls.waitFor({ state: "detached" });
        ck(t("Keep: one call per call of the crew, kept"), ctl.keeps.length === 2 && ctl.keeps.every((k) => k.body.state === "kept")
           && ctl.keeps.map((k) => k.path).join() === `/api/projects/${ids["Billing export"]}/assumptions/A1/state,/api/projects/${ids["Billing export"]}/assumptions/A2/state`, JSON.stringify(ctl.keeps));
        await page.getByText("Kept both. The crew carries on.", { exact: true }).waitFor();
        ck(t("Keep: the home page is read again at once"), ctl.reads > before, `${ctl.reads} vs ${before}`);
        await page.unroute("**/assumptions/*/state");
        await page.waitForFunction(() => /^The crew is not running\. 5 things wait for you/.test(document.querySelector('[data-testid="home-headline"]')?.textContent || ""), null, { timeout: 9000 });
        ck(t("Keep: and again once the daemon's own answer has moved on, the row is still gone"), (await calls.count()) === 0 && (await api("GET", `/api/projects/${ids["Billing export"]}/assumptions?state=open`)).body.assumptions.length === 0);

        // Allow
        const dialog = page.getByTestId("allow-dialog");
        const builder = row(page, "npm run test");
        await builder.getByRole("button", { name: "Allow" }).click();
        await dialog.waitFor();
        const words = dialog.getByTestId("allow-words");
        ck(t("Allow: the box offers a grant in words, with the command and the project"),
           (await words.inputValue()) === `${station} may run \`npm run test -- --watch=false\` in Billing export`, await words.inputValue());
        ck(t("Allow: a builder's grant may hold in its project only"), (await dialog.getByRole("radio").count()) === 2 && (await dialog.getByRole("radio").first().isChecked()));
        await liveShot(page, "allow", width);
        await checkWords(page, ck, t("allow dialog"));
        await dialog.getByRole("radio").nth(1).check();
        ck(t("Allow: Every project rewrites the words"), (await words.inputValue()) === `${station} may run \`npm run test -- --watch=false\` in every project`);
        await words.fill((await words.inputValue()) + " (my words)");
        await dialog.getByRole("radio").first().check();
        ck(t("Allow: words the person edited stay"), (await words.inputValue()).endsWith("(my words)"));
        await dialog.getByRole("radio").nth(1).check();
        await dialog.getByRole("button", { name: "Cancel" }).click();
        await dialog.waitFor({ state: "detached" });
        ck(t("Allow: Cancel asks the daemon for nothing"), ctl.allows.length === 0 && (await builder.count()) === 1);
        await builder.getByRole("button", { name: "Allow" }).click();
        await dialog.waitFor();
        await dialog.getByRole("radio").nth(1).check();
        await words.fill("The builder may run `npm run test` in every project");
        await dialog.getByRole("button", { name: "Allow", exact: true }).click();
        await builder.waitFor({ state: "detached" });
        ck(t("Allow: the words and the scope go to the daemon"), ctl.allows.length === 1 && ctl.allows[0].path === `/api/desk/${ids["allow-builder"]}/allow`
           && ctl.allows[0].body.scope === "all" && ctl.allows[0].body.words === "The builder may run `npm run test` in every project", JSON.stringify(ctl.allows));
        await page.getByText("Allowed. The crew restarts it with the permission once it is between turns.", { exact: true }).waitFor();
        ck(t("Allow: the toast, the row gone, the request answered"), (await api("GET", "/api/desk?state=open")).body.items?.every?.((d) => d.label !== ids["allow-builder"]) ?? true);
        // a crew station serves every project: no choice of scope
        const crew = row(page, "gh pr view");
        await crew.getByRole("button", { name: "Allow" }).click();
        await dialog.waitFor();
        ck(t("Allow: a crew station's grant is for every project, with no choice"), (await dialog.getByRole("radio").count()) === 0
           && (await dialog.getByText("Kept for every project").count()) === 1 && (await words.inputValue()).startsWith("crew-reviewer may run `gh pr view 12 --json state`"));
        await dialog.getByRole("button", { name: "Cancel" }).click();
        await dialog.waitFor({ state: "detached" });
        // a refusal: the request was withdrawn while the box was open
        const withdrawn = row(page, "make seed");
        await withdrawn.getByRole("button", { name: "Allow" }).click();
        await dialog.waitFor();
        await api("POST", `/api/desk/${ids["allow-withdraw"]}/withdraw`, { reason: "not needed" });
        await dialog.getByRole("button", { name: "Allow", exact: true }).click();
        await dialog.getByTestId("dialog-error").waitFor();
        ck(t("Allow refused: the box stays open and says why"), /already withdrawn/.test(await dialog.getByTestId("dialog-error").innerText())
           && (await dialog.getByRole("button", { name: "Allow", exact: true }).isEnabled()), await dialog.getByTestId("dialog-error").innerText());
        await liveShot(page, "allow-refused", width);
        await dialog.getByRole("button", { name: "Cancel" }).click();
        await dialog.waitFor({ state: "detached" });

        // Close: a refusal (an open ticket), then Cancel, then the real one
        const closer = page.getByTestId("close-dialog");
        const retry = row(page, "Retry uploads");
        await retry.getByRole("button", { name: "Close project" }).click();
        await closer.waitFor();
        ck(t("Close: asks first, naming the project"), (await closer.getByText("Close Retry uploads?").count()) === 1 && (await closer.getByText("It moves to Done. Handing it to the crew again opens it.").count()) === 1);
        await liveShot(page, "close", width);
        await checkWords(page, ck, t("close dialog"));
        await closer.getByTestId("close-confirm").click();
        await closer.getByTestId("dialog-error").waitFor();
        ck(t("Close refused: the daemon's sentence in the box, the box open"), /still has open tickets: T2/.test(await closer.getByTestId("dialog-error").innerText()), await closer.getByTestId("dialog-error").innerText());
        await closer.getByRole("button", { name: "Cancel" }).click();
        await closer.waitFor({ state: "detached" });
        ck(t("Close refused: the row stays"), (await retry.count()) === 1);
        const link = row(page, "Link counter");
        await link.getByRole("button", { name: "Close project" }).click();
        await closer.waitFor();
        await closer.getByRole("button", { name: "Cancel" }).click();
        await closer.waitFor({ state: "detached" });
        const asked = ctl.finishes.length;
        await link.getByRole("button", { name: "Close project" }).click();
        await closer.waitFor();
        await closer.getByTestId("close-confirm").click();
        await link.waitFor({ state: "detached" });
        ck(t("Close: confirmed, one call to finish the project, and Cancel made none"), ctl.finishes.length === asked + 1 && ctl.finishes.at(-1) === `/api/projects/${ids["Link counter"]}/finish`, ctl.finishes.join());
        await page.getByText("Link counter is closed. It moved to Done.", { exact: true }).waitFor();
        ck(t("Close: the row leaves the list at once"), (await link.count()) === 0);
        await page.waitForTimeout(6500);
        ck(t("Close: and it stays gone after the daemon's own answer"), (await link.count()) === 0);
        await liveShot(page, "after", width);

        // a read that fails after data: the old data stays under a warning with the time of the last good read
        ctl.fail = true;
        await reread(page);
        const stale = page.getByTestId("stale-banner");
        await stale.waitFor();
        ck(t("stale: the warning names the time of the last good read"), /^Could not refresh\. Showing what we had at ([01]\d|2[0-3]):[0-5]\d\.$/.test((await stale.innerText()).trim()), await stale.innerText());
        ck(t("stale: and the page still shows what it had"), (await rows.count()) >= 3);
        await liveShot(page, "stale", width);
        await checkWords(page, ck, t("stale"));
        ctl.fail = false;
        await reread(page);
        await stale.waitFor({ state: "detached" });
        ck(t("stale: a good read takes the warning away"), true);
        await ctx.close();
      }

      // a first read that fails: the error with Retry, and Retry reads again
      {
        const { ctx, page, ctl } = await liveContext(1440);
        ctl.fail = true;
        await page.goto(`${BASE}/`, { waitUntil: "load" });
        await page.getByTestId("factory-home-error").waitFor();
        await liveShot(page, "error", 1440);
        await checkWords(page, ck, "live first read failed");
        ck("live: a first read that fails shows the error, no rows", (await page.getByRole("button", { name: "Retry" }).count()) === 1 && (await page.getByTestId("waiting-row").count()) === 0);
        ctl.fail = false;
        await page.getByRole("button", { name: "Retry" }).click();
        await page.getByTestId("factory-home").waitFor();
        ck("live: Retry reads again and shows the page", (await page.getByTestId("waiting-row").count()) >= 3);
        await ctx.close();
      }
    }

    // TR-1125: an option of a desk question stays inside its card and wraps, however long it is
    for (const width of [1440, 390]) {
      const t = (l) => `long option @${width}: ${l}`;
      const { ctx, page, ctl } = await liveContext(width);
      const sentence = "Keep the current upload path and add a second step that retries each failed part on its own before the whole upload is marked as failed and handed back to the person who started it";
      const unbroken = "keep-the-current-upload-path-and-add-a-second-step-that-retries-each-failed-part-on-its-own-before-the-whole-upload-is-marked-as-failed";
      ctl.extra = [{ id: "desk:long-option", kind: "question", label: "Question", title: "How should a failed upload behave?", detail: "The crew needs one choice.",
                     meta: "Waiting less than a minute", project: null, repo: null, from: null, since: new Date().toISOString(), waited: "just now", blocking: true,
                     question: { desk: "D9", options: [sentence, unbroken, "Stop"], recommendation: null, blocks: null },
                     actions: [{ label: "Answer", primary: true, href: "/" }] }];
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
      const card = page.getByTestId("waiting-row").filter({ hasText: "How should a failed upload behave?" });
      await card.getByTestId("question-choices").waitFor();
      const m = await card.evaluate((row) => {
        const box = row.getBoundingClientRect();
        return [...row.querySelectorAll("[data-testid=question-choices] button")].map((b) => {
          const r = b.getBoundingClientRect();
          return { inside: r.left >= box.left - 1 && r.right <= box.right + 1, clipped: b.scrollWidth - b.clientWidth, height: r.height };
        });
      });
      ck(t("three option buttons"), m.length === 3, JSON.stringify(m));
      ck(t("every option stays inside its card"), m.every((b) => b.inside), JSON.stringify(m));
      ck(t("no option's text runs past its button"), m.every((b) => b.clipped <= 1), JSON.stringify(m));
      ck(t("the long options wrap onto more lines"), m[0].height > 40 && m[1].height > 40, JSON.stringify(m));
      ck(t("a short option keeps the one-line height"), m[2].height === 32, JSON.stringify(m));
      const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck(t("no sideways scroll"), over <= 0, String(over));
      await liveShot(page, "long-option", width);
      await ctx.close();
    }

    // without the flag the address alone shows no sample, but the cell's own page
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.goto(`${BASE}/?sample=busy`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
      ck("no flag: ?sample=busy does not show the busy sample", (await page.getByText("4 PRs to review in project-x").count()) === 0
         && (await page.getByText("project-x").count()) === 0);
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  for (const l of []) console.log(l);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
