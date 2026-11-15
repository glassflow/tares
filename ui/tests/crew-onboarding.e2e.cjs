/*
 * Browser e2e for TR-1312: the first run of a new factory cell, from an empty home to a crew that is up.
 *
 * Seeds a brand-new factory cell with tests/seed-crew-onboarding.py (nothing saved, one ingest-only key named
 * login-key), starts one daemon on it with a root token on TARES_E2E_PORT (default 8877), MCP port +1, in a clean
 * environment and a fresh data directory, and stops only it. No sample flag: every state is the cell's own,
 * reached the way a person reaches it, and the page is never reloaded to see a step tick (it reads the cell
 * again every few seconds while the first run shows).
 * States: fresh (six steps, the form on the crew's own defaults, Recommended on L3-review, four commands to copy,
 * the crew panel empty), a save that fails (nothing saved), nothing saved until Save and continue, choices saved,
 * GitHub not connected and Linear not connected (Save and continue stays off), GitHub connected and tickets
 * placed, Tares installed and signed in (one report from an ingest-only key, which cannot change the choices),
 * the crew coming up (orchestrator running, reviewer starting), the crew up, a planned project (the list goes),
 * and an older cell with no onboarding block (the plain list). At 1440 and 390 wide: the words check in every
 * state, no horizontal scroll. Shots: crew-onboarding-<state>-<width>.png in SHOTS_DIR.
 * Run:  cd ui && npm run build && TARES_E2E_PORT=<free port> node tests/crew-onboarding.e2e.cjs
 * (tares in ../.venv or on PATH). TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8877);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROOT = "root-token-onboarding-e2e";
const KEYS = ["choices", "connections", "install", "login", "crew", "spec"];
const TITLES = {
  choices: "Decide how your crew works",
  connections: "Connect GitHub and choose where tickets live",
  install: "Install Tares",
  login: "Sign your machine in",
  crew: "Start the crew",
  spec: "Plan the first project",
};
const WAIT = 25000;     // the page reads the cell every 5 seconds and the cell keeps its answer for 5 more

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

const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString();
const stationLines = (sid, station, role, state) => [
  { type: "session_station", sessionId: sid, cwd: "/w/crew", timestamp: iso(40), station, role, parent: "", factory_station: station },
  { type: "session_state", sessionId: sid, cwd: "/w/crew", timestamp: iso(8), state, reason: "" },
];

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-crew-onboarding-e2e-"));
  const python = process.env.TARES_E2E_PYTHON || "python3";
  const base = { TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist,
                 TARES_OTLP_GRPC_PORT: "off", TARES_EDITION: "factory", PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test

  const seeded = spawnSync(python, [path.join(__dirname, "seed-crew-onboarding.py"), home, String(PORT), String(PORT + 1), dist, tares, ROOT],
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
    const home_ = async () => (await api("GET", "/api/home", ROOT)).body;
    const ingest = async (lines) => {
      const r = await fetch(`${BASE}/ingest/claude_code`, { method: "POST",
        headers: { "content-type": "application/x-ndjson", authorization: `Bearer ${ROOT}` },
        body: lines.map((l) => JSON.stringify(l)).join("\n") + "\n" });
      return r.status;
    };

    const context = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript((t) => { try { window.localStorage.setItem("tares_token", t); } catch { /* blocked */ } }, ROOT);
      const page = await ctx.newPage();
      const puts = { settings: 0, tracker: 0 };
      page.on("request", (req) => {
        const p = new URL(req.url()).pathname;
        if (req.method() === "PUT" && p === "/api/crew/settings") puts.settings++;
        if (req.method() === "PUT" && p === "/api/crew/tracker") puts.tracker++;
      });
      return { ctx, page, puts };
    };
    const open = async (page) => {
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.getByTestId("start-checklist").waitFor();
      await page.waitForLoadState("networkidle");
      await page.evaluate(() => { window.__same_page = true; });
    };
    const samePage = (page) => page.evaluate(() => window.__same_page === true);
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const noOverflow = async (page, label) => {
      ck(`${label}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      ck(`${label}: nothing on the page is wider than the window`, await page.evaluate(() => {
        const w = document.documentElement.clientWidth;
        return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1);
      }));
    };
    const shot = async (page, name, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `crew-onboarding-${name}-${width}.png`), fullPage: true });
    };
    const steps = (page) => page.getByTestId("start-step");
    const row = (page, key) => page.locator(`[data-testid=start-step][data-key=${key}]`);
    const doneOf = (page) => steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-done") === "true"));
    const count = async (page) => (await page.getByTestId("start-count").innerText()).trim();
    const waitCount = (page, text) => page.waitForFunction(
      (t) => (document.querySelector("[data-testid=start-count]")?.textContent || "").trim() === t, text, { timeout: WAIT });
    const state = (page, who) => page.getByTestId(`crew-${who}`).getAttribute("data-state");
    // the same cell on a phone: it only looks
    const phone = async (name, expectCount) => {
      const { ctx, page } = await context(390);
      await open(page);
      await shot(page, name, 390);
      ck(`${name} @390: ${expectCount}`, (await count(page)) === expectCount, await count(page));
      await checkWords(page, ck, `${name} @390`);
      await noOverflow(page, `${name} @390`);
      await ctx.close();
    };

    // ── 1. a new cell ──
    const { ctx, page, puts } = await context(1440);
    const oneRead = [];
    page.on("request", (req) => { if (new URL(req.url()).pathname === "/api/home") oneRead.push(req.url()); });
    await open(page);
    const fresh = await home_();
    ck("fresh: the cell carries the first-run block", !!fresh.onboarding && fresh.start.total === 6 && fresh.start.done === 0 && fresh.start.show === true,
       JSON.stringify(fresh.start && { done: fresh.start.done, total: fresh.start.total }));
    await shot(page, "fresh", 1440);
    ck("fresh: the section is named Get your crew running", (await page.getByRole("region", { name: "Get your crew running" }).count()) === 1
       && (await page.getByTestId("onboarding-title").innerText()).trim() === "Get your crew running");
    ck("fresh: 0 of 6 done and six steps in order", (await count(page)) === "0 of 6 done"
       && (await steps(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-key")))).join() === KEYS.join());
    for (const k of KEYS) ck(`fresh: ${k} has its title`, (await row(page, k).innerText()).includes(TITLES[k]));
    ck("fresh: nothing is ticked and the first step is the current one", (await doneOf(page)).every((d) => !d)
       && (await row(page, "choices").getAttribute("data-current")) === "true");
    ck("fresh: the steps are split into this workspace and your terminal", (await page.getByText("Here, in this workspace").count()) === 1
       && (await page.getByText("Then, in your terminal").count()) === 1 && (await page.getByTestId("terminal-panel").count()) === 1);
    ck("fresh: what you need is said once, in the terminal panel", (await page.getByTestId("prerequisites").count()) === 1
       && (await page.getByTestId("prerequisites").innerText()).includes("Claude Code"));
    ck("fresh: the form starts on the crew's own defaults", (await page.getByTestId("choice-L4-ship").isChecked())
       && !(await page.getByTestId("choice-L3-review").isChecked())
       && (await page.getByTestId("builders-count").innerText()).trim() === String(fresh.onboarding.choices.defaults.builders_max)
       && (await page.getByTestId("toggle-releases").getAttribute("aria-checked")) === String(fresh.onboarding.choices.defaults.releaser));
    ck("fresh: L3-review says Recommended for a new crew, and only it", (await page.getByTestId("recommended").count()) === 1
       && (await page.locator("label", { has: page.getByTestId("choice-L3-review") }).getByTestId("recommended").innerText()).trim() === "Recommended for a new crew");
    ck("fresh: no release profile and no pattern in the first run", !/profile|pattern/i.test(await page.getByTestId("choices-form").innerText()));
    ck("fresh: the cell has saved nothing", fresh.onboarding.choices.chosen === false && fresh.onboarding.tracker === null);
    ck("fresh: four commands to copy, each with a Copy button", (await page.getByTestId("terminal-panel").getByTestId("start-copy").count()) === 4
       && (await page.locator("[data-testid=terminal-panel] code[title]").allInnerTexts()).join("|") === "uv tool install tares|tares login|tares factory crew up|tares factory spec");
    ck("fresh: the crew step points to tares factory doctor", (await page.getByTestId("crew-doctor").innerText()).includes("tares factory doctor"));
    ck("fresh: Connect GitHub is a link inside the console", (await row(page, "connections").getByRole("link", { name: "Connect GitHub" }).getAttribute("href")) === "/settings#tools");
    ck("fresh: no Connect Linear until Linear is chosen, and no docs link for GitHub Issues",
       (await row(page, "connections").getByRole("link", { name: "Connect Linear" }).count()) === 0
       && (await row(page, "connections").getByRole("link", { name: "Use GitHub Issues" }).count()) === 0);
    ck("fresh: Your crew shows an empty crew", (await page.getByTestId("crew-machine-state").innerText()).trim() === "Not signed in"
       && (await state(page, "orchestrator")) === "none" && (await state(page, "reviewer")) === "none"
       && (await page.getByTestId("crew-builders-state").innerText()).trim() === "None yet" && (await page.getByTestId("crew-links").count()) === 0);
    ck("fresh: the page shows Your crew once, not twice", (await page.getByRole("heading", { name: "Your crew", exact: true }).count()) === 1);
    ck("fresh: the list sits above Waiting for you", await page.evaluate(() => {
      const a = document.querySelector("[data-testid=start-checklist]").getBoundingClientRect();
      const b = [...document.querySelectorAll("section")].find((s) => s.getAttribute("aria-label") === "Waiting for you");
      return !b || a.bottom <= b.getBoundingClientRect().top;
    }));
    await checkWords(page, ck, "fresh @1440");
    await noOverflow(page, "fresh @1440");
    await phone("fresh", "0 of 6 done");

    // an older cell: no onboarding block, so the plain list shows and nothing breaks
    {
      const { ctx: c2, page: p2 } = await context(1440);
      await p2.route("**/api/home", async (r) => {
        const res = await r.fetch();
        const data = await res.json();
        delete data.onboarding;
        await r.fulfill({ response: res, json: data });
      });
      const crashes = [];
      p2.on("pageerror", (e) => crashes.push(String(e)));
      await p2.goto(`${BASE}/`, { waitUntil: "load" });
      await p2.getByTestId("factory-home").waitFor();
      await p2.waitForLoadState("networkidle");
      ck("older cell: the plain list shows its six steps", (await p2.getByTestId("start-checklist").count()) === 1
         && (await p2.getByTestId("start-step").count()) === 6 && (await p2.getByTestId("choices-form").count()) === 0);
      ck("older cell: nothing is thrown", crashes.length === 0, crashes.join(" | "));
      await shot(p2, "older-cell", 1440);
      await checkWords(p2, ck, "older cell @1440");
      await c2.close();
    }

    // ── 2. choices: nothing is saved until the person confirms ──
    await page.getByTestId("choice-L3-review").check();
    await page.getByTestId("builders-more").click();
    ck("choices: the form follows the clicks", (await page.getByTestId("choice-L3-review").isChecked())
       && (await page.getByTestId("builders-count").innerText()).trim() === String(fresh.onboarding.choices.defaults.builders_max + 1));
    await page.waitForTimeout(600);
    ck("choices: changing the form calls nothing", puts.settings === 0 && (await home_()).onboarding.choices.chosen === false, String(puts.settings));

    const settingsBefore = (await api("GET", "/api/crew/settings", ROOT)).body;

    // a save the cell refuses: said in plain words, the form stays, nothing is kept
    await page.route("**/api/crew/settings", (r) => r.request().method() === "PUT"
      ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }) : r.continue());
    await page.getByTestId("choices-save").click();
    await page.getByTestId("step-error").waitFor();
    ck("failed save: the line says it was not saved", (await page.getByTestId("step-error").innerText()).trim() === "That was not saved. Try again in a moment.");
    ck("failed save: it has role alert, the form stays and the step is not ticked", (await page.getByRole("alert").filter({ hasText: "That was not saved" }).count()) === 1
       && (await page.getByTestId("choices-form").count()) === 1 && (await count(page)) === "0 of 6 done");
    ck("failed save: the cell still has nothing", (await home_()).onboarding.choices.chosen === false);
    await shot(page, "save-failed", 1440);
    await checkWords(page, ck, "save failed @1440");
    await page.unroute("**/api/crew/settings");

    // a save that works: the step ticks at once, without a reload
    await page.getByTestId("choices-save").click();
    await waitCount(page, "1 of 6 done");
    ck("choices saved: the first step is ticked in place, the second is current", (await doneOf(page)).join() === "true,false,false,false,false,false"
       && (await row(page, "connections").getAttribute("data-current")) === "true");
    const settingsAfter = (await api("GET", "/api/crew/settings", ROOT)).body;
    const saved = (await home_()).onboarding.choices;
    ck("choices saved: the cell holds what was chosen", saved.chosen === true && saved.autonomy === "L3-review"
       && saved.builders_max === fresh.onboarding.choices.defaults.builders_max + 1 && saved.releaser === fresh.onboarding.choices.defaults.releaser, JSON.stringify(saved));
    ck("choices saved: release profile and pattern were not touched by the first run",
       JSON.stringify(settingsAfter.release_profile) === JSON.stringify(settingsBefore.release_profile)
       && JSON.stringify(settingsAfter.prod_pattern) === JSON.stringify(settingsBefore.prod_pattern), JSON.stringify([settingsBefore.release_profile, settingsAfter.release_profile]));
    ck("choices saved: the step says what was chosen, with Change next to it", (await row(page, "choices").getByTestId("step-detail").innerText()).includes("You approve each merge")
       && (await row(page, "choices").getByTestId("step-change").count()) === 1 && (await row(page, "choices").getByTestId("choices-form").count()) === 0);
    ck("choices saved: the page did not reload", await samePage(page));
    await shot(page, "choices-saved", 1440);
    await checkWords(page, ck, "choices saved @1440");
    await noOverflow(page, "choices saved @1440");

    // Change reopens the form on what is saved
    await row(page, "choices").getByTestId("step-change").click();
    ck("change: the form reopens on what is saved", (await page.getByTestId("choice-L3-review").isChecked())
       && (await page.getByTestId("builders-count").innerText()).trim() === String(saved.builders_max));
    await page.getByTestId("choices-save").click();
    await page.getByTestId("choices-form").waitFor({ state: "detached" });
    ck("change: saving again closes it and the count stays", (await count(page)) === "1 of 6 done");

    // ── 3. GitHub and the tickets' place ──
    ck("connections: GitHub is not connected, said so, and Save and continue is off",
       (await page.getByTestId("github-state").innerText()).includes("Not connected")
       && (await page.getByTestId("connections-save").isDisabled())
       && (await row(page, "connections").innerText()).includes("Connect GitHub to continue."));
    await page.getByTestId("choice-linear").check();
    ck("connections: choosing Linear without it connected shows Connect Linear and keeps Save off",
       (await page.getByTestId("linear-needed").count()) === 1
       && (await page.getByTestId("linear-needed").getByRole("link", { name: "Connect Linear" }).getAttribute("href")) === "/settings#tools"
       && (await page.getByTestId("connections-save").isDisabled()));
    await shot(page, "github-and-linear-missing", 1440);
    await checkWords(page, ck, "github and linear missing @1440");
    await phone("github-and-linear-missing", "1 of 6 done");
    await page.getByTestId("choice-tares").check();
    ck("connections: back on Tares there is nothing more to connect, but GitHub still is", (await page.getByTestId("linear-needed").count()) === 0
       && (await page.getByTestId("connections-save").isDisabled()));
    const refusedByKey = await api("PUT", "/api/crew/tracker", seed.login_key, { tracker: "tares" });
    ck("connections: a key that may only ingest cannot place the tickets", refusedByKey.status === 403, String(refusedByKey.status));

    // connect GitHub (the person does this under Settings; here, through the cell)
    const gh = await api("POST", "/api/integrations/github", ROOT, { name: "main", kind: "token", token: "ghp_example", api_url: "http://127.0.0.1:9" });
    ck("connections: GitHub connected through the cell", gh.status === 201, JSON.stringify(gh));
    await page.waitForFunction(() => {
      const b = document.querySelector("[data-testid=connections-save]");
      return !!b && !b.disabled;
    }, null, { timeout: WAIT });
    ck("connections: the page noticed without a reload and Save and continue is on", (await samePage(page))
       && !(await page.getByTestId("github-state").innerText()).includes("Not connected"));
    // a refused save keeps the form
    await page.route("**/api/crew/tracker", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }));
    await page.getByTestId("connections-save").click();
    await row(page, "connections").getByTestId("step-error").waitFor();
    ck("connections: a refused save says so and keeps the form", (await row(page, "connections").getByTestId("step-error").innerText()).trim() === "That was not saved. Try again in a moment."
       && (await count(page)) === "1 of 6 done");
    await page.unroute("**/api/crew/tracker");
    await page.getByTestId("connections-save").click();
    await waitCount(page, "2 of 6 done");
    ck("connections saved: ticked in place, with what is connected and where tickets live", (await doneOf(page)).join() === "true,true,false,false,false,false"
       && (await row(page, "connections").getByTestId("step-detail").innerText()).includes("tickets in Tares")
       && (await row(page, "connections").getByTestId("step-detail").innerText()).includes("GitHub connected"));
    ck("connections saved: the install step is the current one", (await row(page, "install").getAttribute("data-current")) === "true");
    await shot(page, "connections-saved", 1440);
    await checkWords(page, ck, "connections saved @1440");
    await noOverflow(page, "connections saved @1440");

    // ── 4. Tares installed and signed in: one report from the machine ──
    await api("POST", "/api/crew/machine", seed.login_key, { machine: "", version: "" });
    await new Promise((r) => setTimeout(r, 6000));      // the cell keeps its answer for five seconds
    ck("machine: a report with nothing in it ticks nothing", (await home_()).start.done === 2 && (await count(page)) === "2 of 6 done");
    const report = await api("POST", "/api/crew/machine", seed.login_key, { machine: "example-laptop", version: "1.60.0" });
    ck("machine: a key that may only ingest can report its machine", report.status === 200, JSON.stringify(report));
    await waitCount(page, "4 of 6 done");
    ck("machine: install and sign in tick together, in place", (await doneOf(page)).join() === "true,true,true,true,false,false" && (await samePage(page)));
    ck("machine: the steps say what was learned", (await row(page, "install").getByTestId("step-detail").innerText()).trim() === "Installed: tares 1.60.0."
       && (await row(page, "login").getByTestId("step-detail").innerText()).trim() === "Signed in from example-laptop.");
    ck("machine: done terminal steps have no copy box, the open ones keep theirs", (await row(page, "install").getByTestId("start-copy").count()) === 0
       && (await row(page, "crew").getByTestId("start-copy").count()) === 1 && (await row(page, "spec").getByTestId("start-copy").count()) === 1);
    ck("machine: Your crew names the machine", (await page.getByTestId("crew-machine-state").innerText()).trim() === "example-laptop");
    ck("machine: the crew step is the current one", (await row(page, "crew").getAttribute("data-current")) === "true");
    ck("machine: the cell never stored a key in the report", !JSON.stringify((await home_()).onboarding).includes(seed.login_key));
    await shot(page, "installed-and-signed-in", 1440);
    await checkWords(page, ck, "installed and signed in @1440");
    await noOverflow(page, "installed and signed in @1440");
    await phone("installed-and-signed-in", "4 of 6 done");

    // ── 5. the crew comes up: the orchestrator first, then the reviewer ──
    ck("crew: the orchestrator reports", (await ingest(stationLines("o1", "crew-orchestrator", "orchestrator", "working"))) < 300);
    await page.getByTestId("crew-coming").waitFor({ timeout: WAIT });
    ck("crew: the page says what is coming up, live", (await page.getByTestId("crew-coming").innerText()).replace(/\s+/g, " ").trim() === "Orchestrator is running Reviewer is starting"
       && (await state(page, "orchestrator")) === "running" && (await state(page, "reviewer")) === "starting", await page.getByTestId("crew-coming").innerText());
    ck("crew: the step is not ticked while the reviewer is missing, and still points to doctor", (await doneOf(page))[4] === false
       && (await page.getByTestId("crew-doctor").count()) === 1 && (await count(page)) === "4 of 6 done");
    await shot(page, "crew-coming-up", 1440);
    await checkWords(page, ck, "crew coming up @1440");
    await noOverflow(page, "crew coming up @1440");
    await phone("crew-coming-up", "4 of 6 done");

    ck("crew: the reviewer reports", (await ingest(stationLines("r1", "crew-reviewer", "reviewer", "idle"))) < 300);
    await waitCount(page, "5 of 6 done");
    ck("crew: both are up, the step ticks and the page says the crew is up", (await doneOf(page)).join() === "true,true,true,true,true,false"
       && (await page.getByTestId("onboarding-title").innerText()).trim() === "Your crew is up"
       && (await page.getByTestId("onboarding-sub").innerText()).includes("example-laptop")
       && (await state(page, "orchestrator")) === "running" && (await state(page, "reviewer")) === "running" && (await samePage(page)));
    ck("crew: the doctor line and the waiting note are gone, the plan step is current", (await page.getByTestId("crew-doctor").count()) === 0
       && (await page.getByTestId("crew-coming").count()) === 0 && (await row(page, "spec").getAttribute("data-current")) === "true");
    ck("crew: Your crew links to Settings and the Crew page", (await page.getByTestId("crew-links").getByRole("link", { name: "Change in Settings" }).getAttribute("href")) === "/settings#crew"
       && (await page.getByTestId("crew-links").getByRole("link", { name: "Open the Crew page" }).getAttribute("href")) === "/crew");
    await shot(page, "crew-up", 1440);
    await checkWords(page, ck, "crew up @1440");
    await noOverflow(page, "crew up @1440");
    await phone("crew-up", "5 of 6 done");

    // ── 6. a planned project: every step is done and the list goes ──
    const made = await api("POST", "/api/projects", ROOT, { template: "custom", name: "parts-shop", objects: [], kind: "software_factory",
                                                            goal: "A small shop for spare parts." });
    ck("plan: a project was made", made.status < 300 && !!made.body.id, JSON.stringify(made.body).slice(0, 200));
    const t1 = await api("POST", `/api/projects/${made.body.id}/tickets`, ROOT, { title: "Show the parts list", repo: "example-org/parts-shop" });
    ck("plan: it has a ticket", t1.status < 300, JSON.stringify(t1.body).slice(0, 200));
    await page.waitForFunction(() => !document.querySelector("[data-testid=start-checklist]"), null, { timeout: WAIT });
    ck("plan: all six are done, so the list is gone, without a reload", (await page.getByTestId("start-step").count()) === 0
       && (await page.getByTestId("start-hide").count()) === 0 && (await page.getByTestId("choices-form").count()) === 0 && (await samePage(page)));
    const done = (await home_()).start;
    ck("plan: the cell says 6 of 6 and does not show the list", done.done === 6 && done.total === 6 && done.show === false, JSON.stringify({ done: done.done, show: done.show }));
    ck("plan: the home is its usual page, with Your crew back in the side column", (await page.getByRole("heading", { name: "Your crew", exact: true }).count()) === 1
       && (await page.getByText("parts-shop").count()) > 0);
    await shot(page, "plan-done", 1440);
    await checkWords(page, ck, "plan done @1440");
    await noOverflow(page, "plan done @1440");
    {
      const { ctx: c3, page: p3 } = await context(390);
      await p3.goto(`${BASE}/`, { waitUntil: "load" });
      await p3.getByTestId("factory-home").waitFor();
      await p3.waitForLoadState("networkidle");
      ck("plan @390: no list on a phone either", (await p3.getByTestId("start-checklist").count()) === 0);
      await shot(p3, "plan-done", 390);
      await checkWords(p3, ck, "plan done @390");
      await noOverflow(p3, "plan done @390");
      await c3.close();
    }
    ck("the page read the cell more than once and was never reloaded", oneRead.length > 4 && (await samePage(page)), String(oneRead.length));
    await ctx.close();
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
