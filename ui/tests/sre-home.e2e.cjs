/*
 * Browser e2e for TR-749: the AI SRE home page (/ on an AI SRE cell).
 *
 * The page is one read of the cell (GET /api/home): a headline, then either the watcher-off panel or the
 * open incident card(s), the services, what was last raised (with a Yes / No for "Was this worth raising?"),
 * and in the second column who is on watch, today, blind spots and what is connected. The buttons call their
 * own routes: "Add $25 and restart", "I'm on it", "Mark resolved", Yes and No. This script checks each
 * state of a real cell (watcher off, all quiet, one incident, many incidents, a new cell, a silent service)
 * and the sample states (watcher off with a monthly budget, loading, error, long text, many items). Every
 * state shows only words from words.json, has no trial card, and does not scroll sideways at 390 px.
 *
 * Each live state seeds a scratch AI SRE cell (tests/seed-sre-home.py over tests/fixtures/sre_cell.py, invented
 * names), then runs the real daemon on it, serving the built console. The sample states (?sample=<name>, with
 * the sample flag in localStorage) need no data.
 * Run:  npm run build && node tests/sre-home.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px (default: a temporary directory).
 * TARES_E2E_PORT sets the daemon port (default 8861); the next port is its MCP endpoint.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8861);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-home-shots-"));
fs.mkdirSync(SHOTS, { recursive: true });
const HOME_API = "/api/home";
const STATES = ["watcher-off", "all-quiet", "one-incident", "many-incidents", "new-cell", "silent-service"];

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

const waitGone = async (url, tries = 50) => {
  for (; tries > 0; tries--) {
    const up = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (!up) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
};

// The daemon's environment: the real one minus every key that could reach a model, Slack or another cell.
function cleanEnv(home, dist) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^(TARES|TARESD|ANTHROPIC|OPENAI)/.test(k) || /SLACK/.test(k)) continue;
    env[k] = v;
  }
  return {
    ...env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
    TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO,
    TARES_TRIGGER_DEBOUNCE_SECONDS: "0", TARES_SCHEDULE_CLOCK_SECONDS: "3600", TARES_WATCH_SECONDS: "3600",
    // a made-up key: the cell counts a service as watched only when the watcher has a model; the clocks
    // above are an hour long, so no call is ever made with it
    ANTHROPIC_API_KEY: "sk-ant-test-not-a-real-key",
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
  const dark = async (page, n) => {
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    await page.waitForTimeout(600); // the controls fade between themes
    await shot(page, n);
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
    await page.waitForTimeout(300);
  };
  const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const text = (loc) => loc.innerText().then((t) => t.replace(/\s+/g, " ").trim());
  const bare = (page) => page.evaluate(() => [...document.querySelectorAll("main ul, .main ul")]
    .every((u) => getComputedStyle(u).listStyleType === "none" && parseFloat(getComputedStyle(u).paddingLeft) === 0));
  const body = (page) => text(page.locator("body"));

  // What every state has in common: no trial card, the words rule, no sideways scroll, no bullets.
  async function common(page, state, width) {
    const t = await body(page);
    ck(`${state} ${width}: no trial card`, !/trial/i.test(t), t.match(/.{0,30}trial.{0,30}/i)?.[0]);
    ck(`${state} ${width}: no "Switch to the trial watcher" button`, (await page.getByRole("button", { name: /switch to the trial watcher/i }).count()) === 0);
    await checkWords(page, ck, `${state} ${width}`);
    ck(`${state} ${width}: does not scroll sideways`, (await overflow(page)) <= 0, String(await overflow(page)));
    ck(`${state} ${width}: lists have no bullets or indent`, await bare(page));
  }

  const sections = async (page) => ({
    services: await page.getByTestId("home-services").count(),
    raised: await page.getByTestId("raised").count(),
    onWatch: await page.getByTestId("on-watch").count(),
    today: await page.getByTestId("home-today").count(),
    blind: await page.getByTestId("blind-spots").count(),
    tools: await page.getByTestId("tools").count(),
  });

  // ── the live states ──────────────────────────────────────────────────────
  for (const state of STATES) {
    console.log(`== ${state} ==`);
    const home = fs.mkdtempSync(path.join(os.tmpdir(), `tares-sre-home-e2e-${state}-`));
    execFileSync(python, [path.join(__dirname, "seed-sre-home.py"), home, state], { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } });
    const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env: cleanEnv(home, dist) });
    try {
      ck(`${state}: daemon up`, await waitFor(`${BASE}/health`));
      const { ctx, page } = await newPage(1440);
      const puts = [], posts = [];
      page.on("request", (r) => {
        const p = new URL(r.url()).pathname;
        if (r.method() === "PUT") puts.push({ p, body: JSON.parse(r.postData() || "{}") });
        if (r.method() === "POST") posts.push({ p, body: r.postData() });
      });
      const one = expectOneCall(page, HOME_API);
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("sre-home").waitFor();
      await page.getByTestId("home-headline").waitFor();
      await one(ck, state);
      const headline = await text(page.getByTestId("home-headline"));
      const sub = await page.getByTestId("home-sub").count() ? await text(page.getByTestId("home-sub")) : "";
      ck(`${state}: the page has its headline`, (await page.getByRole("heading", { level: 1 }).count()) === 1 && headline.length > 0, headline);
      const sec = await sections(page);

      if (state === "watcher-off") {
        ck("watcher-off: the headline says nobody has watched", /^Nobody has watched .* since ((yesterday|\d+ [A-Z][a-z]{2}), )?\d\d:\d\d$/.test(headline), headline);
        const w = page.getByTestId("watch-off");
        ck("watcher-off: the panel says why", (await text(page.getByTestId("watch-off-title"))).startsWith("The watcher is off: the watcher used its $25.00 budget"), await text(page.getByTestId("watch-off-title")));
        const wt = await text(w);
        ck("watcher-off: it has Off for, Last check said and Spent", ["off for", "last check said", "spent"].every((k) => wt.toLowerCase().includes(k)), wt);
        ck("watcher-off: Spent is for the month and says when it starts again", /Spent \$25\.00 of \$25 this month/.test(wt) && /Starts again on \d+ [A-Z][a-z]{2}/.test(wt), wt);
        ck("watcher-off: it says nobody was told", (await text(page.getByTestId("watch-told"))) === "Nobody was told.");
        ck("watcher-off: the button names the amount", (await text(page.getByTestId("way-out-raise_budget"))) === "Add $25 and restart");
        ck("watcher-off: no incident card, no Today", (await page.getByTestId("incident-card").count()) === 0 && sec.today === 0);
        ck("watcher-off: the services, raised and the aside are there", sec.services === 1 && sec.raised === 1 && sec.onWatch === 1 && sec.blind === 1 && sec.tools === 1, JSON.stringify(sec));
        ck("watcher-off: the panel comes before the services", await page.evaluate(() => {
          const a = document.querySelector("[data-testid=watch-off]"), b = document.querySelector("[data-testid=home-services]");
          return !!(a && b && a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        }));
      }
      if (state === "all-quiet" || state === "silent-service") {
        ck(`${state}: the headline is All quiet`, headline === "All quiet", headline);
        ck(`${state}: the sub says how many are watched and when`, /services? watched, last check/.test(sub), sub);
        ck(`${state}: no panel, no incident`, (await page.getByTestId("watch-off").count()) === 0 && (await page.getByTestId("incident-card").count()) === 0);
        ck(`${state}: services, raised, who is on watch, today, blind spots and connected`, Object.values(sec).every((n) => n === 1), JSON.stringify(sec));
        ck(`${state}: a service row opens its page`, (await page.getByTestId("service-row").first().locator("a").getAttribute("href")).startsWith("/services/"));
        ck(`${state}: All findings goes to the findings`, (await page.getByTestId("all-findings").getAttribute("href")) === "/incidents?show=all");
        ck(`${state}: the quiet checks are said under the raised list`, /came back quiet/.test(await text(page.getByTestId("raised"))), await text(page.getByTestId("raised")));
        if (state === "silent-service") {
          ck("silent-service: the sub names the silent service", /service-a-receiver has sent nothing/.test(sub), sub);
          ck("silent-service: and so does a blind spot", /service-a-receiver has sent nothing/.test(await text(page.getByTestId("blind-spots"))), await text(page.getByTestId("blind-spots")));
        }
      }
      if (state === "all-quiet") {
        // Read the note (TR-894): it opens the incident the check was filed under, not the check
        // The link shows for a person who cannot answer (a read key): the same home, with can_answer off
        const ro = await ctx.newPage();
        await ro.route("**/api/home", async (r) => {
          const res = await r.fetch();
          const body = await res.json();
          body.raised = (body.raised || []).map((x) => ({ ...x, can_answer: false }));
          await r.fulfill({ response: res, json: body });
        });
        await ro.goto(`${BASE}/`, { waitUntil: "load" });
        await ro.getByTestId("raised").waitFor();
        const filed = ro.getByTestId("raised-row").filter({ has: ro.getByTestId("read-note") });
        const loose = ro.getByTestId("raised-row").filter({ hasNot: ro.getByTestId("read-note") });
        ck("all-quiet: only the check filed under an incident has Read the note", (await filed.count()) === 1 && (await loose.count()) === 1, `${await filed.count()} / ${await loose.count()}`);
        const href = await filed.getByTestId("read-note").getAttribute("href");
        ck("all-quiet: Read the note goes to an incident id, not the run id", /^\/incidents\/inc_[0-9a-f]+$/.test(href || "") && !(href || "").includes(await filed.getAttribute("data-run")), href);
        const there = await ctx.newPage();
        await there.goto(`${BASE}${href}`, { waitUntil: "load" });
        await there.getByTestId("status-line").or(there.getByTestId("incident-missing")).first().waitFor();
        ck("all-quiet: and that page opens the incident", (await there.getByTestId("incident-missing").count()) === 0 && (await there.getByTestId("status-line").count()) === 1);
        await there.close();
        await ro.close();
        // Yes on a raised item: the answer is sent and shown
        const row = page.getByTestId("raised-row").first();
        const rid = await row.getAttribute("data-run");
        await row.getByTestId("worth-yes").click();
        await row.getByTestId("raised-answer").waitFor();
        ck("all-quiet: Yes sends the answer to that check", puts.length === 1 && puts[0].p === `/api/findings/${rid}/worth` && puts[0].body.worth === true, JSON.stringify(puts));
        ck("all-quiet: the row shows the answer", /worth raising/.test(await text(row.getByTestId("raised-answer"))));
        ck("all-quiet: and no longer asks", (await row.getByTestId("worth-yes").count()) === 0);
      }
      if (state === "one-incident") {
        ck("one-incident: the headline names the service", /^1 incident open on service-a/.test(headline), headline);
        const card = page.getByTestId("incident-card");
        ck("one-incident: one incident card", (await card.count()) === 1);
        const ct = await text(card);
        ck("one-incident: it has the three columns", ["what is failing", "most likely cause", "suggested next step"].every((k) => ct.toLowerCase().includes(k)), ct);
        ck("one-incident: it has the title and the age", /service-a/.test(await text(page.getByTestId("incident-title"))) && /open .*nobody has picked it up/.test(await text(page.getByTestId("incident-age"))), await text(page.getByTestId("incident-age")));
        ck("one-incident: a chart with a dashed mark for the deploy", (await card.locator("svg [data-mark]").count()) >= 1, String(await card.locator("svg [data-mark]").count()));
        ck("one-incident: the post in Slack is said", /Posted to #ops-alerts (at \d\d:\d\d|on \d+ [A-Z][a-z]{2}, \d\d:\d\d)/.test(await text(page.getByTestId("incident-foot"))), await text(page.getByTestId("incident-foot")));
        ck("one-incident: the timeline link is the service's", (await page.getByTestId("incident-timeline").getAttribute("href")) === "/timeline?service=service-a");
        ck("one-incident: all three buttons", (await page.getByTestId("incident-on-it").count()) === 1 && (await page.getByTestId("incident-resolve").count()) === 1);
        ck("one-incident: the incident comes before the services", await page.evaluate(() => {
          const a = document.querySelector("[data-testid=incident-card]"), b = document.querySelector("[data-testid=home-services]");
          return !!(a && b && a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        }));
        ck("one-incident: no Today panel missing, no watcher-off panel", (await page.getByTestId("watch-off").count()) === 0 && sec.today === 1);
      }
      if (state === "many-incidents") {
        ck("many-incidents: the headline counts them", headline === "5 incidents open", headline);
        ck("many-incidents: three cards", (await page.getByTestId("incident-card").count()) === 3);
        ck("many-incidents: and a link to the other two", (await text(page.getByTestId("incidents-more"))) === "2 more open" && (await page.getByTestId("incidents-more").getAttribute("href")) === "/incidents");
      }
      if (state === "new-cell") {
        ck("new-cell: the headline", headline === "No services yet", headline);
        ck("new-cell: one Connect button to the setup", (await page.getByTestId("connect-link").count()) === 1 && (await page.getByTestId("connect-link").getAttribute("href")) === "/settings#tools");
        ck("new-cell: no services, no raised list, no panel", sec.services === 0 && sec.raised === 0 && (await page.getByTestId("watch-off").count()) === 0, JSON.stringify(sec));
      }

      await common(page, state, 1440);
      await shot(page, `sre-home-${state}-desktop.png`);
      if (state === "one-incident") await dark(page, `sre-home-${state}-dark-desktop.png`);

      // the narrow page
      const narrow = await newPage(390);
      await narrow.page.goto(`${BASE}/`, { waitUntil: "load" });
      await narrow.page.getByTestId("home-headline").waitFor();
      await narrow.page.waitForLoadState("networkidle");
      await common(narrow.page, state, 390);
      await shot(narrow.page, `sre-home-${state}-narrow.png`);
      await narrow.ctx.close();

      // actions that change the page
      if (state === "watcher-off") {
        const before = posts.length;
        await page.getByTestId("way-out-raise_budget").click();
        await page.getByTestId("watch-off").waitFor({ state: "detached" });
        const post = posts.slice(before).find((p) => p.p === "/api/home/actions/raise_budget");
        ck("watcher-off: the button posts raise_budget with the amount", !!post && JSON.parse(post.body).amount_usd === 25, JSON.stringify(posts.slice(before)));
        ck("watcher-off: the panel turns into the watched layout", (await page.getByTestId("home-today").count()) === 1 && !/^Nobody/.test(await text(page.getByTestId("home-headline"))), await text(page.getByTestId("home-headline")));
        await shot(page, "sre-home-watcher-off-after-desktop.png");
      }
      if (state === "one-incident") {
        await page.getByTestId("incident-on-it").click();
        await page.getByTestId("incident-on-it").waitFor({ state: "detached" });
        ck("one-incident: I'm on it posts to the incident", posts.some((p) => /^\/api\/incidents\/.+\/on_it$/.test(p.p)), JSON.stringify(posts));
        ck("one-incident: the card says it is picked up and has no I'm on it", /on it/.test(await text(page.getByTestId("incident-age"))), await text(page.getByTestId("incident-age")));
        await page.getByTestId("incident-resolve").click();
        await page.getByTestId("incident-card").waitFor({ state: "detached" });
        ck("one-incident: Mark resolved posts to the incident", posts.some((p) => /^\/api\/incidents\/.+\/resolve$/.test(p.p)), JSON.stringify(posts));
        ck("one-incident: the headline is All quiet", (await text(page.getByTestId("home-headline"))) === "All quiet", await text(page.getByTestId("home-headline")));
        await page.waitForTimeout(6500); // the page reads again once the cell's short cache has gone
        ck("one-incident: and it stays so after the page reads again", (await page.getByTestId("incident-card").count()) === 0 && (await text(page.getByTestId("home-headline"))) === "All quiet", await text(page.getByTestId("home-headline")));
      }
      if (state === "all-quiet") {
        await page.goto(`${BASE}/start`, { waitUntil: "load" });
        await page.getByTestId("sre-home").waitFor();
        ck("all-quiet: /start lands on /", new URL(page.url()).pathname === "/", page.url());
      }
      await ctx.close();

    } finally {
      daemon.kill();
      await waitGone(`${BASE}/health`);
      fs.rmSync(home, { recursive: true, force: true });
    }
  }

  // ── the sample states: they need no data ─────────────────────────────────
  console.log("== sample states ==");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-home-e2e-sample-"));
  execFileSync(python, [path.join(__dirname, "seed-sre-home.py"), home, "new-cell"], { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } });
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env: cleanEnv(home, dist) });
  try {
    ck("sample: daemon up", await waitFor(`${BASE}/health`));
    const open = async (name, width) => {
      const { ctx, page } = await newPage(width, true);
      await page.goto(`${BASE}/?sample=${name}`, { waitUntil: "load" });
      await page.getByTestId("sre-home").or(page.getByTestId("home-loading")).first().waitFor();
      return { ctx, page };
    };

    {
      const { ctx, page } = await open("load", 1440);
      await page.waitForTimeout(400);
      ck("loading: the same two columns in skeleton", (await page.getByTestId("home-loading").count()) === 1 && (await page.getByTestId("home-main").count()) === 1 && (await page.getByTestId("home-aside").count()) === 1);
      await common(page, "loading", 1440);
      await shot(page, "sre-home-loading-desktop.png");
      await ctx.close();
      const n = await open("load", 390);
      await common(n.page, "loading", 390);
      await shot(n.page, "sre-home-loading-narrow.png");
      await n.ctx.close();
    }
    {
      const { ctx, page } = await open("error", 1440);
      await page.getByTestId("home-error").waitFor();
      const t = await text(page.getByTestId("home-error"));
      ck("error: one sentence of what happened and a Retry", /did not load/.test(t) && (await page.getByRole("button", { name: "Retry" }).count()) === 1, t);
      await common(page, "error", 1440);
      await shot(page, "sre-home-error-desktop.png");
      await ctx.close();
      const n = await open("error", 390);
      await n.page.getByTestId("home-error").waitFor();
      await common(n.page, "error", 390);
      await shot(n.page, "sre-home-error-narrow.png");
      await n.ctx.close();
    }
    {
      // a monthly budget: the date it starts again comes from the cell
      const { ctx, page } = await open("watcher-off-monthly", 1440);
      await page.getByTestId("watch-off").waitFor();
      ck("watcher-off-monthly: Spent is for the month and says 1 Nov", /Spent \$25\.00 of \$25 this month/.test(await text(page.getByTestId("watch-spent"))) && (await text(page.getByTestId("watch-resets"))) === "Starts again on 1 Nov", await text(page.getByTestId("watch-spent")));
      ck("watcher-off-monthly: Slack was told, where and when", /^Told #ops-alerts on 29 Oct, 03:00\.$/.test(await text(page.getByTestId("watch-told"))), await text(page.getByTestId("watch-told")));
      await common(page, "watcher-off-monthly", 1440);
      await shot(page, "sre-home-watcher-off-monthly-desktop.png");
      await page.getByTestId("way-out-raise_budget").click();
      await page.getByTestId("watch-off").waitFor({ state: "detached" });
      ck("watcher-off-monthly: the sample button turns it into the watched layout", (await page.getByTestId("home-today").count()) === 1);
      await ctx.close();
    }
    {
      const { ctx, page } = await open("many-incidents", 1440);
      await page.getByTestId("incident-card").first().waitFor();
      ck("many-incidents sample: three cards and 2 more", (await page.getByTestId("incident-card").count()) === 3 && (await text(page.getByTestId("incidents-more"))) === "2 more open");
      const picked = await page.getByTestId("incident-card").nth(1);
      ck("many-incidents sample: an incident someone has picked up has no I'm on it", (await picked.getByTestId("incident-on-it").count()) === 0 && /Alex is on it/.test(await text(picked.getByTestId("incident-age"))));
      await common(page, "many-incidents sample", 1440);
      await shot(page, "sre-home-many-incidents-sample-desktop.png");
      await ctx.close();
    }
    for (const [name, width] of [["long-text", 375], ["long-text", 1440], ["many-items", 1440], ["many-items", 390]]) {
      const { ctx, page } = await open(name, width);
      await page.getByTestId("home-headline").waitFor();
      await page.waitForLoadState("networkidle");
      if (name === "long-text") {
        ck(`long-text ${width}: the long name and cause are all there`, (await text(page.getByTestId("incident-cause"))).length > 250 && (await text(page.getByTestId("home-headline"))).includes("service-a-receiver-eu-central-1"));
      } else {
        ck(`many-items ${width}: ten services and a link to all 40`, (await page.getByTestId("service-row").count()) === 10 && /All 40 services/.test(await text(page.getByTestId("all-services"))), String(await page.getByTestId("service-row").count()));
        ck(`many-items ${width}: five blind spots and a button for the other seven`, (await page.getByTestId("blind-spot").count()) === 5 && (await text(page.getByTestId("blind-spots-toggle"))) === "Show 7 more", await text(page.getByTestId("blind-spots-toggle")));
        await page.getByTestId("blind-spots-toggle").click();
        ck(`many-items ${width}: Show more shows them all`, (await page.getByTestId("blind-spot").count()) === 12);
      }
      await common(page, name, width);
      await shot(page, `sre-home-${name}-${width === 375 ? "narrow" : width === 390 ? "narrow" : "desktop"}.png`);
      await ctx.close();
    }
    {
      // the sample "new-cell", an answer on a sample, and the address alone shows no sample on a live cell
      const { ctx, page } = await open("new-cell", 1440);
      ck("new-cell sample: the one Connect button", (await page.getByTestId("connect-link").count()) === 1);
      await ctx.close();
      const { ctx: c2, page: p2 } = await open("all-quiet", 1440);
      await p2.getByTestId("raised-row").first().getByTestId("worth-no").click();
      await p2.getByTestId("raised-row").first().getByTestId("raised-answer").waitFor();
      ck("all-quiet sample: No shows the answer", /not worth raising/.test(await text(p2.getByTestId("raised-row").first().getByTestId("raised-answer"))));
      await c2.close();
      const plain = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const p3 = await plain.newPage();
      await p3.goto(`${BASE}/?sample=all-quiet`, { waitUntil: "load" });
      await p3.getByTestId("home-headline").waitFor();
      ck("the sample address alone shows the live cell", (await text(p3.getByTestId("home-headline"))) === "No services yet");
      await plain.close();
    }
  } finally {
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(`screenshots: ${SHOTS}`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
