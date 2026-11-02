/*
 * Browser e2e for TR-753: the AI SRE Runbooks and memory page (/runbooks).
 *
 * Per service, what the investigator should know: the runbook (lines people wrote, each with its
 * author and day, one Remove per line, an Add line box under them) and, apart from it and read only,
 * what the investigator remembered. One read of the cell (GET /api/sre/runbooks) feeds the page and it
 * reads again every 60 seconds; a write answers with the new whole screen, so it costs no second read.
 * The first line added for a service makes the shared skill runbook-<service>. On a phone the service
 * list is a picker. Every state shows only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch AI SRE cell (tests/seed-sre-runbooks.py, invented names), then runs the real daemon on
 * it, serving the built console. Each of the 11 states (normal, typing, dupe, nolines, memonly, empty,
 * loading, error, addfail, long, many) is checked twice at 1440 and 390: live (the real daemon, or the
 * daemon with one answer held back, failed or replaced where the cell cannot make the state itself) and
 * as a sample (?sample=<name>, with the sample flag in localStorage).
 * Run:  npm run build && node tests/sre-runbooks.e2e.cjs     (from ui/, the repo's .venv with tares' dependencies)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8872);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROUTE = "/api/sre/runbooks";
const STATES = ["normal", "typing", "dupe", "nolines", "memonly", "empty", "loading", "error", "addfail", "long", "many"];
const SUBTITLE = "What the investigator should know about each service. People write the lines; the investigator adds what it learned.";
const SUBTITLE_SHORT = "What the investigator should know about each service.";
const HINT = "One line, up to 500 characters. The investigator reads it from its next investigation.";
const PLACEHOLDER = "Add one line the investigator should know about this service";
const ERROR_WORDS = "Runbooks did not load. Tares could not read them just now. It will try again in 60 seconds.";
const SAME = "That line is already in this runbook.";
const NOT_SAVED = "The line was not saved. Nothing changed, try again.";
const NO_LINE = "Write the line to add.";
const NO_SERVICES = "Services show here once the watcher has seen logs from them. Then you can write what the investigator should know about each one.";

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

const squash = (t) => String(t).replace(/\s+/g, " ").trim();
const json = (body, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

// 50 services, the answer the "many" state replaces the cell's with (service-02 has 3 lines)
function manyServices() {
  const line = (i) => ({ text: `Check the dashboard for service ${i}.`, by: "Alex", when_words: "3 days ago" });
  const services = Array.from({ length: 50 }, (_, i) => {
    const name = `service-${String(i + 1).padStart(2, "0")}`;
    const n = name === "service-02" ? 3 : i % 4 === 0 ? 0 : 1 + ((i * 7) % 5);
    const runbook = Array.from({ length: n }, () => line(i + 1));
    return { name, lines_words: n === 0 ? "No lines yet" : n === 1 ? "1 line" : `${n} lines`, runbook, memory: [],
             used_words: n ? "Read by the investigator 2 times this week" : null };
  });
  return { services, empty_words: null };
}

(async () => {
  const python = fs.existsSync(path.join(REPO, ".venv/bin/python")) ? path.join(REPO, ".venv/bin/python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-runbooks-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const seeded = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-runbooks.py"), home, String(PORT),
    String(PORT + 1), dist], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded the cell", seeded.services.includes("edge-proxy") && seeded.services.includes("docs-site"));

  const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO };   // this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]) delete env[k];
  const daemon = spawn(python, ["-c", "from tares.cli import main; main()", "up", "--port", String(PORT), "--data-dir", home],
                       { cwd: REPO, stdio: "ignore", env });
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
    const apiGet = async (p) => JSON.parse(await (await fetch(BASE + p)).text());

    // what a page shows, by test id
    const T = (page) => ({
      root: page.getByTestId("sre-runbooks"),
      title: page.getByTestId("rb-title"),
      services: page.getByTestId("rb-service"),
      count: (name) => page.locator(`[data-testid=rb-service][data-service="${name}"] [data-testid=rb-count]`),
      lines: page.getByTestId("rb-line"),
      texts: page.getByTestId("rb-line-text"),
      bys: page.getByTestId("rb-line-by"),
      removes: page.getByTestId("rb-remove"),
      input: page.getByTestId("rb-add-input"),
      add: page.getByTestId("rb-add").getByRole("button", { name: "Add line" }),
      error: page.getByTestId("rb-add-error"),
      memory: page.getByTestId("rb-memory"),
      memLines: page.getByTestId("rb-memory-line"),
      picker: page.getByTestId("rb-picker"),
      list: page.getByTestId("rb-list"),
      used: page.getByTestId("rb-used"),
      banner: page.getByTestId("rb-error"),
    });
    const body = (page) => page.getByTestId("sre-runbooks").innerText().then(squash);
    const texts = (page) => T(page).texts.allInnerTexts().then((a) => a.map(squash));

    // ---------------------------------------------------------------- live, 1440 px
    const { ctx, page } = await newPage(1440);
    const t = T(page);
    const writes = [];     // every request that is not a read
    page.on("request", (req) => { if (req.method() !== "GET" && new URL(req.url()).pathname.startsWith("/api/")) writes.push(`${req.method()} ${new URL(req.url()).pathname}`); });
    const reads = [];
    page.on("request", (req) => { if (req.method() === "GET" && new URL(req.url()).pathname === ROUTE) reads.push(1); });

    // 1. the first open: one read, the first service chosen, the page in its words
    let one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/runbooks`, { waitUntil: "load" });
    await t.services.first().waitFor();
    await one(ck, "/runbooks");
    ck("the heading", await page.getByRole("heading", { name: "Runbooks and memory", exact: true }).isVisible());
    ck("the subtitle", squash(await page.locator("h1 + p").innerText()) === SUBTITLE);
    const names = (await t.services.evaluateAll((e) => e.map((x) => x.dataset.service)));
    ck("every service the cell knows is listed", ["edge-proxy", "service-a", "docs-site", "service-a-receiver", "extra-svc-01"].every((n) => names.includes(n)), names.join(","));
    ck("the first service is open and marked", (await t.title.innerText()) === names[0] &&
       (await t.services.first().getAttribute("aria-current")) === "true");
    ck("no picker on a wide page, the list is there", !(await t.picker.isVisible()) && (await t.list.isVisible()));
    await checkWords(page, ck, "live default");

    // 2. normal: edge-proxy, five lines with author and day, memory apart
    one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/runbooks?service=edge-proxy`, { waitUntil: "load" });
    await t.lines.first().waitFor();
    await one(ck, "normal");
    ck("normal: edge-proxy is chosen", (await t.title.innerText()) === "edge-proxy");
    ck("normal: the counts in words", squash(await t.count("edge-proxy").innerText()) === "5 lines" && squash(await t.count("extra-svc-02").innerText()) === "1 line" &&
       squash(await t.count("docs-site").innerText()) === "none" && squash(await t.count("extra-svc-01").innerText()) === "2 lines");
    ck("normal: a service with only memory says none, not a count of its notes", squash(await t.count("service-a").innerText()) === "none");
    ck("normal: how often it is read", squash(await t.used.innerText()) === "Read by the investigator 4 times this week");
    ck("normal: five lines, in the order written", JSON.stringify(await texts(page)) === JSON.stringify([
      "Check /health on the load balancer before restarting edge-proxy.",
      "The checkout-v2 cluster retries 3 times; a burst of 502s after a deploy is usually the retry budget.",
      "Rolling back is safe: the last 5 deploys had no migrations.",
      "Page the payments team only when 5xx stays above 2% for 10 minutes.",
      "Ignore the nightly 02:00 spike: it is the log rotation."]), (await texts(page)).join(" | "));
    ck("normal: each line says who and when", JSON.stringify((await t.bys.allInnerTexts()).map(squash)) === JSON.stringify(
      ["Alex, 3 days ago", "Kim, 14 days ago", "Alex, 14 days ago", "Kim, yesterday", "Lee, 30 days ago"]), (await t.bys.allInnerTexts()).join(" | "));
    ck("normal: one Remove per line", (await t.removes.count()) === 5 && (await t.removes.first().innerText()).trim() === "Remove");
    ck("normal: the add box and its hint", (await t.input.getAttribute("placeholder")) === PLACEHOLDER && (await body(page)).includes(HINT));
    ck("normal: the Add line button", (await t.add.isVisible()));
    ck("normal: the selected service has the accent edge", (await t.services.filter({ hasText: "edge-proxy" }).first().evaluate((e) => getComputedStyle(e).boxShadow)) !== "none");
    await checkWords(page, ck, "normal");
    await shot(page, "live-normal-1440.png");

    // 3. memory: apart, read only, the service's own
    ck("memory: its heading and what it is", (await t.memory.innerText()).includes("What the investigator remembered") &&
       (await t.memory.innerText()).includes("Kept by the investigator. You can read these, not change them."));
    ck("memory: edge-proxy's two notes, newest first", JSON.stringify((await t.memLines.getByTestId("rb-memory-text").allInnerTexts()).map(squash)) === JSON.stringify([
      "p95 latency above 800 ms usually follows a cache flush.",
      "A restart of edge-proxy cleared a 502 burst on 28 Oct; the cause was a stale upstream list."]), (await t.memLines.allInnerTexts()).join(" | "));
    ck("memory: each note says who and when", (await t.memLines.first().innerText()).includes("Investigator, today"));
    ck("memory: it is outside the runbook box", (await page.getByTestId("rb-pane").getByTestId("rb-memory").count()) === 0);
    ck("memory: nothing in it can be clicked or typed in", (await t.memory.locator("button, input, textarea, select, a, [contenteditable], [role=button]").count()) === 0);
    const before = (await t.memory.innerText());
    writes.length = 0;
    await t.memLines.first().dblclick();
    await page.keyboard.type("x");
    await t.memLines.first().click();
    ck("memory: clicking and typing on a note changes nothing", (await t.memory.innerText()) === before && writes.length === 0, writes.join(","));
    await checkWords(page, ck, "memory");

    // 4. typing
    await t.input.fill("Restart edge-proxy one pod at a time, never all at once.");
    ck("typing: the draft stays in the box, no error", (await t.input.inputValue()) === "Restart edge-proxy one pod at a time, never all at once." && (await t.error.count()) === 0);
    await checkWords(page, ck, "typing");
    await shot(page, "live-typing-1440.png");

    // 5. adding a line: one write, the whole screen back, no second read
    writes.length = 0; reads.length = 0;
    await t.add.click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=rb-line]").length === 6);
    await settle(page);
    ck("add: one POST to this service's lines, and no read after it", JSON.stringify(writes) === JSON.stringify(["POST /api/sre/runbooks/edge-proxy/lines"]) && reads.length === 0, `${writes.join(",")} reads=${reads.length}`);
    ck("add: the line is last, with who and today", (await texts(page)).at(-1) === "Restart edge-proxy one pod at a time, never all at once." && /^.+, today$/.test(squash(await t.bys.last().innerText())), squash(await t.bys.last().innerText()));
    ck("add: the count in the list follows", squash(await t.count("edge-proxy").innerText()) === "6 lines");
    ck("add: the box is empty again", (await t.input.inputValue()) === "");
    const saved = await apiGet(`${ROUTE}?service=edge-proxy`);
    ck("add: the cell has it", saved.services[0].runbook.at(-1).text === "Restart edge-proxy one pod at a time, never all at once.");

    // 6. a line that is already there, and an empty one
    await t.input.fill("Rolling back is safe: the last 5 deploys had no migrations.");
    writes.length = 0;
    await t.add.click();
    await t.error.waitFor();
    ck("dupe: the sentence, in the alert, in place of the hint", squash(await t.error.innerText()) === SAME && !(await body(page)).includes(HINT));
    ck("dupe: nothing was added and the draft is kept", (await t.lines.count()) === 6 && (await t.input.inputValue()).startsWith("Rolling back is safe"));
    ck("dupe: the error is announced", (await t.error.getAttribute("role")) === "alert");
    await checkWords(page, ck, "dupe");
    await shot(page, "live-dupe-1440.png");
    await t.input.fill("");
    writes.length = 0;
    await t.add.click();
    await t.error.waitFor();
    ck("empty: asks for the line and makes no call", squash(await t.error.innerText()) === NO_LINE && writes.length === 0, writes.join(","));
    await t.input.fill("   ");
    await t.add.click();
    ck("empty: a box of spaces is empty too", squash(await t.error.innerText()) === NO_LINE && writes.length === 0);
    await t.input.fill("typing clears the error");
    ck("empty: typing clears the error", (await t.error.count()) === 0);
    await t.input.fill("");

    // 7. Enter adds, Shift+Enter does not
    await t.input.fill("Enter adds the line.");
    await t.input.press("Shift+Enter");
    ck("enter: Shift+Enter does not add", (await t.lines.count()) === 6);
    await t.input.fill("Enter adds the line.");
    await t.input.press("Enter");
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=rb-line]").length === 7);
    ck("enter: Enter adds it", (await texts(page)).at(-1) === "Enter adds the line.");

    // 8. remove a line: one DELETE with its place, the whole screen back
    writes.length = 0; reads.length = 0;
    await t.removes.last().click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=rb-line]").length === 6);
    await settle(page);
    ck("remove: one DELETE for the last line, no read after it", JSON.stringify(writes) === JSON.stringify(["DELETE /api/sre/runbooks/edge-proxy/lines/6"]) && reads.length === 0, `${writes.join(",")} reads=${reads.length}`);
    ck("remove: the line is gone", !(await texts(page)).includes("Enter adds the line.") && squash(await t.count("edge-proxy").innerText()) === "6 lines");
    await t.removes.nth(5).click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=rb-line]").length === 5);
    ck("remove: back to the five that were written", JSON.stringify(await texts(page)) === JSON.stringify(saved.services[0].runbook.slice(0, 5).map((l) => l.text)));
    ck("remove: the cell agrees", (await apiGet(`${ROUTE}?service=edge-proxy`)).services[0].runbook.length === 5);

    // 9. nolines: docs-site has nothing; the first line makes the skill, the last removal takes it away
    one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/runbooks?service=docs-site`, { waitUntil: "load" });
    await t.title.waitFor();
    await one(ck, "nolines");
    ck("nolines: the words", (await body(page)).includes("No runbook for docs-site yet") &&
       (await body(page)).includes("Add the first line below. The investigator reads it from its next investigation of docs-site."));
    ck("nolines: no lines, no memory box, no used words", (await t.lines.count()) === 0 && (await t.memory.count()) === 0 && (await t.used.count()) === 0);
    ck("nolines: the add box is there", (await t.input.isVisible()) && (await t.add.isVisible()));
    await checkWords(page, ck, "nolines");
    await shot(page, "live-nolines-1440.png");
    ck("first line: no skill for docs-site yet", !(await apiGet("/api/skills")).some((s) => s.name === "runbook-docs-site"));
    await t.input.fill("Docs deploys on every merge to main.");
    await t.add.click();
    await t.lines.first().waitFor();
    ck("first line: the empty words are gone and the line is there", (await texts(page)).join("|") === "Docs deploys on every merge to main." && !(await body(page)).includes("No runbook for docs-site yet"));
    const skill = (await apiGet("/api/skills")).find((s) => s.name === "runbook-docs-site");
    ck("first line: it made the shared skill runbook-docs-site", !!skill, JSON.stringify((await apiGet("/api/skills")).map((s) => s.name)));
    ck("first line: the skill is wired into the project that has the watcher", !!skill && skill.used_by.some((p) => p.id === seeded.project));
    await t.removes.first().click();
    await t.title.waitFor();
    await page.getByTestId("rb-nolines").waitFor();
    ck("last line out: the empty words are back", (await body(page)).includes("No runbook for docs-site yet") && squash(await t.count("docs-site").innerText()) === "none");
    ck("last line out: the skill is gone", !(await apiGet("/api/skills")).some((s) => s.name === "runbook-docs-site"));

    // 10. memonly: service-a has notes and no runbook
    one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/runbooks?service=service-a`, { waitUntil: "load" });
    await t.title.waitFor();
    await one(ck, "memonly");
    ck("memonly: no runbook, the short words", (await body(page)).includes("No runbook for service-a yet") && (await body(page)).includes("Add the first line below.") &&
       !(await body(page)).includes("next investigation of service-a"));
    ck("memonly: the list says none for it", squash(await t.count("service-a").innerText()) === "none");
    const mem = (await t.memLines.getByTestId("rb-memory-text").allInnerTexts()).map(squash);
    ck("memonly: its own notes, not edge-proxy's", mem.length === 2 && mem.every((m) => m.includes("service-a") || m.startsWith("Job latency")) && !mem.some((m) => m.includes("edge-proxy")), mem.join(" | "));
    ck("memonly: the notes are read only", (await t.memory.locator("button, input, textarea, select, [contenteditable]").count()) === 0);
    await checkWords(page, ck, "memonly");
    await shot(page, "live-memonly-1440.png");

    // 11. long: a 500 character line wraps inside its box
    one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/runbooks?service=extra-svc-01`, { waitUntil: "load" });
    await t.lines.first().waitFor();
    await one(ck, "long");
    const longText = (await texts(page))[0];
    ck("long: the whole line is shown", longText.length > 450 && longText.endsWith("Do not restart the cache during business hours."), String(longText.length));
    ck("long: the next line reads Roll back, not Rollback", (await texts(page))[1] === "Roll back the last deploy first.");
    ck("long: read twelve times", squash(await t.used.innerText()) === "Read by the investigator 12 times this week");
    ck("long: no sideways scroll", (await overflow(page)) <= 0);
    await checkWords(page, ck, "long");
    await shot(page, "live-long-1440.png");

    // 12. the page is the cell's: a sample address without the flag shows the live data
    await page.goto(`${BASE}/runbooks?sample=many`, { waitUntil: "load" });
    await t.services.first().waitFor();
    ck("a sample address without the flag shows the live cell", (await t.services.count()) === seeded.services.length && !names.includes("service-02"));

    // 13. a service that is not on the page opens the first one
    await page.goto(`${BASE}/runbooks?service=no-such-service`, { waitUntil: "load" });
    await t.title.waitFor();
    ck("an unknown service in the address opens the first service", (await t.title.innerText()) === names[0]);

    // 14. every read of the page is one call; 60 seconds between them
    await ctx.close();
    {
      const { ctx: c2, page: p2 } = await newPage(1440);
      let n = 0;
      p2.on("request", (req) => { if (req.method() === "GET" && new URL(req.url()).pathname === ROUTE) n++; });
      await p2.clock.install();
      await p2.goto(`${BASE}/runbooks`, { waitUntil: "load" });
      await T(p2).services.first().waitFor();
      ck("polling: one read on open", n === 1, String(n));
      await p2.clock.runFor(55_000);
      ck("polling: no read before a minute has passed", n === 1, String(n));
      await p2.clock.runFor(6_000);
      await p2.waitForTimeout(300);
      ck("polling: a second read after a minute", n === 2, String(n));
      await c2.close();
    }

    // 15. phone: the list is a picker, the page does not scroll sideways
    {
      const { ctx: c, page: p } = await newPage(390);
      const q = T(p);
      one = expectOneCall(p, ROUTE);
      await p.goto(`${BASE}/runbooks?service=edge-proxy`, { waitUntil: "load" });
      await q.lines.first().waitFor();
      await one(ck, "phone normal");
      ck("phone: the picker is there and the list is not", (await q.picker.isVisible()) && !(await q.list.isVisible()));
      ck("phone: the picker names the open service", (await q.picker.inputValue()) === "edge-proxy");
      ck("phone: the picker holds every service", (await q.picker.locator("option").count()) === seeded.services.length);
      ck("phone: the picker has its label", squash(await p.locator("label", { has: q.picker }).innerText()).startsWith("Service"));
      ck("phone: no sideways scroll", (await overflow(p)) <= 0, String(await overflow(p)));
      await checkWords(p, ck, "phone normal");
      await shot(p, "live-normal-390.png");
      await q.picker.selectOption("service-a");
      await q.title.waitFor();
      await p.waitForFunction(() => document.querySelector("[data-testid=rb-title]").textContent === "service-a");
      ck("phone: choosing a service opens it, and keeps it in the address", (await q.title.innerText()) === "service-a" && new URL(p.url()).searchParams.get("service") === "service-a");
      ck("phone: the notes of service-a", (await q.memLines.count()) === 2);
      await checkWords(p, ck, "phone memonly");
      await shot(p, "live-memonly-390.png");
      ck("phone memonly: no sideways scroll", (await overflow(p)) <= 0);
      await q.input.fill("Restart edge-proxy one pod at a time.");
      await shot(p, "live-typing-390.png");
      await checkWords(p, ck, "phone typing");
      await q.input.fill("");
      for (const [state, service] of [["nolines", "docs-site"], ["long", "extra-svc-01"], ["dupe", "edge-proxy"]]) {
        one = expectOneCall(p, ROUTE);
        await p.goto(`${BASE}/runbooks?service=${service}`, { waitUntil: "load" });
        await q.title.waitFor();
        await one(ck, `phone ${state}`);
        if (state === "dupe") {
          await q.input.fill((await texts(p))[0]);
          await q.add.click();
          await q.error.waitFor();
          ck("phone dupe: the sentence", squash(await q.error.innerText()) === SAME);
        }
        ck(`phone ${state}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, `phone ${state}`);
        await shot(p, `live-${state}-390.png`);
      }
      await c.close();
    }

    // ---------------------------------------------------------------- live states the cell cannot make by itself
    // each runs at 1440 and 390 with the daemon's own answer held back, failed or replaced
    for (const width of [1440, 390]) {
      const { ctx: c, page: p } = await newPage(width);
      const q = T(p);
      const mark = (state) => `live ${state} ${width}`;

      // loading: the one read is held until the page has been looked at
      let release;
      const held = new Promise((r) => { release = r; });
      await p.route(`**${ROUTE}`, async (route) => { await held; await route.continue(); });
      one = expectOneCall(p, ROUTE);
      await p.goto(`${BASE}/runbooks`, { waitUntil: "commit" });
      await p.getByTestId("rb-loading").waitFor();
      ck(`${mark("loading")}: skeleton, no subtitle, no banner, no list rows`, (await q.services.count()) === 0 && (await q.banner.count()) === 0 && (await p.locator("h1 + p").count()) === 0);
      ck(`${mark("loading")}: says it is loading to assistive tech`, (await p.getByTestId("rb-loading").getAttribute("aria-busy")) === "true");
      ck(`${mark("loading")}: no sideways scroll`, (await overflow(p)) <= 0);
      await checkWords(p, ck, mark("loading"));
      await shot(p, `live-loading-${width}.png`);
      release();
      await q.title.waitFor();
      await one(ck, mark("loading"));
      await p.unroute(`**${ROUTE}`);

      // error: the read fails, the banner says when it will try again, Try now reads again
      let failing = true;
      await p.route(`**${ROUTE}`, (route) => (failing ? route.fulfill(json({ detail: "boom" }, 500)) : route.continue()));
      one = expectOneCall(p, ROUTE);
      await p.goto(`${BASE}/runbooks`, { waitUntil: "load" });
      await q.banner.waitFor();
      await one(ck, mark("error"));
      ck(`${mark("error")}: the banner words`, squash(await q.banner.innerText()).startsWith(ERROR_WORDS), squash(await q.banner.innerText()));
      ck(`${mark("error")}: an alert, with Try now, no subtitle, no list`, (await q.banner.getAttribute("role")) === "alert" &&
         (await q.banner.getByRole("button", { name: "Try now" }).isVisible()) && (await p.locator("h1 + p").count()) === 0 && (await q.services.count()) === 0);
      ck(`${mark("error")}: no sideways scroll`, (await overflow(p)) <= 0);
      await checkWords(p, ck, mark("error"));
      await shot(p, `live-error-${width}.png`);
      failing = false;
      await q.banner.getByRole("button", { name: "Try now" }).click();
      await q.title.waitFor();
      ck(`${mark("error")}: Try now loads the page`, (await q.banner.count()) === 0 && (await q.title.count()) === 1);
      await p.unroute(`**${ROUTE}`);

      // empty: the cell answers with no services
      await p.route(`**${ROUTE}`, (route) => route.fulfill(json({ services: [], empty_words: "No services yet. Once your connections send data, each service gets a page here for what the investigator should know." })));
      one = expectOneCall(p, ROUTE);
      await p.goto(`${BASE}/runbooks`, { waitUntil: "load" });
      await p.getByTestId("rb-empty").waitFor();
      await one(ck, mark("empty"));
      ck(`${mark("empty")}: the words, the short subtitle`, (await body(p)).includes("No services yet") && (await body(p)).includes(NO_SERVICES) && squash(await p.locator("h1 + p").innerText()) === SUBTITLE_SHORT);
      ck(`${mark("empty")}: no list, no add box`, (await q.services.count()) === 0 && (await q.input.count()) === 0);
      ck(`${mark("empty")}: no sideways scroll`, (await overflow(p)) <= 0);
      await checkWords(p, ck, mark("empty"));
      await shot(p, `live-empty-${width}.png`);
      await p.unroute(`**${ROUTE}`);

      // many: fifty services, service-02 open
      await p.route(`**${ROUTE}`, (route) => route.fulfill(json(manyServices())));
      one = expectOneCall(p, ROUTE);
      await p.goto(`${BASE}/runbooks?service=service-02`, { waitUntil: "load" });
      await q.title.waitFor();
      await one(ck, mark("many"));
      ck(`${mark("many")}: service-02 is open, with the full subtitle`, (await q.title.innerText()) === "service-02" && squash(await p.locator("h1 + p").innerText()) === SUBTITLE);
      const listed = width > 600 ? await q.services.count() : await q.picker.locator("option").count();
      ck(`${mark("many")}: fifty services`, listed === 50, String(listed));
      if (width > 600) {
        const counts = (await p.getByTestId("rb-count").allInnerTexts()).map(squash);
        ck(`${mark("many")}: every count reads right ("1 line", never "1 lines")`, counts.every((c) => /^(none|1 line|[2-9] lines)$/.test(c)) && counts.includes("1 line") && counts[1] === "3 lines", counts.slice(0, 8).join(","));
        ck(`${mark("many")}: the list scrolls inside its own box`, await q.list.locator("div").first().evaluate((e) => e.scrollHeight > e.clientHeight && e.clientHeight <= 560));
      }
      ck(`${mark("many")}: no sideways scroll`, (await overflow(p)) <= 0);
      await checkWords(p, ck, mark("many"));
      await shot(p, `live-many-${width}.png`);
      await p.unroute(`**${ROUTE}`);

      // addfail: the cell cannot save the line
      await p.route(`**/api/sre/runbooks/*/lines`, (route) => route.fulfill(json({ detail: NOT_SAVED }, 500)));
      await p.goto(`${BASE}/runbooks?service=edge-proxy`, { waitUntil: "load" });
      await q.lines.first().waitFor();
      const n0 = await q.lines.count();
      await q.input.fill("Restart edge-proxy one pod at a time.");
      await q.add.click();
      await q.error.waitFor();
      ck(`${mark("addfail")}: the cell's sentence, nothing added, the draft kept`, squash(await q.error.innerText()) === NOT_SAVED && (await q.lines.count()) === n0 && (await q.input.inputValue()) === "Restart edge-proxy one pod at a time.");
      ck(`${mark("addfail")}: no sideways scroll`, (await overflow(p)) <= 0);
      await checkWords(p, ck, mark("addfail"));
      await shot(p, `live-addfail-${width}.png`);
      await p.unroute(`**/api/sre/runbooks/*/lines`);
      // a cell that does not answer the write at all says the same, in the page's own words
      await p.route(`**/api/sre/runbooks/*/lines`, (route) => route.abort());
      await q.add.click();
      await p.waitForFunction((w) => document.querySelector("[data-testid=rb-add-error]")?.textContent === w, NOT_SAVED);
      ck(`${mark("addfail")}: a write that gets no answer says it was not saved`, squash(await q.error.innerText()) === NOT_SAVED);
      await p.unroute(`**/api/sre/runbooks/*/lines`);
      // a failed remove names itself
      await p.route(`**/api/sre/runbooks/*/lines/*`, (route) => route.abort());
      await q.removes.first().click();
      await p.getByTestId("rb-remove-error").waitFor();
      ck(`${mark("remove fail")}: it says nothing changed and the line stays`, squash(await p.getByTestId("rb-remove-error").innerText()) === "The line was not removed. Nothing changed, try again." && (await q.lines.count()) === n0);
      await p.unroute(`**/api/sre/runbooks/*/lines/*`);
      await c.close();
    }

    // ---------------------------------------------------------------- the samples: every state, both widths
    for (const width of [1440, 390]) {
      for (const state of STATES) {
        const { ctx: c, page: p } = await newPage(width, true);
        const q = T(p);
        const label = `sample ${state} ${width}`;
        const calls = [];
        p.on("request", (req) => { if (new URL(req.url()).pathname.startsWith("/api/") && new URL(req.url()).pathname !== "/api/whoami") calls.push(req.method() + " " + new URL(req.url()).pathname); });
        await p.goto(`${BASE}/runbooks?sample=${state}`, { waitUntil: "load" });
        if (state === "loading") await p.getByTestId("rb-loading").waitFor();
        else if (state === "error") await q.banner.waitFor();
        else if (state === "empty") await p.getByTestId("rb-empty").waitFor();
        else await q.title.waitFor();
        await settle(p);
        ck(`${label}: no call to the cell`, calls.length === 0, calls.join(","));
        ck(`${label}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        const text = await body(p);
        const wide = width > 600;
        const subtitle = state === "loading" || state === "error" ? null : state === "empty" ? SUBTITLE_SHORT : SUBTITLE;
        ck(`${label}: the subtitle`, subtitle === null ? (await p.locator("h1 + p").count()) === 0 : squash(await p.locator("h1 + p").innerText()) === subtitle,
           await p.locator("h1 + p").allInnerTexts().then((a) => a.join("|")));

        if (state === "normal") {
          ck(`${label}: edge-proxy, five lines, two notes`, (await q.title.innerText()) === "edge-proxy" && (await q.lines.count()) === 5 && (await q.memLines.count()) === 2);
          ck(`${label}: the lines carry who and when`, (await q.bys.first().innerText()).trim() === "Alex, 3 days ago" && (await q.bys.nth(3).innerText()).trim() === "you, today");
          if (wide) ck(`${label}: the five counts`, JSON.stringify((await p.getByTestId("rb-count").allInnerTexts()).map(squash)) === JSON.stringify(["5 lines", "3 lines", "2 lines", "1 line", "none"]));
        }
        if (state === "typing") ck(`${label}: the draft is in the box`, (await q.input.inputValue()) === "Restart edge-proxy one pod at a time, never all at once." && (await q.error.count()) === 0);
        if (state === "dupe") ck(`${label}: the same line, and the sentence`, (await q.input.inputValue()).startsWith("Rolling back is safe") && squash(await q.error.innerText()) === SAME);
        if (state === "addfail") ck(`${label}: the not-saved sentence`, squash(await q.error.innerText()) === NOT_SAVED);
        if (state === "nolines") ck(`${label}: docs-site, empty words, no notes`, (await q.title.innerText()) === "docs-site" && text.includes("Add the first line below. The investigator reads it from its next investigation of docs-site.") && (await q.memory.count()) === 0);
        if (state === "memonly") {
          ck(`${label}: service-a with no runbook, short words, its own notes`, (await q.title.innerText()) === "service-a" && text.includes("No runbook for service-a yet") &&
             !text.includes("next investigation of service-a") && (await q.memLines.count()) === 2 && !text.includes("restart of edge-proxy"), text.slice(0, 300));
          if (wide) ck(`${label}: the list says none for service-a`, squash(await q.count("service-a").innerText()) === "none");
          ck(`${label}: the notes are read only`, (await q.memory.locator("button, input, textarea, select, [contenteditable]").count()) === 0);
        }
        if (state === "empty") ck(`${label}: no services, the words`, text.includes("No services yet") && text.includes(NO_SERVICES) && (await q.input.count()) === 0);
        if (state === "loading") ck(`${label}: skeleton`, (await p.getByTestId("rb-loading").isVisible()) && (await q.services.count()) === 0);
        if (state === "error") ck(`${label}: the banner words, Try now`, squash(await q.banner.innerText()).startsWith(ERROR_WORDS) && (await q.banner.getByRole("button", { name: "Try now" }).isVisible()), squash(await q.banner.innerText()));
        if (state === "long") {
          ck(`${label}: checkout, the long line, Roll back`, (await q.title.innerText()) === "checkout" && (await texts(p))[0].length > 450 && (await texts(p))[1] === "Roll back the last deploy first." && !text.includes("Rollback"));
          ck(`${label}: read twelve times`, squash(await q.used.innerText()) === "Read by the investigator 12 times this week");
        }
        if (state === "many") {
          ck(`${label}: service-02 open with 3 lines`, (await q.title.innerText()) === "service-02" && (await q.lines.count()) === 3);
          if (wide) {
            const counts = (await p.getByTestId("rb-count").allInnerTexts()).map(squash);
            ck(`${label}: fifty services, counts in words, no "1 lines"`, counts.length === 50 && counts.every((x) => /^(none|1 line|[2-9] lines)$/.test(x)) && counts[1] === "3 lines", counts.slice(0, 8).join(","));
          } else ck(`${label}: fifty services in the picker`, (await q.picker.locator("option").count()) === 50);
        }
        ck(`${label}: ${wide ? "the list, no picker" : "the picker, no list"}`, state === "loading" || state === "error" || state === "empty" ||
           (wide ? (await q.list.isVisible()) && !(await q.picker.isVisible()) : (await q.picker.isVisible()) && !(await q.list.isVisible())));
        await checkWords(p, ck, label);
        await shot(p, `sample-${state}-${width}.png`);

        // a sample answers a write as the cell does, on its own copy
        if (width === 1440 && ["normal", "typing", "dupe", "addfail", "nolines"].includes(state)) {
          const n = await q.lines.count();
          await q.input.fill(state === "dupe" ? "Rolling back is safe: the last 5 deploys had no migrations." : "A new line from the sample.");
          await q.add.click();
          if (state === "dupe") { await p.waitForTimeout(150); ck(`${label}: a second add of the same line is refused`, squash(await q.error.innerText()) === SAME && (await q.lines.count()) === n); }
          else if (state === "addfail") { await q.error.waitFor(); ck(`${label}: an add fails again`, squash(await q.error.innerText()) === NOT_SAVED && (await q.lines.count()) === n); }
          else {
            await p.waitForFunction((k) => document.querySelectorAll("[data-testid=rb-line]").length === k, n + 1);
            ck(`${label}: an add shows the line, with you, today`, squash(await q.bys.last().innerText()) === "you, today" && (await texts(p)).at(-1) === "A new line from the sample.");
            await q.removes.last().click();
            await p.waitForFunction((k) => document.querySelectorAll("[data-testid=rb-line]").length === k, n);
            ck(`${label}: a remove takes it away`, (await q.lines.count()) === n);
          }
          ck(`${label}: and it never called the cell`, calls.length === 0, calls.join(","));
        }
        await c.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill("SIGTERM");
    await new Promise((r) => setTimeout(r, 500));
  }
  console.log(`\n${pass} ok, ${fail} FAIL`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
