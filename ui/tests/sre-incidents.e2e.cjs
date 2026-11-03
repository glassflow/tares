/*
 * Browser e2e for TR-751: the AI SRE Incidents list (/incidents) and Incident page (/incidents/:id).
 *
 * The list is one read of the cell (GET /api/sre/incidents): Open, Someone on it and Resolved as
 * groups, counts on the tabs, a Service filter, 20 resolved at a time, and a quiet line with a link
 * to every finding. One incident is one read (GET /api/sre/incidents/:id): the status in words, the
 * errors and traffic chart with the deploy marks, the note (cut at about 260 px with a fade and a
 * "Read the whole note" button), what it read in time order, the Slack thread, who was on it, the
 * "worth raising" question, and later notes that joined. I'm on it, Mark resolved, Reopen and the
 * worth answer are real actions on the cell, and the page shows the result. Every state shows only
 * words from words.json, and none scrolls sideways at 390 px.
 *
 * Seeds a scratch AI SRE cell (tests/seed-sre-incidents.py, invented names), then runs the real daemon
 * on it, serving the built console. The sample states (?sample=<name>, with the sample flag in
 * localStorage) need no data and make no read.
 * Run:  npm run test:sre-incidents     (from ui/; or vite build, then node tests/sre-incidents.e2e.cjs)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 * TARES_E2E_PORT is the daemon port (default 8870); the MCP endpoint takes the next one.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8870);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const LIST_SAMPLES = ["list-normal", "list-empty", "list-long", "list-many", "list-loading", "list-error"];
const INC_SAMPLES = ["inc-open", "inc-onit", "inc-you", "inc-resolved", "inc-self", "inc-noslack", "inc-long",
  "inc-action-err", "inc-loading", "inc-error", "inc-missing"];
const LIST = "/api/sre/incidents";

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

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-incidents-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-incidents.py"), home], { cwd: REPO, encoding: "utf8" })
    .trim().split("\n").pop());
  ck("seeded the incidents", !!ids["open-a"] && !!ids["open-g"] && !!ids["onit-b"] && !!ids["res-c"] && !!ids["res-d"] && !!ids["res-e"] && !!ids["open-l"]);

  const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO };   // run the code under test, not an installed copy
  for (const k of ["TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET", "TARES_SLACK_CONNECT_URL", "TARES_AUTH_TOKEN", "TARES_DB",
    "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL",
    "TARES_FACTORY_MCP_URL", "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE"]) delete env[k];
  const daemon = spawn(python, ["-c", `import sys; from tares.cli import main; sys.argv = ["tares", "up", "--port", "${PORT}", "--data-dir", ${JSON.stringify(home)}]; main()`],
    { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, sampleFlag = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, timezoneId: "UTC", locale: "en-GB" });
      if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const txt = async (loc) => ((await loc.first().innerText()) || "").replace(/\s+/g, " ").trim();
    const api = async (p) => (await fetch(`${BASE}${p}`)).json();
    const goto = async (page, p) => { await page.goto(`${BASE}${p}`, { waitUntil: "load" }); };
    const incUrl = (k) => `/incidents/${ids[k]}`;
    const incApi = (k) => `${LIST}/${ids[k]}`;
    const sampleReads = (page) => {
      const reads = [];
      page.on("request", (r) => { const p = new URL(r.url()).pathname; if (r.method() === "GET" && p.startsWith("/api/") && p !== "/api/whoami") reads.push(p); });
      return reads;
    };
    // an incident page, loaded by its one read, then waited on until the status shows
    const openIncident = async (page, key, label, tag) => {
      const one = expectOneCall(page, incApi(key));
      await goto(page, incUrl(key));
      await page.getByTestId("status-line").waitFor();
      await one(ck, label);
      if (tag) ck(`${label}: the tag says ${tag}`, (await txt(page.getByTestId("incident-tag"))).toLowerCase() === tag.toLowerCase(), await txt(page.getByTestId("incident-tag")));
    };
    const statusIs = async (page, re, label) => {
      await page.waitForFunction(([id, src]) => new RegExp(src).test(document.querySelector('[data-testid="status-line"]')?.textContent || ""),
        [null, re.source], { timeout: 8000 }).catch(() => {});
      const t = await txt(page.getByTestId("status-line"));
      ck(label, re.test(t), t);
    };

    // ============================ the list, live ============================
    console.log("== the list, live ==");
    const { page } = await newPage(1440);
    const rows = page.getByTestId("incident-row");
    const tab = (k) => page.getByTestId(`tab-${k}`);
    const titles = () => page.getByTestId("incident-title").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const rowOf = (k) => page.locator(`[data-testid=incident-row][data-id="${ids[k]}"]`);
    const one = expectOneCall(page, LIST);
    await goto(page, "/incidents");
    await rows.first().waitFor();
    await one(ck, "/incidents");
    ck("the page has its heading", await page.getByRole("heading", { name: "Incidents", exact: true }).isVisible());
    ck("the tabs: All, Open, Someone on it, Resolved",
       JSON.stringify(await page.getByRole("tab").evaluateAll((t) => t.map((x) => x.firstChild.textContent.trim()))) === JSON.stringify(["All", "Open", "Someone on it", "Resolved"]));
    const counts = await Promise.all(["all", "open", "on_it", "resolved"].map((k) => txt(page.getByTestId(`tab-count-${k}`))));
    ck("each tab has its count (29, 3, 1, 25)", JSON.stringify(counts) === JSON.stringify(["29", "3", "1", "25"]), counts.join(","));
    ck("All is the chosen tab", (await tab("all").getAttribute("aria-selected")) === "true");
    ck("the groups are Open, Someone on it, Resolved", JSON.stringify(await page.locator("[data-testid^=group-]").evaluateAll((g) => g.map((x) => x.dataset.testid))) === JSON.stringify(["group-open", "group-on_it", "group-resolved"]));
    ck("open: the newest note first, 3 rows", JSON.stringify(await page.locator("[data-testid=incident-row][data-state=open]").evaluateAll((r) => r.map((x) => x.dataset.id))) === JSON.stringify([ids["open-g"], ids["open-a"], ids["open-l"]]));
    ck("20 resolved on the first page", (await page.locator("[data-testid=incident-row][data-state=resolved]").count()) === 20);
    ck("an open row says how long it has been open", /^open \d+ minutes?, nobody on it$/i.test(await txt(rowOf("open-a").getByTestId("incident-words"))), await txt(rowOf("open-a").getByTestId("incident-words")));
    ck("a row names the person on it", /^Alex is on it since /.test(await txt(rowOf("onit-b").getByTestId("incident-words"))), await txt(rowOf("onit-b").getByTestId("incident-words")));
    ck("a row says who resolved it", /^resolved by Alex, 2 days ago$/i.test(await txt(rowOf("res-c").getByTestId("incident-words"))), await txt(rowOf("res-c").getByTestId("incident-words")));
    ck("a row says it resolved itself", /^resolved itself after 12 minutes$/i.test(await txt(rowOf("res-d").getByTestId("incident-words"))), await txt(rowOf("res-d").getByTestId("incident-words")));
    ck("a row links to the incident", (await rowOf("open-a").getAttribute("href")) === incUrl("open-a"), String(await rowOf("open-a").getAttribute("href")));
    ck("the sub line counts the states", /3 open, 1 someone on it, 25 resolved/.test(await txt(page.getByTestId("incidents-sub"))), await txt(page.getByTestId("incidents-sub")));
    ck("the quiet line links to every finding", (await page.getByTestId("show-all-findings").count()) === 1 && (await txt(page.getByTestId("show-all-findings"))) === "Show all findings", await txt(page.getByTestId("show-all-findings")));
    await checkWords(page, ck, "list");
    await shot(page, "live-list-1440.png");

    // paging: 20 more
    ck("Show 20 more is offered", (await page.getByTestId("show-more").count()) === 1 && (await txt(page.getByTestId("show-more"))) === "Show 20 more", await txt(page.getByTestId("show-more")));
    await page.getByTestId("show-more").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row][data-state=resolved]").length === 25);
    ck("Show 20 more adds the other 5", (await page.locator("[data-testid=incident-row][data-state=resolved]").count()) === 25);
    ck("and the button goes", (await page.getByTestId("show-more").count()) === 0);
    ck("none repeats", new Set(await rows.evaluateAll((r) => r.map((x) => x.dataset.id))).size === 29);

    // the tabs filter, with the filter in the address
    await tab("open").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row]").length === 3);
    ck("Open shows 3 rows", (await rows.count()) === 3);
    ck("the address carries the state", new URL(page.url()).searchParams.get("state") === "open", page.url());
    await tab("resolved").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row]").length >= 20);
    ck("Resolved shows only resolved rows", (await page.locator("[data-testid=incident-row]:not([data-state=resolved])").count()) === 0);
    await tab("on_it").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row]").length === 1);
    ck("Someone on it shows the one row", (await rowOf("onit-b").count()) === 1);
    await tab("all").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row]").length >= 23);
    ck("All brings every group back", (await page.locator("[data-testid^=group-]").count()) === 3);

    // the service filter
    await page.getByRole("combobox", { name: "Service" }).click();
    await page.getByRole("option", { name: "docs-site", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row]").length === 2);
    ck("the service filter keeps that service's incidents", (await rowOf("open-l").count()) === 1 && (await rowOf("onit-b").count()) === 1);
    ck("the address carries the service", new URL(page.url()).searchParams.get("service") === "docs-site", page.url());
    const fcounts = await Promise.all(["all", "open", "on_it", "resolved"].map((k) => txt(page.getByTestId(`tab-count-${k}`))));
    ck("the counts follow the filter (2 in all, 1 open, 1 someone on it, 0 resolved)", JSON.stringify(fcounts) === JSON.stringify(["2", "1", "1", "0"]), fcounts.join(","));
    await shot(page, "live-list-filtered-1440.png");
    await goto(page, "/incidents?service=nothing-here");
    await page.getByTestId("incidents-none").waitFor();
    ck("a filter with no match says so", /No incident matches this filter/.test(await txt(page.getByTestId("incidents-none"))));

    // every finding
    await goto(page, "/incidents");
    await rows.first().waitFor();
    await page.getByTestId("show-all-findings").click();
    await page.getByTestId("group-raised").waitFor();
    ck("Show all findings adds Raised, not an incident", (await page.locator("[data-testid=group-raised] [data-testid=incident-row]").count()) === 1);
    ck("a finding that is no incident is not a link", (await page.locator("[data-testid=group-raised] [data-testid=incident-row]").evaluate((e) => e.tagName)) !== "A");
    ck("it says so", /Raised once, did not repeat/.test(await txt(page.getByTestId("group-raised"))));
    ck("the link turns into Show incidents only", /Show incidents only/.test(await txt(page.getByTestId("show-all-findings"))));
    ck("the address carries show=all", new URL(page.url()).searchParams.get("show") === "all", page.url());
    await checkWords(page, ck, "list, every finding");
    await shot(page, "live-list-showall-1440.png");
    await page.getByTestId("show-all-findings").click();
    await page.waitForFunction(() => !document.querySelector("[data-testid=group-raised]"));
    ck("Show incidents only takes it away again", (await page.getByTestId("group-raised").count()) === 0);

    // an error, then back
    {
      const { page: p } = await newPage(1440);
      let broken = true;
      await p.route((u) => u.pathname === LIST, (r) => (broken ? r.fulfill({ status: 500, body: "{}" }) : r.continue()));
      await goto(p, "/incidents");
      await p.getByTestId("incidents-error").waitFor();
      const t = await txt(p.getByTestId("incidents-error"));
      ck("a failed read says the incidents did not load and that it will try again in 20 seconds", /The incidents did not load\./.test(t) && /Tares could not read them just now\. It will try again in 20 seconds\./.test(t), t);
      await checkWords(p, ck, "list, error");
      await shot(p, "live-list-error-1440.png");
      broken = false;
      await p.getByTestId("incidents-retry").click();
      await p.getByTestId("incident-row").first().waitFor();
      ck("Try now loads the list", (await p.getByTestId("incident-row").count()) >= 3);
      ck("and the error goes", (await p.getByTestId("incidents-error").count()) === 0);
    }

    // it reads again every 20 seconds
    {
      const { page: p } = await newPage(1440);
      const reads = [];
      p.on("request", (r) => { if (r.method() === "GET" && new URL(r.url()).pathname === LIST) reads.push(Date.now()); });
      await p.clock.install();
      await goto(p, "/incidents");
      await p.getByTestId("incident-row").first().waitFor();
      const first = reads.length;
      await p.clock.runFor(19000);
      await p.waitForTimeout(300);
      ck("it does not read again before 20 seconds", reads.length === first, `${reads.length} vs ${first}`);
      await p.clock.runFor(2000);
      await p.waitForTimeout(500);
      ck("it reads again after 20 seconds", reads.length === first + 1, `${reads.length} vs ${first}`);
    }

    // ============================ one incident, live ============================
    console.log("== one incident, live ==");
    await openIncident(page, "open-a", "open incident", "Open");
    ck("the title is the incident's", (await txt(page.getByTestId("incident-title"))).startsWith("edge-proxy:"), await txt(page.getByTestId("incident-title")));
    ck("the service chip names the service", (await txt(page.getByTestId("incident-service"))) === "edge-proxy");
    ck("the back link goes to the list", (await page.getByTestId("back").getAttribute("href")) === "/incidents");
    await statusIs(page, /^Open, nobody on it yet\. It has waited .+\.$/, "the status says how long it has waited");
    ck("the buttons: I'm on it and Mark resolved", (await txt(page.getByTestId("act-on_it"))) === "I'm on it" && (await txt(page.getByTestId("act-resolve"))) === "Mark resolved" && (await page.getByTestId("act-reopen").count()) === 0);
    ck("the chart is titled Errors and traffic", /Errors and traffic/.test(await txt(page.getByTestId("chart"))), await txt(page.getByTestId("chart")));
    ck("the chart draws", (await page.getByTestId("chart").locator("svg").count()) >= 1);
    const note = await txt(page.getByTestId("note"));
    ck("the note shows what is failing, the likely cause and the next step", /What is failing/.test(note) && /edge-proxy returns 502 on \/ui/.test(note) && /Likely cause/.test(note) && /Next step/.test(note) && /Roll the deploy back/.test(note), note);
    ck("a short note is not cut", (await page.getByTestId("note-toggle").count()) === 0 && (await page.getByTestId("note-fade").count()) === 0);
    const ev = await page.getByTestId("evidence-row").allInnerTexts();
    const evTimes = ev.map((e) => e.trim().slice(0, 5));
    ck("What it read lists 3 steps in time order", ev.length === 3 && JSON.stringify(evTimes) === JSON.stringify([...evTimes].sort()), JSON.stringify(evTimes));
    ck("the Slack section names the place and links nowhere without a link from Slack", /The note was posted in Slack\./.test(await txt(page.getByTestId("slack"))) && (await page.getByTestId("slack-link").count()) === 0, await txt(page.getByTestId("slack")));
    ck("Who was on it says nobody yet", (await txt(page.getByTestId("who-line"))) === "Nobody yet.", await txt(page.getByTestId("who-line")));
    ck("one later note joined", (await page.getByTestId("joined-row").count()) === 1 && /It did not start a new one/.test(await txt(page.getByTestId("joined-row"))));
    ck("the worth question is asked", (await page.getByTestId("worth-yes").count()) === 1 && (await page.getByTestId("worth-no").count()) === 1 && /Was this worth raising\?/.test(await txt(page.getByTestId("worth"))), await txt(page.getByTestId("worth")));
    // the page puts What it read in time order even when the cell sends it the other way round
    const flip = (r) => r.fetch().then(async (resp) => { const j = await resp.json(); j.evidence = [...j.evidence].reverse(); return r.fulfill({ response: resp, json: j }); });
    const isOpenA = (u) => u.pathname === incApi("open-a");
    await page.route(isOpenA, flip);
    await page.reload({ waitUntil: "load" });
    await page.getByTestId("evidence-row").first().waitFor();
    const flipped = (await page.getByTestId("evidence-row").allInnerTexts()).map((e) => e.trim().slice(0, 5));
    ck("What it read stays in time order when the cell sends it reversed", JSON.stringify(flipped) === JSON.stringify([...flipped].sort()) && flipped.length === 3, JSON.stringify(flipped));
    await page.unroute(isOpenA, flip);
    await checkWords(page, ck, "open incident");
    await shot(page, "live-inc-open-1440.png");

    // worth: yes, then change to no
    await page.getByTestId("worth-yes").click();
    await page.getByTestId("worth-answer").waitFor();
    ck("worth yes: the page says You answered yes", /You answered yes\. Thank you\./.test(await txt(page.getByTestId("worth-answer"))), await txt(page.getByTestId("worth-answer")));
    ck("worth yes: it is saved on the cell", (await api(incApi("open-a"))).worth.answer === true);
    await page.getByTestId("worth-change").click();
    await page.getByTestId("worth-no").click();
    await page.waitForFunction(() => /answered no/.test(document.querySelector('[data-testid="worth-answer"]')?.textContent || ""));
    ck("worth no: the page says You answered no", /You answered no\. Thank you\./.test(await txt(page.getByTestId("worth-answer"))), await txt(page.getByTestId("worth-answer")));
    ck("worth no: it is saved on the cell", (await api(incApi("open-a"))).worth.answer === false);

    // I'm on it, Mark resolved, Reopen
    await openIncident(page, "open-g", "open incident, not raised", "Open");
    ck("a note nobody raised has no worth question", (await page.getByTestId("worth").count()) === 0);
    ck("and no Slack section", (await page.getByTestId("slack").count()) === 0);
    await page.getByTestId("act-on_it").click();
    await statusIs(page, /^You are on it since /, "I'm on it: the status says You are on it");
    ck("I'm on it: the tag says Someone on it", (await txt(page.getByTestId("incident-tag"))) === "Someone on it" || /someone on it/i.test(await txt(page.getByTestId("incident-tag"))), await txt(page.getByTestId("incident-tag")));
    ck("I'm on it: Who was on it says You", /^You, since /.test(await txt(page.getByTestId("who-line"))), await txt(page.getByTestId("who-line")));
    ck("I'm on it: it is saved on the cell", (await api(incApi("open-g"))).state === "on_it");
    ck("I'm on it: Mark resolved is the button now, and I'm on it is gone", (await page.getByTestId("act-resolve").count()) === 1 && (await page.getByTestId("act-on_it").count()) === 0);
    await shot(page, "live-inc-you-1440.png");
    await page.getByTestId("act-resolve").click();
    await statusIs(page, /^Resolved by you, /, "Mark resolved: the status says Resolved by you");
    ck("Mark resolved: the tag says Resolved", /resolved/i.test(await txt(page.getByTestId("incident-tag"))));
    ck("Mark resolved: Reopen is the button", (await page.getByTestId("act-reopen").count()) === 1 && (await page.getByTestId("act-resolve").count()) === 0);
    ck("Mark resolved: it is saved on the cell", (await api(incApi("open-g"))).state === "resolved");
    await page.getByTestId("act-reopen").click();
    await statusIs(page, /^Open, nobody on it yet\./, "Reopen: the status is Open again");
    ck("Reopen: I'm on it is back", (await page.getByTestId("act-on_it").count()) === 1);
    ck("Reopen: it is saved on the cell", (await api(incApi("open-g"))).state === "open");

    await openIncident(page, "onit-b", "incident someone is on", "Someone on it");
    await statusIs(page, /^Alex is on it since /, "the status names Alex");
    ck("Who was on it names Alex", /^Alex, since /.test(await txt(page.getByTestId("who-line"))), await txt(page.getByTestId("who-line")));
    ck("the buttons: Mark resolved only, no I'm on it for an incident Alex holds", (await txt(page.getByTestId("act-resolve"))) === "Mark resolved" && (await page.getByTestId("act-on_it").count()) === 0);
    await shot(page, "live-inc-onit-1440.png");
    ck("the holder is still Alex on the cell", (await api(incApi("onit-b"))).who.name === "Alex");

    await openIncident(page, "res-c", "incident resolved by a person", "Resolved");
    await statusIs(page, /^Resolved by Alex, 2 days ago, after 1 hour\.$/, "the status says who, when and after how long");
    ck("Who was on it says Alex resolved it", (await txt(page.getByTestId("who-line"))) === "Alex resolved it.", await txt(page.getByTestId("who-line")));
    ck("the Reopen button says Reopen", (await txt(page.getByTestId("act-reopen"))) === "Reopen");
    ck("the only button is Reopen", (await page.getByTestId("act-reopen").count()) === 1 && (await page.getByTestId("act-resolve").count()) === 0 && (await page.getByTestId("act-on_it").count()) === 0);
    ck("a resolved incident still offers the worth question", (await page.getByTestId("worth-yes").count()) === 1);
    await shot(page, "live-inc-resolved-1440.png");

    await openIncident(page, "res-d", "incident that resolved itself", "Resolved");
    await statusIs(page, /^Resolved itself after 12 minutes\. Nobody had to act\.$/, "the status says it resolved itself");
    ck("Who was on it says nobody (no name from another incident)", (await txt(page.getByTestId("who-line"))) === "Nobody was on it.", await txt(page.getByTestId("who-line")));
    ck("a note-less incident has no worth question", (await page.getByTestId("worth").count()) === 0);
    await shot(page, "live-inc-self-1440.png");

    await openIncident(page, "res-e", "incident from before tracking", "Resolved");
    await statusIs(page, /^Closed: from before incidents were tracked\.$/, "the status says it was closed from before tracking");
    await shot(page, "live-inc-before-1440.png");

    // the long note
    await openIncident(page, "open-l", "incident with a long note", "Open");
    await page.getByTestId("note-toggle").waitFor();
    const clipH = await page.getByTestId("note-body").evaluate((e) => e.getBoundingClientRect().height);
    ck("a long note is cut at about 260 px", clipH >= 240 && clipH <= 270, String(clipH));
    ck("with a fade", (await page.getByTestId("note-fade").count()) === 1);
    ck("and a Read the whole note button", (await txt(page.getByTestId("note-toggle"))) === "Read the whole note", await txt(page.getByTestId("note-toggle")));
    ck("the note shows its first heading", /What happened/.test(await txt(page.getByTestId("note"))));
    await checkWords(page, ck, "incident, long note");
    await shot(page, "live-inc-long-1440.png");
    await page.getByTestId("note-toggle").click();
    await page.waitForFunction(() => (document.querySelector('[data-testid="note-body"]')?.getBoundingClientRect().height || 0) > 300);
    ck("Read the whole note shows all of it", (await page.getByTestId("note-body").evaluate((e) => e.getBoundingClientRect().height)) > 300 && /Ask the registry owner/.test(await txt(page.getByTestId("note"))));
    ck("and the fade goes", (await page.getByTestId("note-fade").count()) === 0);
    ck("the button says Show less", (await txt(page.getByTestId("note-toggle"))) === "Show less");
    await page.getByTestId("note-toggle").click();
    await page.waitForFunction(() => (document.querySelector('[data-testid="note-body"]')?.getBoundingClientRect().height || 0) < 280);
    ck("Show less cuts it again", (await page.getByTestId("note-fade").count()) === 1);

    // missing, error
    {
      const m = expectOneCall(page, `${LIST}/inc_nope`);
      await goto(page, "/incidents/inc_nope");
      await page.getByTestId("incident-missing").waitFor();
      await m(ck, "missing incident");
      ck("a missing incident says so", /No incident with that id/.test(await txt(page.getByTestId("incident-missing"))), await txt(page.getByTestId("incident-missing")));
      ck("and links back to all incidents", /Back to all incidents/.test(await txt(page.getByTestId("incident-missing"))));
      await checkWords(page, ck, "incident, missing");
      await shot(page, "live-inc-missing-1440.png");
    }
    {
      const { page: p } = await newPage(1440);
      let broken = true;
      await p.route((u) => u.pathname === incApi("open-a"), (r) => (broken ? r.fulfill({ status: 500, body: "{}" }) : r.continue()));
      await goto(p, incUrl("open-a"));
      await p.getByTestId("incident-error").waitFor();
      const t = await txt(p.getByTestId("incident-error"));
      ck("a failed read says the incident did not load and that it will try again in 20 seconds", /did not load/.test(t) && /try again in 20 seconds/.test(t), t);
      await checkWords(p, ck, "incident, error");
      broken = false;
      await p.getByTestId("incident-retry").click();
      await p.getByTestId("status-line").waitFor();
      ck("Try now loads the incident", (await p.getByTestId("incident-error").count()) === 0);
    }
    {
      const { page: p } = await newPage(1440);
      let broken = false;
      await p.route((u) => u.pathname === `${incApi("open-a")}/on_it`, (r) => (broken ? r.fulfill({ status: 500, body: "{}" }) : r.continue()));
      await goto(p, incUrl("open-a"));
      await p.getByTestId("status-line").waitFor();
      broken = true;
      await p.getByTestId("act-on_it").click();
      await p.getByText("That did not save. Try again in a moment.").waitFor({ timeout: 5000 }).catch(() => {});
      ck("an action that fails says it did not save", (await p.getByText("That did not save. Try again in a moment.").count()) >= 1);
      ck("and the status does not change", /^Open, nobody on it yet/.test(await txt(p.getByTestId("status-line"))));
    }

    // ============================ 390 px, live ============================
    console.log("== 390 px, live ==");
    {
      const { page: p } = await newPage(390);
      await goto(p, "/incidents");
      await p.getByTestId("incident-row").first().waitFor();
      ck("390: the list does not scroll sideways", (await overflow(p)) <= 0, String(await overflow(p)));
      ck("390: the tabs wrap instead of scrolling", await p.getByRole("tablist").evaluate((e) => e.scrollWidth <= e.clientWidth + 1));
      await checkWords(p, ck, "390 list");
      await shot(p, "live-list-390.png");
      for (const [k, name] of [["open-a", "open"], ["open-l", "long"], ["onit-b", "onit"], ["res-c", "resolved"], ["res-d", "self"], ["res-e", "before"]]) {
        await goto(p, incUrl(k));
        await p.getByTestId("status-line").waitFor();
        await p.getByTestId("chart").waitFor();
        ck(`390: ${name} does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, `390 ${name}`);
        await shot(p, `live-inc-${name}-390.png`);
      }
      await goto(p, "/incidents/inc_nope");
      await p.getByTestId("incident-missing").waitFor();
      ck("390: a missing incident does not scroll sideways", (await overflow(p)) <= 0, String(await overflow(p)));
    }

    // ============================ the sample states ============================
    console.log("== the sample states ==");
    for (const width of [1440, 390]) {
      const { page: p } = await newPage(width, true);
      const reads = sampleReads(p);
      const label = (n) => `${n} @${width}`;
      const done = async (n, extra) => {
        ck(`${label(n)}: does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, label(n));
        await shot(p, `${n}-${width}.png`);
        if (extra) await extra();
      };
      for (const n of LIST_SAMPLES) {
        await goto(p, `/incidents?sample=${n}`);
        if (n === "list-loading") {
          await p.getByTestId("incidents-page").waitFor();
          ck(label(n) + ": shows the loading state", (await p.locator("[data-testid=incidents-page][data-loading]").count()) === 1);
          ck(label(n) + ": with placeholder rows", (await p.locator("[data-testid=incidents-page][data-loading] [role=status] > div").count()) >= 3);
        } else if (n === "list-error") {
          await p.getByTestId("incidents-error").waitFor();
          ck(label(n) + ": says it will try again in 20 seconds", /Tares could not read them just now\. It will try again in 20 seconds\./.test(await txt(p.getByTestId("incidents-error"))));
        } else if (n === "list-empty") {
          await p.getByTestId("incidents-empty").waitFor();
          ck(label(n) + ": says there are no incidents", /No incidents/.test(await txt(p.getByTestId("incidents-empty"))) && /The watcher has raised nothing that needed a note\./.test(await txt(p.getByTestId("incidents-empty"))));
          ck(label(n) + ": has no tabs", (await p.getByRole("tab").count()) === 0);
          ck(label(n) + ": no quiet checks, yet the way to every finding is there", (await p.getByTestId("quiet-words").count()) === 0 && (await txt(p.getByTestId("show-all-findings"))) === "Show all findings");
          await p.getByTestId("show-all-findings").click();
          await p.waitForFunction(() => /Show incidents only/.test(document.querySelector("[data-testid=show-all-findings]")?.textContent || ""));
          ck(label(n) + ": and the way back stays", new URL(p.url()).searchParams.get("show") === "all" && (await txt(p.getByTestId("show-all-findings"))) === "Show incidents only");
        } else {
          await p.getByTestId("incident-row").first().waitFor();
          if (n === "list-normal") {
            ck(label(n) + ": the three groups", JSON.stringify(await p.locator("[data-testid^=group-]").evaluateAll((g) => g.map((x) => x.dataset.testid))) === JSON.stringify(["group-open", "group-on_it", "group-resolved"]));
            ck(label(n) + ": the quiet line is there", (await p.getByTestId("quiet-line").count()) === 1);
          }
          if (n === "list-many") {
            const whens = await p.locator("[data-testid=incident-row][data-state=resolved] [data-testid=incident-when]").allInnerTexts();
            ck(label(n) + ": 20 resolved rows with different times", whens.length === 20 && new Set(whens.map((w) => w.trim())).size >= 10, `${whens.length} rows, ${new Set(whens).size} different`);
            await p.getByTestId("show-more").click();
            await p.waitForFunction(() => document.querySelectorAll("[data-testid=incident-row][data-state=resolved]").length === 40);
            ck(label(n) + ": Show 20 more makes 40", (await p.locator("[data-testid=incident-row][data-state=resolved]").count()) === 40);
          }
        }
        await done(n);
      }
      for (const n of INC_SAMPLES) {
        await goto(p, `/incidents/inc_sample?sample=${n}`);
        if (n === "inc-loading") { await p.getByTestId("incident-loading").waitFor(); }
        else if (n === "inc-error") {
          await p.getByTestId("incident-error").waitFor();
          ck(label(n) + ": says it will try again in 20 seconds", /try again in 20 seconds/.test(await txt(p.getByTestId("incident-error"))));
        } else if (n === "inc-missing") {
          await p.getByTestId("incident-missing").waitFor();
          ck(label(n) + ": says there is no such incident", /No incident with that id/.test(await txt(p.getByTestId("incident-missing"))));
        } else {
          await p.getByTestId("status-line").waitFor();
          await p.getByTestId("chart").waitFor();
          const status = await txt(p.getByTestId("status-line"));
          const who = await txt(p.getByTestId("who-line"));
          const opened = await txt(p.getByTestId("incident-opened"));
          const evRows = await p.getByTestId("evidence-row").allInnerTexts();
          const evT = evRows.map((e) => e.trim().slice(0, 5));
          if (evRows.length > 1) ck(label(n) + ": What it read is in time order", JSON.stringify(evT) === JSON.stringify([...evT].sort()), evT.join(","));
          ck(label(n) + ": the chart has errors, traffic and the deploy mark", /Errors and traffic/.test(await txt(p.getByTestId("chart"))));
          if (n === "inc-open") ck(label(n) + ": open, nobody on it", /^Open, nobody on it yet\./.test(status) && who === "Nobody yet.", status);
          if (n === "inc-onit") ck(label(n) + ": Alex is on it", /^Alex is on it since /.test(status) && /^Alex, since /.test(who), `${status} / ${who}`);
          if (n === "inc-you") {
            ck(label(n) + ": You are on it", /^You are on it since /.test(status) && /^You, since /.test(who), `${status} / ${who}`);
            ck(label(n) + ": two later notes joined", (await p.getByTestId("joined-row").count()) === 2);
            ck(label(n) + ": no I'm on it too for me", (await p.getByTestId("act-on_it").count()) === 0 && (await p.getByTestId("act-resolve").count()) === 1);
          }
          if (n === "inc-resolved") {
            ck(label(n) + ": resolved by Alex, with how long it took", /^Resolved by Alex, .* ago, after 1 hour 4 minutes\.$/.test(status), status);
            ck(label(n) + ": it did not open today and resolve 2 days ago", !/today/.test(opened) && /Resolved by Alex, 2 days ago/.test(status), `${opened} / ${status}`);
            ck(label(n) + ": the worth answer is shown", /You answered no\. Thank you\./.test(await txt(p.getByTestId("worth-answer"))));
            ck(label(n) + ": the Slack thread links out", (await p.getByTestId("slack-link").count()) === 1 && (await p.getByTestId("slack-link").getAttribute("target")) === "_blank");
          }
          if (n === "inc-self") {
            ck(label(n) + ": resolved itself", /^Resolved itself after 12 minutes\. Nobody had to act\.$/.test(status), status);
            ck(label(n) + ": nobody was on it, and no name", who === "Nobody was on it." && !/Alex/.test(who), who);
          }
          if (n === "inc-noslack") ck(label(n) + ": no Slack section", (await p.getByTestId("slack").count()) === 0);
          if (n === "inc-long") {
            await p.getByTestId("note-toggle").waitFor();
            ck(label(n) + ": the long note is cut with a fade and a button", (await p.getByTestId("note-fade").count()) === 1 && (await txt(p.getByTestId("note-toggle"))) === "Read the whole note");
            ck(label(n) + ": it is cut at about 260 px", await p.getByTestId("note-body").evaluate((e) => { const h = e.getBoundingClientRect().height; return h >= 240 && h <= 270; }));
          }
          if (n === "inc-action-err") {
            await p.getByTestId("act-on_it").click();
            await p.getByText("Your key can read but not change incidents.").waitFor({ timeout: 5000 }).catch(() => {});
            ck(label(n) + ": a read-only key says so", (await p.getByText("Your key can read but not change incidents.").count()) >= 1);
          }
        }
        await done(n);
      }
      ck(`${width}: the samples make no read of the cell`, reads.filter((r) => r.startsWith(LIST)).length === 0, reads.join(","));
    }

    // the address alone shows no sample on a live cell
    {
      const { page: p } = await newPage(1440);
      await goto(p, "/incidents?sample=list-empty");
      await p.getByTestId("incident-row").first().waitFor();
      ck("the sample address alone shows the live cell", (await p.getByTestId("incidents-empty").count()) === 0);
    }
  } finally {
    if (browser) await browser.close().catch(() => {});
    daemon.kill();
    try { fs.rmSync(home, { recursive: true, force: true }); } catch (e) { /* none */ }
  }
  console.log(`\n${pass} ok, ${fail} FAIL`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
