/*
 * Browser e2e for TR-736: the factory Crew page (/crew).
 *
 * The page shows who is on the crew by role (the one you talk to, the reviewer, the releaser, the
 * builders of each project), what each is doing, how heard from, how full its memory is, how far the
 * crew may go, the Claude plan, the rules the reviewer proposes (accept or reject from here) and the
 * standup. "Show what it did" opens a member's lines, its terminal command and a way to ask for a new
 * memory reading; those are read only when the fold is opened. No station name shows outside the fold.
 * Each project lists its running builders first and hides the finished ones behind "Show N finished";
 * its header reads running and total.
 * Raw HTML in a line or a rule is shown as text. Pacing shows as a sentence. At 390 px nothing scrolls
 * sideways. Every state shows only words from words.json (the crew's own words are exempt).
 *
 * Seeds a scratch factory cell (tests/seed-factory-crew.py, invented names), then runs the real daemon
 * on it, serving the built console. The sample states (?sample=<name>, with the sample flag in
 * localStorage) need no data.
 * Run:  npm run build && node tests/factory-crew.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8851);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["busy", "fold", "finished", "l3", "pace", "quiet", "none", "many", "long", "team", "load", "err"];

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

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-crew-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-crew.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded the projects and the crew", !!ids["project-x"] && !!ids["project-y"] && !!ids["project-d"]);

  const env = { ...process.env, TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, sampleFlag = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };
    const member = (page, text) => page.getByTestId("crew-member").filter({ hasText: text });
    const heads = (page) => page.locator("[data-testid=factory-crew] h2").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const api = (p, init) => fetch(BASE + p, { headers: { "content-type": "application/json" }, ...init });

    const { ctx, page } = await newPage(1440);

    // 1. one read call; the page, in the order of the mock
    const one = expectOneCall(page, "/api/factory/crew");
    const detailCalls = [];
    page.on("request", (r) => { if (/\/api\/crew\//.test(r.url())) detailCalls.push(`${r.method()} ${new URL(r.url()).pathname}${new URL(r.url()).search}`); });
    await page.goto(`${BASE}/crew`, { waitUntil: "load" });
    await page.getByTestId("crew-builders").waitFor();
    await settle(page);
    await one(ck, "/crew");
    ck("the page has its heading", await page.getByRole("heading", { name: "Crew", exact: true }).isVisible());
    ck("the sections, in order", JSON.stringify(await heads(page)) === JSON.stringify(["Orchestrator", "Reviewer", "Releaser", "Builders",
       "Rules the crew proposes", "Standup", "Autonomy", "Claude plan"]), JSON.stringify(await heads(page)));
    ck("the summary says the crew works and when it was heard", /^Working, last heard \d+ seconds? ago$/.test((await page.getByTestId("crew-summary").innerText()).trim()),
       await page.getByTestId("crew-summary").innerText());
    ck("the menu has Crew, and it is the current one",
       (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "Crew");

    // 2. members
    const lead = page.getByTestId("crew-member").first();
    ck("the one you talk to: working, heard seconds ago, memory 82% full",
       (await lead.getAttribute("data-state")) === "working" && /Orchestrator · Working/.test(await lead.innerText())
       && /Heard \d+ seconds? ago/.test(await lead.innerText()) && /Memory 82% full/.test(await lead.innerText()), await lead.innerText());
    ck("the reviewer is idle", /Reviewer · Idle for 1 minute/.test(await member(page, "Reviewer").first().innerText()), await member(page, "Reviewer").first().innerText());
    ck("the releaser is idle for two hours", /Releaser · Idle for 2 hours/.test(await member(page, "Releaser").first().innerText()));
    const groups = await page.getByTestId("crew-builders").evaluate((s) => [...s.querySelectorAll("[data-user-text]")].filter((e) => e.parentElement.className.includes("wash-2"))
      .map((e) => [e.textContent, e.nextElementSibling.textContent]));
    ck("builders sit in their projects, each with running and total; the finished project's builder is left out",
       JSON.stringify(groups) === JSON.stringify([["project-x", "2 running · 3"], ["project-y", "1 running · 1"]]), JSON.stringify(groups));
    ck("the count says how many are on the crew now", /3 on the crew now/.test(await page.getByTestId("crew-builders").innerText()));
    const m1 = member(page, "Working on Footer");
    ck("a working builder with a helper", (await m1.count()) === 1 && /1 helper/.test(await m1.innerText()) && (await m1.getAttribute("data-state")) === "working", await m1.innerText());
    const m2 = member(page, "Waiting for the review of Search box");
    ck("an idle builder says what it waits for", (await m2.count()) === 1 && /Idle for 8 minutes/.test(await m2.innerText()));
    const mold = page.getByTestId("crew-member").and(page.locator("[data-state=ended]"));
    const finishedFold = page.getByTestId("crew-finished-fold");
    ck("an ended builder is hidden behind Show 1 finished", (await mold.count()) === 0 && (await finishedFold.count()) === 1
       && (await finishedFold.innerText()) === "Show 1 finished" && (await finishedFold.getAttribute("aria-expanded")) === "false", await finishedFold.innerText());
    await finishedFold.click();
    ck("the toggle opens it and says Hide 1 finished", (await finishedFold.innerText()) === "Hide 1 finished" && (await finishedFold.getAttribute("aria-expanded")) === "true");
    ck("an ended builder: finished, no memory, no fold", (await mold.count()) === 1 && /Finished its work\./.test(await mold.innerText())
       && (await mold.getByTestId("member-fold").count()) === 0 && (await mold.getByTestId("memory-bar").count()) === 0 && /Ended 1 day ago/.test(await mold.innerText()));
    const xStates = await page.getByTestId("crew-group").first().getByTestId("crew-member").evaluateAll((r) => r.map((x) => x.dataset.state));
    ck("the finished builder comes after the running ones", JSON.stringify(xStates) === JSON.stringify(["working", "idle", "ended"]), JSON.stringify(xStates));
    await finishedFold.click();
    ck("the toggle hides it again", (await mold.count()) === 0 && (await finishedFold.innerText()) === "Show 1 finished");
    const y = member(page, "Not heard from in 2 hours");
    ck("a builder not heard from for two hours says so", (await y.count()) === 1 && (await y.getAttribute("data-state")) === "not heard" && /Memory 88% full/.test(await y.innerText()));
    const bars = await page.getByTestId("memory-bar").evaluateAll((b) => b.map((x) => [x.getAttribute("aria-valuenow"), x.className.includes("--warn") ? "warn" : x.className.includes("bg-foreground") ? "ink" : "?"]));
    ck("a memory bar is amber from 80%, ink below", JSON.stringify(bars) === JSON.stringify([["82", "warn"], ["41", "ink"], ["58", "ink"], ["63", "ink"], ["88", "warn"]]), JSON.stringify(bars));
    const body = await page.getByTestId("factory-crew").innerText();
    ck("no station name, session or version shows outside the fold", !/project-x-build|crew-orchestrator|crew-reviewer|crew-releaser|ctx|claude_version|\bv\d+\.\d+/.test(body)
       && !/\bsession\b/i.test(body), body.slice(0, 200));
    ck("no member's lines were read before a fold was opened", detailCalls.length === 0, detailCalls.join(", "));

    // 3. how far, plan, standup
    const how = await page.getByTestId("crew-how-far").innerText();
    ck("how far it may go, with the link to Settings", /You approve each merge\./.test(how) && /Releases wait for you\./.test(how)
       && (await page.getByTestId("crew-how-far").getByRole("link", { name: "Change in Settings" }).getAttribute("href")) === "/settings#crew", how);
    const plan = await page.getByTestId("crew-plan").innerText();
    ck("the plan: both limits and the share of each project", /Current 5 hours\s+1% used, resets /.test(plan) && /This week\s+11% used, resets /.test(plan)
       && /\d+%, (\$\d+|under \$1) at API prices/.test(plan), plan);
    ck("the standup says what moved", /^In the last day: .*project-x/.test((await page.getByTestId("crew-standup").locator("[data-user-text]").innerText()).trim()),
       await page.getByTestId("crew-standup").innerText());
    ck("pace and grants are not set here: no switch, no pace control, no pacing banner", (await page.getByRole("switch").count()) === 0
       && (await page.getByRole("checkbox").count()) === 0 && (await page.getByText(/^pace\b/i).count()) === 0 && (await page.getByTestId("crew-pacing").count()) === 0);

    // 4. rules: markup is text; reject one, accept the other
    const rules = page.getByTestId("crew-rule");
    ck("two rules the reviewer proposes", (await rules.count()) === 2 && /^Rules the crew proposes\s*2$/m.test(await page.getByTestId("crew-rules").innerText()), `${await rules.count()}`);
    const markup = rules.filter({ hasText: "on a red check" });
    ck("markup in a rule shows as text", /<i>on a red check<\/i>/.test(await markup.innerText()) && (await markup.locator("i").count()) === 0);
    ck("each rule says where it goes and when (TR-1223)", /For (every project|\S+) · .*6 hours ago/.test(await markup.innerText()), await markup.innerText());
    await checkWords(page, ck, "live");
    await shot(page, "live-1440.png");
    const rejected = page.waitForResponse((r) => /\/api\/rule-proposals\/.+\/reject$/.test(r.url()) && r.request().method() === "POST");
    await markup.getByRole("button", { name: "Reject" }).click();
    ck("Reject is one POST that answers 200", (await rejected).status() === 200);
    await settle(page);
    ck("the rejected rule is gone", (await rules.count()) === 1 && !/red check/.test(await page.getByTestId("crew-rules").innerText()));
    const accepted = page.waitForResponse((r) => /\/api\/rule-proposals\/.+\/accept$/.test(r.url()) && r.request().method() === "POST");
    await rules.first().getByRole("button", { name: "Accept" }).click();
    ck("Accept is one POST that answers 200", (await accepted).status() === 200);
    await settle(page);
    ck("no rules are left, and it says so", /No rules are proposed\./.test(await page.getByTestId("crew-rules").innerText()) && (await rules.count()) === 0);

    // 5. the fold
    const fold = m1.getByTestId("member-fold");
    ck("the fold is closed, its button says Show what it did", (await fold.innerText()) === "Show what it did" && (await fold.getAttribute("aria-expanded")) === "false");
    const detailRead = page.waitForResponse((r) => /\/api\/crew\/project-x-build-m1\?recent=0&conversation=200&crew=[^&]+$/.test(r.url()));
    await fold.click();
    ck("opening it reads that member's lines, once, of its own crew (TR-1223)", (await detailRead).status() === 200);
    await m1.getByTestId("member-conversation").waitFor();
    const lines = await m1.getByTestId("member-conversation").innerText();
    ck("it reads as a conversation, its text as text, markup included (TR-1177)",
       lines.includes("<script>alert(1)</script> & more") && (await m1.getByTestId("member-conversation").locator("script").count()) === 0, lines);
    ck("what it said is the person-facing text of the member, exempt from the words rule",
       (await m1.getByTestId("bubble-said").count()) > 0
       && (await m1.getByTestId("bubble-said").first().locator("[data-user-text]").count()) > 0);
    ck("it says how to open the member in a terminal", /Open in a terminal/.test(await m1.getByTestId("member-detail").innerText())
       && (await m1.getByTestId("member-attach").innerText()) === "tares factory crew attach project-x-build-m1");
    ck("the button says Hide what it did", (await fold.innerText()) === "Hide what it did");
    await checkWords(page, ck, "live, fold open");
    await shot(page, "live-fold-1440.png");
    const asked = page.waitForResponse((r) => /\/api\/crew\/project-x-build-m1\/context\?crew=[^&]+$/.test(r.url()) && r.request().method() === "POST");
    await m1.getByRole("button", { name: "Read memory again" }).click();
    ck("Read memory again is one POST that answers 200", (await asked).status() === 200);
    ck("and says it was asked", /Asked\. It answers in a few seconds\./.test(await m1.innerText()));
    await fold.click();
    ck("closing the fold hides the lines", (await m1.getByTestId("member-detail").count()) === 0);
    ck("the lines were read once for the one fold that was opened", detailCalls.filter((c) => c.startsWith("GET")).length === 1, detailCalls.join(", "));

    // 5b. review of PR 602: a full output that does not load is tried once; Show earlier and the live
    // refresh leave no gap (the member's answers are replaced with controlled ones)
    {
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const p = await c.newPage();
      const at = (m) => `2026-11-11T10:${String(m % 60).padStart(2, "0")}:00+00:00`;
      const said = (n) => ({ kind: "said", at: at(n), text: `line ${n}` });
      let toolCalls = 0;
      await p.route(/\/api\/crew\/[^/?]+\/tool\//, (r) => { toolCalls++; r.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no tool call"}' }); });
      let total = 200;
      const asked = [];
      await p.route(/\/api\/crew\/[^/?]+\?/, async (r) => {
        const u = new URL(r.request().url());
        const before = u.searchParams.get("before"), since = u.searchParams.get("since");
        asked.push(before !== null ? `before=${before}` : since !== null ? `since=${since}` : "live");
        const real = await (await r.fetch()).json();
        const end = before !== null ? Number(before) : total;
        const start = since !== null ? Number(since) : Math.max(end - 100, 0);
        const conversation = Array.from({ length: end - start }, (_, i) => said(start + i));
        if (before === null && since === null) conversation.push({ kind: "tool", at: at(1), id: "big1", name: "Bash", summary: "cmd big1", input: "{}", output: "short", failed: false, ms: 100, more: true });
        r.fulfill({ json: { ...real, conversation, conversation_from: start } });
      });
      await p.goto(`${BASE}/crew`, { waitUntil: "load" });
      await p.getByTestId("member-fold").first().click();
      await p.getByText("cmd big1").first().click();
      await p.getByTestId("tool-output-missed").waitFor();
      await p.waitForTimeout(1500);
      ck("a full output that does not load is asked for once, and says so", toolCalls === 1, `${toolCalls} requests`);
      await p.getByTestId("conversation-earlier").click();
      await p.getByText("line 0", { exact: true }).waitFor();
      total = 250;
      await p.waitForTimeout(11000);
      const shown = await p.getByTestId("conversation-scroll").innerText();
      const missing = [];
      for (let n = 0; n < 250; n++) if (!new RegExp(`\\bline ${n}\\b`).test(shown)) missing.push(n);
      ck("Show earlier then a refresh: every line is there, none twice", missing.length === 0
         && (shown.match(/\bline 150\b/g) || []).length === 1, `missing ${missing.slice(0, 5).join(",")}; asked ${asked.join(", ")}`);
      ck("the refresh after Show earlier reads from where the earlier lines end", asked.includes("since=100"), asked.join(", "));
      await c.close();
    }

    // 6. an error, then Retry
    await page.route("**/api/factory/crew", (r) => r.abort());
    await page.goto(`${BASE}/crew`, { waitUntil: "load" });
    await page.getByRole("button", { name: "Retry" }).waitFor();
    ck("a cell that does not answer: the crew did not load, and Retry", /The crew did not load\./.test(await page.getByTestId("factory-crew").innerText()));
    await checkWords(page, ck, "live error");
    await page.unroute("**/api/factory/crew");
    await page.getByRole("button", { name: "Retry" }).click();
    await page.getByTestId("crew-builders").waitFor();
    ck("Retry brings the crew back", (await page.getByRole("button", { name: "Retry" }).count()) === 0 && (await page.getByTestId("crew-member").count()) >= 5);
    await ctx.close();

    // 7. the sample flag is needed: without it the address alone loads the cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/crew?sample=busy`, { waitUntil: "load" });
      await p.getByTestId("crew-builders").waitFor();
      ck("without the sample flag ?sample= shows the cell's crew", !/Handing out the tickets of project-x/.test(await p.getByTestId("factory-crew").innerText())
         && /Working on Footer/.test(await p.getByTestId("factory-crew").innerText()));
      await c.close();
    }

    // 8. L4 with no releaser and a window at 86%: the sentence, no Releaser, the new words
    {
      const put = await api("/api/crew/settings", { method: "PUT", body: JSON.stringify({ autonomy: "L4-ship", releaser: false, builders_max: 4, pace: true }) });
      ck("the crew's settings are changed", put.status === 200, `${put.status}`);
      const now = new Date();
      const line = { type: "claude_plan", timestamp: now.toISOString(), usage_report: { session: { total_cost_usd: 0 }, rate_limits: { limits: [
        { kind: "session", group: "session", percent: 86, scope: null, resets_at: new Date(now.getTime() + 2 * 3600e3).toISOString(), severity: "normal", is_active: true },
        { kind: "weekly_all", group: "weekly", percent: 61, scope: null, resets_at: new Date(now.getTime() + 2 * 86400e3).toISOString(), severity: "normal", is_active: false }],
        extra_usage: { is_enabled: false, monthly_limit: null } } } };
      const ing = await fetch(`${BASE}/ingest/claude_code`, { method: "POST", headers: { "content-type": "application/x-ndjson" }, body: JSON.stringify(line) + "\n" });
      ck("a plan reading at 86% is sent", ing.status < 300, `${ing.status}`);
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/crew`, { waitUntil: "load" });
      await p.getByTestId("crew-pacing").waitFor();
      const t = await p.getByTestId("crew-pacing").innerText();
      ck("pacing is a sentence at the top, naming the window and when it ends", /^Pacing: the 5-hour window is at 86%, so no new builder starts until /.test(t.trim()), t);
      ck("no Releaser now, and the words follow", JSON.stringify(await heads(p)).indexOf("Releaser") < 0
         && /The crew merges after its review and green CI\./.test(await p.getByTestId("crew-how-far").innerText())
         && /No releases\. Up to 4 builders at once\./.test(await p.getByTestId("crew-how-far").innerText()));
      ck("the 5-hour bar is amber", await p.getByTestId("crew-plan").locator("[role=progressbar]").first().evaluate((b) => b.className.includes("--warn")));
      await checkWords(p, ck, "live paced");
      await shot(p, "live-paced-1440.png");
      await c.close();
    }

    // 9. the live page at 390 px
    {
      const { ctx: c, page: p } = await newPage(390);
      await p.goto(`${BASE}/crew`, { waitUntil: "load" });
      await p.getByTestId("crew-builders").waitFor();
      await settle(p);
      ck("390: the page does not scroll sideways", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      const first = p.getByTestId("crew-member").first();
      const pos = await first.evaluate((r) => {
        const title = r.querySelector("[data-user-text]").getBoundingClientRect();
        const heard = [...r.querySelectorAll("div")].find((d) => /^Heard /.test(d.textContent) && d.children.length === 0).getBoundingClientRect();
        return { sameLeft: Math.abs(title.left - heard.left) < 1, below: heard.top >= title.bottom - 1 };
      });
      ck("390: heard sits under the title", pos.sameLeft && pos.below, JSON.stringify(pos));
      await p.getByTestId("member-fold").first().click();
      await p.getByTestId("member-conversation").first().waitFor();
      ck("390: an open fold does not scroll sideways", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      await checkWords(p, ck, "390 live");
      await shot(p, "live-390.png");
      await c.close();
    }

    // 10. every sample state at 1440 and 390: its words, no sideways scroll, a screenshot
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, true);
        const tag = `${width} sample ${name}`;
        await p.goto(`${BASE}/crew?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("crew-loading").waitFor();
        else if (name === "err") await p.getByRole("button", { name: "Retry" }).waitFor();
        else await p.getByTestId("factory-crew").getByRole("heading", { name: "Autonomy" }).waitFor();
        await settle(p);
        const text = await p.getByTestId("factory-crew").innerText();
        if (name === "load") ck(`${tag}: it is the loading state, no crew`, (await p.getByTestId("crew-member").count()) === 0 && (await p.getByTestId("crew-loading").isVisible()));
        if (name === "err") ck(`${tag}: it says the crew did not load and offers Retry`, /The crew did not load\./.test(text) && (await p.getByRole("button", { name: "Retry" }).count()) === 1 && (await p.getByTestId("crew-member").count()) === 0, text);
        if (name === "busy") ck(`${tag}: 6 members shown, the 2 finished ones folded, no Releaser`, (await p.getByTestId("crew-member").count()) === 6 && !/Releaser/.test(text), `${await p.getByTestId("crew-member").count()}`);
        if (name === "finished") {
          const g = p.getByTestId("crew-group");
          const heads2 = await g.evaluateAll((a) => a.map((e) => e.firstElementChild.lastElementChild.textContent));
          ck(`${tag}: the headers read running and total`, JSON.stringify(heads2) === JSON.stringify(["2 running · 19", "0 running · 2", "1 running · 1"]), JSON.stringify(heads2));
          const states = () => g.first().getByTestId("crew-member").evaluateAll((r) => r.map((x) => x.dataset.state));
          ck(`${tag}: the two running ones are the only rows, the quiet one among them`, JSON.stringify(await states()) === JSON.stringify(["working", "not heard"]), JSON.stringify(await states()));
          const folds = p.getByTestId("crew-finished-fold");
          ck(`${tag}: a toggle where something finished, and none where nothing did`, JSON.stringify(await folds.allInnerTexts()) === JSON.stringify(["Show 17 finished", "Show 2 finished"]), JSON.stringify(await folds.allInnerTexts()));
          await checkWords(p, ck, `${tag} closed`);
          await shot(p, `crew-finished-closed-${width}.png`);
          await folds.first().click();
          const open = await states();
          ck(`${tag}: open, every finished one is reachable under the running ones`, open.length === 19 && open[0] === "working" && open[1] === "not heard" && open.slice(2).every((s) => s === "ended"), JSON.stringify(open));
          ck(`${tag}: open, the toggle says Hide and the other group stays closed`, JSON.stringify(await folds.allInnerTexts()) === JSON.stringify(["Hide 17 finished", "Show 2 finished"]), JSON.stringify(await folds.allInnerTexts()));
          ck(`${tag}: open, no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
          if (width === 390) {
            const box = await folds.first().boundingBox();
            ck(`${tag}: the toggle is a row wide and easy to tap`, box.height >= 36 && box.width > 300, JSON.stringify(box));
          }
        }
        if (name === "fold") {
          await p.getByTestId("member-fold").nth(3).click();
          await p.getByTestId("member-conversation").waitFor();
          const convo = p.getByTestId("member-conversation");
          ck(`${tag}: the conversation shows text as text`, /<b>not markup<\/b>/.test(await convo.innerText()) && (await convo.locator("b").count()) === 0);
          const md = convo.getByTestId("bubble-said").filter({ hasText: "Crew on the machine" });
          ck(`${tag}: a message reads as markdown: bold, a list and code, no stars left (TR-1177)`,
             (await md.locator("strong").count()) === 3 && (await md.locator("li").count()) === 2 && (await md.locator("code").count()) === 2
             && !/\*\*/.test(await md.innerText()));
          ck(`${tag}: messages on both sides, tool cards, a failed one, a group and dividers (TR-1177)`,
             (await convo.getByTestId("bubble-said").count()) > 0 && (await convo.getByTestId("bubble-asked").count()) === 2
             && (await convo.getByTestId("bubble-factory").count()) === 1 && (await convo.locator("[data-failed=true]").count()) === 1
             && (await convo.getByTestId("tool-group").count()) === 1 && (await convo.getByTestId("conversation-divider").count()) === 2);
          ck(`${tag}: it opens at the newest and says it follows`,
             (await convo.getByTestId("conversation-following").count()) === 1
             && (await convo.getByTestId("conversation-scroll").evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight < 48)));
          const bash = convo.locator("[data-testid=tool-card][data-tool=Bash]").first();
          ck(`${tag}: closed, a Bash card's header says what it does, in words (TR-1317)`,
             (await bash.getByTestId("tool-summary").innerText()) === "Check the frozen kit");
          await convo.getByTestId("tool-head").nth(1).click();
          ck(`${tag}: a Bash card opens to its description, its command and its output with the exit status (TR-1317)`,
             (await bash.getByTestId("tool-description").innerText()) === "Check the frozen kit"
             && (await bash.getByTestId("tool-command").innerText()) === "$ bash tests/factory/claude_update_test.sh"
             && (await bash.getByTestId("tool-mark").innerText()) === "exit 1" && (await bash.getByTestId("tool-mark").getAttribute("data-ok")) === "false"
             && /^FAIL cc_frozen_midround: unknown flag --known/.test(await bash.getByTestId("tool-output").innerText())
             && (await bash.getByTestId("tool-input").count()) === 0 && (await bash.getByTestId("tool-copy").count()) === 2,
             await bash.getByTestId("tool-body").innerText());
          await p.evaluate(() => {
            window.__copied = "";
            Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (t) => { window.__copied = t; return Promise.resolve(); } } });
          });
          await bash.getByTestId("tool-copy").first().click();
          const copied = await p.evaluate(() => window.__copied);
          ck(`${tag}: the command's Copy copies the call's input as it came`,
             (() => { try { return JSON.parse(copied).description === "Check the frozen kit"; } catch { return false; } })(), copied);
          await bash.getByTestId("tool-raw").click();
          ck(`${tag}: Raw shows the plain Input and Output blocks, each with Copy, and the switch stays`,
             (await convo.getByTestId("tool-input").count()) === 1 && /Exit code 1\nFAIL cc_frozen_midround/.test(await bash.getByTestId("tool-output").innerText())
             && (await bash.getByTestId("tool-copy").count()) === 2 && (await bash.getByTestId("tool-raw").getAttribute("aria-pressed")) === "true");
          const bashInput = convo.getByTestId("tool-input");
          ck(`${tag}: the input is JSON in colour: keys, strings and numbers each their own span`,
             (await bashInput.getAttribute("data-look")) === "json" && (await bashInput.locator("span").count()) >= 4
             && /"command": "bash tests\/factory\/claude_update_test.sh"/.test(await bashInput.innerText()));
          await convo.getByTestId("tool-head").nth(0).click();
          const editIn = convo.getByTestId("tool-input").first();
          ck(`${tag}: an Edit call's input reads as a change, taken out red and put in green`,
             (await editIn.getAttribute("data-look")) === "diff" && /^-check cc_frozen_midround/m.test(await editIn.innerText())
             && /^\+check cc_frozen_midround --known/m.test(await editIn.innerText()));
          ck(`${tag}: an output that is a diff is coloured too, and plain output stays plain`,
             (await convo.getByTestId("tool-output").first().getAttribute("data-look")) === "diff"
             && (await convo.getByTestId("tool-output").nth(1).getAttribute("data-look")) === "plain");
          await convo.getByTestId("tool-head").nth(0).click();
          await bash.getByTestId("tool-formatted").click();
          ck(`${tag}: Formatted brings the Bash body back`, (await bash.getByTestId("tool-command").count()) === 1);
          await convo.getByTestId("tool-head").nth(1).click();

          const plan = convo.locator("[data-testid=tool-card][data-tool=TodoWrite]");
          ck(`${tag}: closed, a TodoWrite card is the plan and counts what is done (TR-1317)`,
             /^Plan/.test(await plan.getByTestId("tool-head").innerText()) && (await plan.getByTestId("tool-summary").innerText()) === "2 of 4 done · Renaming the variable");
          await plan.getByTestId("tool-head").click();
          const stepStates = await plan.getByTestId("plan-step").evaluateAll((a) => a.map((e) => e.dataset.status));
          ck(`${tag}: open, it is a checklist with a progress bar`,
             JSON.stringify(stepStates) === JSON.stringify(["completed", "completed", "in_progress", "pending"])
             && (await plan.locator("[role=img][aria-label='2 of 4 done']").count()) === 1 && (await plan.getByTestId("tool-output").count()) === 0, JSON.stringify(stepStates));
          await plan.getByTestId("tool-head").click();

          const agent = convo.locator("[data-testid=tool-card][data-tool=Agent]");
          ck(`${tag}: closed, an Agent card shows its description and the helper's type (TR-1317)`,
             (await agent.getByTestId("tool-summary").innerText()) === "Find every use of the old name" && (await agent.getByTestId("tool-tag").innerText()) === "Explore");
          await agent.getByTestId("tool-head").click();
          ck(`${tag}: open, the brief and the report read as markdown`,
             (await agent.getByTestId("tool-brief").locator("code").count()) === 3
             && (await agent.getByTestId("tool-report").locator("h2").innerText()) === "Found 2 places"
             && (await agent.getByTestId("tool-report").locator("li").count()) === 2 && !/\*\*/.test(await agent.getByTestId("tool-report").innerText())
             && (await agent.getByTestId("tool-mark").innerText()) === "done");
          await agent.getByTestId("tool-head").click();

          await convo.getByTestId("group-head").click();
          const cut = convo.getByTestId("tool-group").locator("[data-testid=tool-card][data-tool=Bash]").nth(1);
          await cut.getByTestId("tool-head").click();
          ck(`${tag}: a Bash input the list cut short shows the plain blocks, no switch, no error (TR-1317)`,
             (await cut.getByTestId("tool-body").getAttribute("data-shaped")) === "false" && (await cut.getByTestId("tool-input").count()) === 1
             && (await cut.getByTestId("tool-switch").count()) === 0 && /cat > notes\.md/.test(await cut.getByTestId("tool-input").innerText()));
          await cut.getByTestId("tool-head").click();
          const fetchCard = convo.locator("[data-testid=tool-card][data-tool=WebFetch]");
          await fetchCard.getByTestId("tool-head").click();
          ck(`${tag}: a tool with no view of its own looks as it did`,
             (await fetchCard.getByTestId("tool-input").count()) === 1 && (await fetchCard.getByTestId("tool-switch").count()) === 0
             && (await fetchCard.getByTestId("tool-title").innerText()) === "WebFetch"
             && (await fetchCard.getByTestId("tool-summary").innerText()) === "https://docs.glassflow.ai/tares");
          await fetchCard.getByTestId("tool-head").click();

          const send = convo.locator("[data-testid=tool-card][data-tool=SendMessage]");
          ck(`${tag}: closed, a message names who it went to and its first line (TR-1317)`,
             (await send.getByTestId("tool-title").innerText()) === "To crew-reviewer" && (await send.getByTestId("tool-summary").innerText()) === "PR #595 is ready.");
          await send.getByTestId("tool-head").click();
          ck(`${tag}: open, the message reads as markdown and the answer stays in Raw`,
             (await send.getByTestId("tool-message").locator("li").count()) === 2 && (await send.getByTestId("tool-message").locator("strong").innerText()) === "known"
             && (await send.getByTestId("tool-output").count()) === 0);
          await send.getByTestId("tool-head").click();

          const read = convo.locator("[data-testid=tool-card][data-tool=Read]");
          ck(`${tag}: closed, a Read card names the file and the lines (TR-1317)`,
             (await read.getByTestId("tool-summary").innerText()) === "/w/shop/tests/factory/lib.sh · lines 10-14");
          await read.getByTestId("tool-head").click();
          const nums = await read.getByTestId("line-number").allInnerTexts();
          ck(`${tag}: open, the code has its line numbers beside it, not in it`,
             JSON.stringify(nums) === JSON.stringify(["10", "11", "12", "13", "14"])
             && !/\t/.test(await read.getByTestId("tool-lines").innerText().then((x) => x.replace(/^\d+/gm, "")))
             && /check\(\) \{/.test(await read.getByTestId("tool-lines").innerText()), JSON.stringify(nums));
          await read.getByTestId("tool-head").click();

          const grep = convo.locator("[data-testid=tool-card][data-tool=Grep]");
          ck(`${tag}: closed, a Grep card says its pattern, where, and how many files (TR-1317)`,
             (await grep.getByTestId("tool-summary").innerText()) === "KNOWN_DIFFS in /w/shop · 2 files");
          await grep.getByTestId("tool-head").click();
          const found = await grep.getByTestId("match-file").allInnerTexts();
          ck(`${tag}: open, the files it found, relative to where it looked, with their counts`,
             JSON.stringify(found.map((x) => x.replace(/\s+/g, " "))) === JSON.stringify(["tests/factory/claude_update_test.sh 3", "tests/factory/lib.sh 1"]), JSON.stringify(found));
          await grep.getByTestId("tool-head").click();

          const glob = convo.locator("[data-testid=tool-card][data-tool=Glob]");
          ck(`${tag}: a Glob that found nothing says so in the header`,
             (await glob.getByTestId("tool-summary").innerText()) === "docs/**/*.md in /w/shop · no files");
          await glob.getByTestId("tool-head").click();
          ck(`${tag}: and in the body`, (await glob.getByTestId("matches-none").count()) === 1);
          await glob.getByTestId("tool-head").click();
          await convo.getByTestId("group-head").click();
          await convo.getByTestId("group-head").click();
          await convo.getByTestId("tool-group").getByTestId("tool-head").nth(1).click();
          await p.waitForFunction(() => /case 120/.test(document.querySelector("[data-testid=tool-group] [data-testid=tool-output]")?.textContent || ""));
          ck(`${tag}: an output the list cut loads whole when its card opens`, true);
          await convo.getByTestId("conversation-open-all").click();
          ck(`${tag}: Open all tools opens every card`, (await convo.getByTestId("tool-body").count()) === 13);
          await convo.getByTestId("conversation-scroll").evaluate((e) => { e.scrollTop = 0; e.dispatchEvent(new Event("scroll")); });
          await p.waitForTimeout(100);
          ck(`${tag}: scrolled up, it stops following and offers the newest`, (await convo.getByTestId("conversation-newest").count()) === 1);
          ck(`${tag}: open, no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
          ck(`${tag}: the attach command shows`, /tares factory crew attach /.test(await p.getByTestId("member-attach").innerText()));
          await checkWords(p, ck, `${tag} open`);
          await shot(p, `crew-fold-open-${width}.png`);
        }
        if (name === "l3") ck(`${tag}: a Releaser, a reviewer waiting for you, and the L3 words`, /Releaser/.test(text) && /Waiting for you/.test(text) && /You approve each merge\./.test(text), text);
        if (name === "pace") ck(`${tag}: the pacing banner and no switch`, (await p.getByTestId("crew-pacing").isVisible()) && (await p.getByRole("switch").count()) === 0);
        if (name === "quiet") ck(`${tag}: everybody is not heard from, with no standup yet`, (await p.getByTestId("crew-member").and(p.locator("[data-state='not heard']")).count()) === 4 && /Nothing moved in the last day\./.test(text), text);
        if (name === "none") ck(`${tag}: not started, how to start it, no other section`, /The crew is not started\./.test(text) && (await p.getByTestId("crew-not-started").locator("code").innerText()) === "tares factory crew up"
           && JSON.stringify(await heads(p)) === JSON.stringify(["Orchestrator", "Autonomy"]) && (await p.getByTestId("crew-member").count()) === 0, JSON.stringify(await heads(p)));
        if (name === "busy") {
          const metas = await p.getByTestId("crew-rule-meta").allInnerTexts();
          ck(`${tag}: each rule says where it goes and the tickets it came back in (TR-1223)`,
             /^For project-x · came back in 3 tickets \(project-x T3, project-x T5, project-x T9\), 2 days ago$/.test(metas[0] || "")
             && /^For every project · came back in 6 tickets \(.* and 2 more\), 6 hours ago$/.test(metas[1] || ""), JSON.stringify(metas));
        }
        if (name === "team") {
          const crews = p.getByTestId("crew-of-person");
          ck(`${tag}: one section per person's crew, the caller's first, each with its own state (TR-1223)`,
             (await crews.count()) === 2 && /^Ashish \(you\)/.test(await crews.nth(0).locator("h2").first().innerText())
             && /^Adam/.test(await crews.nth(1).locator("h2").first().innerText())
             && /Working/.test(await crews.nth(0).getByTestId("crew-of-person-summary").innerText())
             && /^Not running/.test(await crews.nth(1).getByTestId("crew-of-person-summary").innerText())
             && (await p.getByTestId("crew-summary").count()) === 0);
          ck(`${tag}: a crew's own name shows next to its person, and an ended crew is put away`,
             /, crew ashish-mbp/.test(await crews.nth(0).locator("h2").first().innerText())
             && (await p.getByTestId("crew-ended-toggle").innerText()) === "Show 1 ended crew");
          await p.getByTestId("crew-ended-toggle").click();
          ck(`${tag}: Show 1 ended crew shows it, folded, with when it was last heard and its unknown machine`,
             (await crews.count()) === 3 && /Ended, last heard 2 days ago/.test(await crews.nth(2).innerText())
             && /on an unknown machine/.test(await crews.nth(2).locator("h2").first().innerText())
             && (await crews.nth(2).getByTestId("crew-of-person-body").count()) === 0);
          await p.getByTestId("crew-ended-toggle").click();
          ck(`${tag}: the caller's crew starts open, the others folded, each still saying how it runs`,
             (await crews.nth(0).getByTestId("crew-of-person-body").count()) === 1
             && (await crews.nth(1).getByTestId("crew-of-person-body").count()) === 0
             && /Not running, no builder on it/.test(await crews.nth(1).getByTestId("crew-of-person-summary").innerText())
             && /3 builders on it/.test(await crews.nth(0).getByTestId("crew-of-person-summary").innerText()));
          await crews.nth(1).getByTestId("crew-of-person-toggle").click();
          ck(`${tag}: opening a folded crew shows only its own members`, /Finished its work\./.test(await crews.nth(1).innerText())
             && !/Finished its work\./.test(await crews.nth(0).innerText())
             && (await crews.nth(1).getByTestId("crew-of-person-toggle").getAttribute("aria-expanded")) === "true");
          await crews.nth(0).getByTestId("crew-of-person-toggle").click();
          ck(`${tag}: the caller's own crew folds too`, (await crews.nth(0).getByTestId("crew-of-person-body").count()) === 0);
          await p.reload({ waitUntil: "load" });
          await p.getByTestId("crew-of-person").first().waitFor();
          ck(`${tag}: the folds are kept in this browser after a reload`,
             (await p.getByTestId("crew-of-person").nth(0).getByTestId("crew-of-person-body").count()) === 0
             && (await p.getByTestId("crew-of-person").nth(1).getByTestId("crew-of-person-body").count()) === 1);
          await p.evaluate(() => { try { localStorage.removeItem("taresCrewFolds"); } catch (e) { /* none */ } });
        }
        if (name === "many") ck(`${tag}: four projects`, (await p.getByTestId("crew-builders").locator("[data-user-text]").evaluateAll((e) => e.filter((x) => x.parentElement.className.includes("wash-2")).length)) === 4);
        if (name === "long") ck(`${tag}: long names and texts wrap inside the page`, (await overflow(p)) <= 0);
        ck(`${tag}: no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
        await checkWords(p, ck, tag);
        await shot(p, `crew-${name}-${width}.png`);
        await c.close();
      }
    }
    // a sample rule is taken off the page (the sample has no cell to write to)
    {
      const { ctx: c, page: p } = await newPage(1440, true);
      await p.goto(`${BASE}/crew?sample=busy`, { waitUntil: "load" });
      await p.getByTestId("crew-rules").waitFor();
      await p.getByTestId("crew-rule").first().getByRole("button", { name: "Reject" }).click();
      await settle(p);
      ck("sample: Reject takes the rule off the page", (await p.getByTestId("crew-rule").count()) === 1);
      await c.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
