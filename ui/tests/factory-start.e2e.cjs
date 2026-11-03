/*
 * Browser e2e for TR-756: the Start checklist on the factory home.
 *
 * Seeds a scratch factory cell with tests/seed-factory-start.py (GitHub connected, one access key that has been
 * used: steps 1 and 2 done, 3 to 5 open), then starts one daemon on it with a root token, on TARES_E2E_PORT
 * (default 8875), MCP port +1, in a clean environment and a fresh data directory, and stops only it.
 * Samples (browser flag taresSamples=1): startNothing, startThree, startLong, startDone, loading and error at
 * 1440 and 390 wide. Checks: five steps with what each gives, the count and the bar, done steps in place without
 * buttons, copy actions as a command with Copy, the long detail clamped to three lines with the full text in its
 * title, no list once everything is done or while the home is in error, the loading rows, no horizontal scroll,
 * the words check in every state.
 * Live: the page opened with the seeded key, one read call to /api/home, rows 1 and 2 ticked and 3 to 5 open,
 * Copy copies the command and reads Copied for two seconds, a Hide that fails puts the list back with a line
 * saying so, Hide removes the list at once and calls the daemon, the list stays gone after a reload, another key
 * still sees it, and showing it again brings it back. Shots: factory-start-<state>-<width>.png.
 * Run:  npm run test:factory-start     (builds first: the daemon serves ui/dist; tares in ../.venv or on PATH)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8875);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROOT = "root-token-start-e2e";
const KEYS = ["claude_code", "github", "tickets", "crew", "spec"];
const GETS = {
  claude_code: "Your Claude Code sessions and the crew report here.",
  github: "The crew opens pull requests and Tares follows their reviews and checks.",
  tickets: "Tickets stay where your team already tracks work.",
  crew: "Claude Code sessions that build, review and merge your tickets.",
  spec: "A plan with milestones and tickets the crew can build.",
};
const TITLES = {
  claude_code: "Sign in from Claude Code", github: "Connect GitHub", tickets: "Connect Linear or GitHub Issues",
  crew: "Start the crew", spec: "Write your first spec",
};
const READY = { loading: "factory-home-loading", error: "factory-home-error" };

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-start-e2e-"));
  const python = process.env.TARES_E2E_PYTHON || "python3";
  const base = { TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist,
                 TARES_OTLP_GRPC_PORT: "off", TARES_EDITION: "factory", PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test

  // the seed: GitHub connected, one key (laptop) that has been used
  const seeded = spawnSync(python, [path.join(__dirname, "seed-factory-start.py"), home, String(PORT), String(PORT + 1), dist, tares],
                           { cwd: REPO, encoding: "utf8", env: cleanEnv(base) });
  ck("seed ran", seeded.status === 0, (seeded.stderr || seeded.stdout || "").slice(-300));
  if (seeded.status !== 0) { console.log(`\n${pass} passed, ${fail} failed`); process.exit(1); }
  const seed = JSON.parse(seeded.stdout.trim().split("\n").pop());

  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home],
                       { cwd: REPO, stdio: "ignore", env: cleanEnv({ ...base, TARES_AUTH_TOKEN: ROOT }), detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    const api = async (method, p, token, body) => {
      const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
                                        body: body ? JSON.stringify(body) : undefined });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    };
    // a second person: another key, made with the root token
    const other = await api("POST", "/api/keys", ROOT, { name: "desktop", scopes: ["read"] });
    ck("a second key was made", other.status < 300 && !!other.body.secret, JSON.stringify(other.body));

    // A browser context signed in with a key, a stand-in clipboard that records what is written to it,
    // and the sample flag when asked for.
    const context = async (width, { token = seed.key_secret, samples = false } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript(([t, flag]) => {
        try {
          window.localStorage.setItem("tares_token", t);
          if (flag) window.localStorage.setItem("taresSamples", "1");
        } catch { /* blocked */ }
        window.__copied = [];
        Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (x) => { window.__copied.push(x); return Promise.resolve(); } } });
      }, [token, samples]);
      const page = await ctx.newPage();
      const posts = [];
      page.on("request", (req) => {
        if (new URL(req.url()).pathname === "/api/home/start/hide") posts.push(req.method());
      });
      return { ctx, page, posts };
    };
    const openSample = async (page, sample) => {
      await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
      await page.getByTestId(READY[sample] || "factory-home").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const shot = async (page, name, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-start-${name}-${width}.png`), fullPage: true });
    };
    const steps = (page) => page.getByTestId("start-step");
    const keysOf = (page) => steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-key")));
    const doneOf = (page) => steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-done") === "true"));
    const tickNames = (page) => steps(page).evaluateAll((els) => els.map((e) => e.querySelector("[data-testid=start-tick]").getAttribute("aria-label")));
    const noOverflow = async (page, label) => {
      ck(`${label}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      ck(`${label}: nothing on the page is wider than the window`, await page.evaluate(() => {
        const w = document.documentElement.clientWidth;
        return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1);
      }));
    };
    const list = (page) => page.getByTestId("start-checklist");
    const panelOk = async (page, label, { done, total = 5 }) => {
      ck(`${label}: the list is a section named for what it is`, (await page.getByRole("region", { name: "Get your factory going" }).count()) === 1);
      ck(`${label}: ${done} of ${total} done`, (await page.getByTestId("start-count").innerText()).trim() === `${done} of ${total} done`);
      const bar = await list(page).getByRole("progressbar").first().getAttribute("aria-valuenow");
      ck(`${label}: the bar shows ${done / total * 100} percent`, Number(bar) === done / total * 100, String(bar));
      ck(`${label}: five steps in a list, in order`, (await keysOf(page)).join() === KEYS.join());
      ck(`${label}: the steps are list items`, (await list(page).getByRole("listitem").count()) === 5);
      for (const k of KEYS) {
        const row = steps(page).nth(KEYS.indexOf(k));
        const text = await row.innerText();
        ck(`${label}: ${k} has its title and what it gives`, text.includes(TITLES[k]) && text.includes(GETS[k]), text);
      }
      ck(`${label}: a Hide this list button`, (await list(page).getByRole("button", { name: "Hide this list" }).count()) === 1);
    };

    for (const width of [1440, 390]) {
      const { ctx, page } = await context(width, { samples: true });
      const tag = (s) => `${s} @${width}`;

      // nothing done
      await openSample(page, "startNothing");
      await shot(page, "nothing-done", width);
      await panelOk(page, tag("startNothing"), { done: 0 });
      ck(`${tag("startNothing")}: no step is ticked`, (await doneOf(page)).every((d) => !d) && (await tickNames(page)).every((n) => n === "Not done"), (await tickNames(page)).join());
      ck(`${tag("startNothing")}: three commands to copy, each with a Copy button`,
         (await list(page).getByTestId("start-copy").count()) === 3
         && (await list(page).locator("code").allInnerTexts()).join("|") === 'tares login|tares factory crew up|tares factory spec "what you want to build"');
      ck(`${tag("startNothing")}: Connect GitHub is a link inside the console`,
         (await steps(page).nth(1).getByRole("link", { name: "Connect GitHub" }).getAttribute("href")) === "/settings#tools");
      ck(`${tag("startNothing")}: the tickets step has Connect Linear and Use GitHub Issues`,
         (await steps(page).nth(2).locator("a").allInnerTexts()).join("|") === "Connect Linear|Use GitHub Issues"
         && /^https:\/\/docs\.glassflow\.ai\//.test(await steps(page).nth(2).getByRole("link", { name: "Use GitHub Issues" }).getAttribute("href")));
      ck(`${tag("startNothing")}: the list sits above Waiting for you`, await page.evaluate(() => {
        const a = document.querySelector("[data-testid=start-checklist]").getBoundingClientRect();
        const b = [...document.querySelectorAll("section")].find((s) => s.getAttribute("aria-label") === "Waiting for you").getBoundingClientRect();
        return a.bottom <= b.top;
      }));
      await checkWords(page, ck, tag("startNothing"));
      await noOverflow(page, tag("startNothing"));

      // three of five
      await openSample(page, "startThree");
      await shot(page, "three-of-five", width);
      if (width === 390) await shot(page, "phone", width);
      await panelOk(page, tag("startThree"), { done: 3 });
      ck(`${tag("startThree")}: the first three are ticked and stay in place`,
         (await doneOf(page)).join() === "true,true,true,false,false" && (await tickNames(page)).join() === "Done,Done,Done,Not done,Not done");
      ck(`${tag("startThree")}: done steps have no button and no link`,
         (await steps(page).nth(0).locator("a, button").count()) === 0 && (await steps(page).nth(1).locator("a, button").count()) === 0
         && (await steps(page).nth(2).locator("a, button").count()) === 0);
      ck(`${tag("startThree")}: open steps keep their buttons`,
         (await steps(page).nth(3).getByTestId("start-copy").count()) === 1 && (await steps(page).nth(4).getByTestId("start-copy").count()) === 1);
      const titleWeight = async (i) => steps(page).nth(i).locator("div.font-semibold, div.font-bold").first().evaluate((e) => getComputedStyle(e).color);
      ck(`${tag("startThree")}: a done title is muted`, (await titleWeight(0)) !== (await titleWeight(3)), `${await titleWeight(0)} vs ${await titleWeight(3)}`);
      const copyBox = steps(page).nth(4).getByTestId("start-copy");
      ck(`${tag("startThree")}: the command box stays inside the window`, await copyBox.evaluate((e) => e.getBoundingClientRect().right <= document.documentElement.clientWidth));
      await checkWords(page, ck, tag("startThree"));
      await noOverflow(page, tag("startThree"));

      // copy in a sample: the button says Copied, then Copy again
      await steps(page).nth(3).getByTestId("start-copy").click();
      await page.waitForFunction(() => window.__copied.length === 1);
      ck(`${tag("startThree")}: Copy puts the command on the clipboard`, (await page.evaluate(() => window.__copied[0])) === "tares factory crew up");
      ck(`${tag("startThree")}: the button now says Copied`, (await steps(page).nth(3).getByTestId("start-copy").innerText()).trim() === "Copied");
      await page.waitForTimeout(1500);
      ck(`${tag("startThree")}: it still says Copied after 1.5 seconds`, (await steps(page).nth(3).getByTestId("start-copy").innerText()).trim() === "Copied");
      await page.waitForFunction(() => document.querySelector("[data-key=crew] [data-testid=start-copy]").textContent.trim() === "Copy", null, { timeout: 2500 });
      ck(`${tag("startThree")}: and says Copy again after two seconds`, true);

      // long text
      await openSample(page, "startLong");
      await shot(page, "long-text", width);
      const detail = steps(page).nth(1).locator("[title]").first();
      const full = await detail.getAttribute("title");
      ck(`${tag("startLong")}: the full detail is in the title`, !!full && full.length > 300 && full.startsWith("Connected as example-org."), String(full && full.length));
      const clamp = await detail.evaluate((e) => {
        const cs = getComputedStyle(e);
        const line = parseFloat(cs.lineHeight);
        return { lines: Math.round(e.getBoundingClientRect().height / line), cut: e.scrollHeight > e.clientHeight, clamp: cs.webkitLineClamp };
      });
      ck(`${tag("startLong")}: the detail is cut at 3 lines`, clamp.lines === 3 && clamp.cut && clamp.clamp === "3", JSON.stringify(clamp));
      await checkWords(page, ck, tag("startLong"));
      await noOverflow(page, tag("startLong"));

      // all done: no list
      await openSample(page, "startDone");
      ck(`${tag("startDone")}: no list once everything is done`, (await list(page).count()) === 0 && (await steps(page).count()) === 0
         && (await page.getByText("Get your factory going").count()) === 0);
      await checkWords(page, ck, tag("startDone"));

      // a sample that has no start block shows none
      await openSample(page, "busy");
      ck(`${tag("busy")}: a home without a start block shows no list`, (await list(page).count()) === 0);

      // loading
      await openSample(page, "loading");
      await shot(page, "loading", width);
      const loading = page.getByTestId("start-checklist-loading");
      ck(`${tag("loading")}: the loading list says Checking on each row`, (await loading.count()) === 1
         && (await loading.getByRole("listitem").count()) === 3
         && (await loading.getByRole("listitem").evaluateAll((els) => els.every((e) => e.innerText.trim() === "Checking"))));
      ck(`${tag("loading")}: it has the same title and no Hide button`, (await loading.getByRole("heading", { name: "Get your factory going" }).count()) === 1
         && (await loading.getByRole("button").count()) === 0);
      await checkWords(page, ck, tag("loading"));
      await noOverflow(page, tag("loading"));

      // error: the home's own error covers the page
      await openSample(page, "error");
      await shot(page, "error", width);
      ck(`${tag("error")}: the home's error shows and the list adds none`, (await page.getByTestId("factory-home-error").count()) === 1
         && (await list(page).count()) === 0 && (await page.getByTestId("start-checklist-loading").count()) === 0
         && (await page.getByText("Get your factory going").count()) === 0);
      await checkWords(page, ck, tag("error"));
      await noOverflow(page, tag("error"));
      await ctx.close();
    }

    // ── live: the seeded cell, the page opened with the seeded key and no sample flag ──
    const liveOpen = async (page) => {
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const mine = await api("GET", "/api/home", seed.key_secret);
    ck("live: the daemon says 2 of 5 done, shown", mine.body.start && mine.body.start.done === 2 && mine.body.start.show === true,
       JSON.stringify(mine.body.start && { done: mine.body.start.done, show: mine.body.start.show }));

    // 390 first: it only looks, so the cell is whole for 1440, which hides the list
    {
      const { ctx, page } = await context(390);
      const oneRead = expectOneCall(page, "/api/home");
      await liveOpen(page);
      await oneRead(ck, "live @390");
      await shot(page, "live-two-of-five", 390);
      await panelOk(page, "live @390", { done: 2 });
      ck("live @390: rows 1 and 2 ticked, 3 to 5 open", (await doneOf(page)).join() === "true,true,false,false,false"
         && (await tickNames(page)).join() === "Done,Done,Not done,Not done,Not done", (await tickNames(page)).join());
      await checkWords(page, ck, "live @390");
      await noOverflow(page, "live @390");
      await ctx.close();
    }

    {
      const { ctx, page, posts } = await context(1440);
      const oneRead = expectOneCall(page, "/api/home");
      await liveOpen(page);
      await oneRead(ck, "live @1440");
      await shot(page, "live-two-of-five", 1440);
      await panelOk(page, "live @1440", { done: 2 });
      ck("live @1440: rows 1 and 2 ticked, 3 to 5 open", (await doneOf(page)).join() === "true,true,false,false,false"
         && (await tickNames(page)).join() === "Done,Done,Not done,Not done,Not done", (await tickNames(page)).join());
      ck("live @1440: the done rows have no button", (await steps(page).nth(0).locator("a, button").count()) === 0
         && (await steps(page).nth(1).locator("a, button").count()) === 0);
      ck("live @1440: the tickets row has Connect Linear as a link to Settings", await (async () => {
        const row = steps(page).nth(2);
        return (await row.getByRole("link", { name: "Connect Linear" }).getAttribute("href")) === "/settings#tools";
      })());
      await checkWords(page, ck, "live @1440");

      // Copy
      const crew = steps(page).nth(3);
      await crew.getByTestId("start-copy").click();
      await page.waitForFunction(() => window.__copied.length === 1);
      ck("live: Copy copies the command", (await page.evaluate(() => window.__copied[0])) === "tares factory crew up");
      ck("live: and the button says Copied", (await crew.getByTestId("start-copy").innerText()).trim() === "Copied");
      await page.waitForFunction(() => document.querySelector("[data-key=crew] [data-testid=start-copy]").textContent.trim() === "Copy", null, { timeout: 2800 });
      ck("live: then Copy again", true);
      ck("live: a second copy of the same box works again", await (async () => {
        await crew.getByTestId("start-copy").click();
        await page.waitForFunction(() => window.__copied.length === 2);
        return (await crew.getByTestId("start-copy").innerText()).trim() === "Copied";
      })());

      // Hide that fails: the list comes back with a line saying why
      await page.route("**/api/home/start/hide", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }));
      await list(page).getByRole("button", { name: "Hide this list" }).click();
      await page.getByTestId("start-error").waitFor();
      ck("live: a refused Hide puts the list back", (await list(page).count()) === 1 && (await steps(page).count()) === 5);
      const msg = (await page.getByTestId("start-error").innerText()).trim();
      ck("live: and says so in plain words", msg === "Could not hide the list. Try again in a moment.", msg);
      ck("live: the failure line has role alert", (await page.getByRole("alert").filter({ hasText: "Could not hide the list" }).count()) === 1);
      await shot(page, "hide-failed", 1440);
      await checkWords(page, ck, "live hide failed");
      await page.unroute("**/api/home/start/hide");
      ck("live: the cell still has the list shown", (await api("GET", "/api/home", seed.key_secret)).body.start.show === true);

      // Hide for real: the list goes at once, the daemon is called, and it stays gone
      posts.length = 0;
      let letGo; const held = new Promise((res) => { letGo = res; });
      await page.route("**/api/home/start/hide", async (r) => { await held; await r.continue(); });
      await list(page).getByRole("button", { name: "Hide this list" }).click();
      ck("live: Hide removes the list at once, before the cell answers", (await list(page).count()) === 0);
      letGo();
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300);
      ck("live: Hide called the cell once", posts.join() === "POST", posts.join());
      ck("live: no failure line after a good Hide", (await page.getByTestId("start-error").count()) === 0);
      await page.unroute("**/api/home/start/hide");
      ck("live: the cell says the list is hidden for this key", await (async () => {
        const s = (await api("GET", "/api/home", seed.key_secret)).body.start;
        return s.show === false && s.done === 2;
      })());
      await shot(page, "live-hidden", 1440);
      await checkWords(page, ck, "live hidden");
      const oneAgain = expectOneCall(page, "/api/home");
      await page.reload({ waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await oneAgain(ck, "live after reload");
      ck("live: after a reload the list is still gone", (await list(page).count()) === 0 && (await steps(page).count()) === 0
         && (await page.getByRole("region", { name: "Get your factory going" }).count()) === 0);
      await ctx.close();
    }

    // another key still sees it
    {
      const { ctx, page } = await context(1440, { token: other.body.secret });
      await liveOpen(page);
      ck("another key: still sees the list", (await list(page).count()) === 1 && (await steps(page).count()) === 5);
      ck("another key: with 2 of 5 done", (await page.getByTestId("start-count").innerText()).trim() === "2 of 5 done");
      await ctx.close();
    }

    // showing it again brings it back for the key that hid it
    {
      const shown = await api("DELETE", "/api/home/start/hide", seed.key_secret);
      ck("show again: the cell answers with the block shown", shown.status === 200 && shown.body.show === true && shown.body.total === 5, JSON.stringify(shown.body));
      const { ctx, page } = await context(1440);
      await liveOpen(page);
      ck("show again: the list is back on the page", (await list(page).count()) === 1);

      // loading, held: the list keeps its shape with Checking rows; then the real one
      let release; const hold = new Promise((r) => { release = r; });
      await page.route("**/api/home", async (r) => { await hold; await r.continue(); });
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home-loading").waitFor();
      ck("live loading: the Checking list shows while the first read is out", (await page.getByTestId("start-checklist-loading").count()) === 1
         && (await page.getByTestId("start-step").count()) === 0);
      await shot(page, "live-loading", 1440);
      release();
      await page.getByTestId("factory-home").waitFor();
      ck("live loading: then the real list", (await list(page).count()) === 1 && (await page.getByTestId("start-checklist-loading").count()) === 0);
      await page.unroute("**/api/home");

      // a first read that fails: the home's error, and the list adds none
      await page.route("**/api/home", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }));
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home-error").waitFor();
      ck("live error: the home's error shows and the list adds none", (await list(page).count()) === 0 && (await page.getByTestId("start-checklist-loading").count()) === 0);
      await shot(page, "live-error", 1440);
      await checkWords(page, ck, "live error");
      await page.unroute("**/api/home");
      await ctx.close();
    }

    // an API that has no start block (an older answer) draws a home without the list
    {
      const { ctx, page } = await context(1440);
      await page.route("**/api/home", async (r) => {
        const res = await r.fetch();
        const data = await res.json();
        delete data.start;
        await r.fulfill({ response: res, json: data });
      });
      await liveOpen(page);
      ck("no start block: the home shows without a list", (await list(page).count()) === 0);
      await ctx.close();
    }

    // without the flag the address alone shows no sample, but the cell's own page
    {
      const { ctx, page } = await context(1440);
      await page.goto(`${BASE}/?sample=startNothing`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
      ck("no flag: ?sample=startNothing shows the cell's own count", (await page.getByTestId("start-count").innerText()).trim() === "2 of 5 done");
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
