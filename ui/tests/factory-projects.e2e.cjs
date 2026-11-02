/*
 * Browser e2e for TR-733: the factory Projects list (/projects).
 *
 * Every software factory project is one row (goal, state, current milestone, tickets merged and
 * blocked, repos, last activity) from one read of the cell (GET /api/factory/projects). The page
 * filters by state in people's words, with the filter in the address (?state=); Open, the default,
 * leaves the finished ones behind a "Show n finished" link. A project whose tickets are all merged
 * says "Ready to close" and has a Close button that asks first. A project that is not a factory
 * project is left out. There is no objects column and no Factory badge. A name or goal with markup
 * shows as text. Every state shows only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-projects.py, invented names), then runs the real
 * daemon on it, serving the built console. The sample states (?sample=<name>, with the sample flag
 * in localStorage) need no data.
 * Run:  npm run build && node tests/factory-projects.e2e.cjs     (from ui/, tares installed in ../.venv)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8843);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["busy", "empty", "many", "long", "load", "err", "ready", "dialog", "fail"];
const ROUTE = "/api/factory/projects";
const SERVER_WORDS = "2 pull requests of this project are still open. Merge or close them first.";

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

// "2 Nov": the day and month of today in UTC, as the cell words a finish date
const today = () => {
  const d = new Date();
  return `${d.getUTCDate()} ${d.toLocaleString("en", { month: "short", timeZone: "UTC" })}`;
};

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-projects-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-projects.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded the projects", !!ids["project-b1"] && !!ids["project-z"] && !!ids["project-d"]);

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

    const { ctx, page } = await newPage(1440);
    const rows = page.getByTestId("project-row");
    const names = () => page.getByTestId("project-name").allInnerTexts().then((a) => a.map((t) => t.trim()));
    const row = (name) => rows.filter({ has: page.getByTestId("project-name").filter({ hasText: new RegExp(`^${name}$`) }) });
    const tab = (k) => page.getByTestId(`tab-${k}`);
    const counts = () => page.getByTestId("tab-count").allInnerTexts();
    const states = () => rows.evaluateAll((r) => [...new Set(r.map((x) => x.dataset.state))]);
    const pick = async (k) => { await tab(k).click(); await page.waitForTimeout(100); };
    const apiReads = [];
    page.on("request", (req) => { if (req.method() === "GET" && new URL(req.url()).pathname.startsWith("/api/")) apiReads.push(new URL(req.url()).pathname); });

    // 1. one call feeds the page; Open is the default and hides the finished
    const one = expectOneCall(page, ROUTE);
    await page.goto(`${BASE}/projects`, { waitUntil: "load" });
    await rows.first().waitFor();
    await one(ck, "/projects");
    ck("the page has its heading", await page.getByRole("heading", { name: "Projects", exact: true }).isVisible());
    ck("New project links to /projects/new", (await page.getByTestId("new-project").getAttribute("href")) === "/projects/new");
    ck("Open is the chosen tab", (await tab("open").getAttribute("aria-selected")) === "true");
    ck("Open lists what is not finished, what needs you first",
       JSON.stringify(await names()) === JSON.stringify(["project-w", "project-b1", "project-b2", "project-y", "project-q", "project-z"]), (await names()).join(","));
    ck("a project that is not a factory project is not listed", !(await page.getByTestId("projects-list").innerText()).includes("project-n"));
    ck("the tabs are in order: Open, Waiting on you, Building, Not started, Ready to close, Done, All",
       JSON.stringify(await page.getByRole("tab").evaluateAll((t) => t.map((x) => x.firstChild.textContent.trim()))) ===
       JSON.stringify(["Open", "Waiting on you", "Building", "Not started", "Ready to close", "Done", "All"]));
    ck("each tab has its count", JSON.stringify(await counts()) === JSON.stringify(["6", "1", "2", "1", "2", "2", "8"]), (await counts()).join(","));
    ck("the finished ones are behind a link", (await page.getByTestId("show-finished").innerText()).trim() === "Show 2 finished");
    ck("the address has no filter for Open", !new URL(page.url()).searchParams.has("state"), page.url());

    // 2. no objects column, no Factory badge
    const listText = await page.getByTestId("projects-list").innerText();
    ck("no objects column or word", !/objects?/i.test(await page.locator("main").innerText()) && (await page.locator("table, [role=columnheader]").count()) === 0);
    ck("no Factory badge or emoji on a row", !/factory/i.test(listText) && !/\p{Extended_Pictographic}/u.test(listText));
    await checkWords(page, ck, "open list");
    await shot(page, "live-open-1440.png");

    // 3. a row: name link, state, goal, repos and meta, bar, counts, milestone
    const b1 = row("project-b1");
    ck("a name links to its project", (await b1.getByTestId("project-name").getAttribute("href")) === `/projects/${ids["project-b1"]}`);
    ck("the state is in words", (await b1.getByTestId("project-state").textContent()).trim() === "Building" &&
       (await row("project-w").getByTestId("project-state").textContent()).trim() === "Waiting on you" &&
       (await row("project-y").getByTestId("project-state").textContent()).trim() === "Not started");
    ck("tickets merged and blocked", (await b1.getByTestId("project-tickets").innerText()).replace(/\s+/g, " ").trim() === "1 of 4 tickets merged 1 blocked",
       await b1.getByTestId("project-tickets").innerText());
    ck("a project with nothing blocked says nothing about it", !/blocked/.test(await row("project-b2").getByTestId("project-tickets").innerText()));
    ck("the bar says what it measures", (await b1.getByRole("progressbar").getAttribute("aria-label")) === "1 of 4 tickets merged");
    ck("the milestone line", (await b1.getByTestId("project-milestone").innerText()).trim() === "Now on M1: Foundations");
    ck("a project not started says how many milestones", (await row("project-y").getByTestId("project-milestone").innerText()).trim() === "2 milestones");
    ck("the repos are listed in full, then who and when",
       /^example-org\/service-a, example-org\/docs-site · No builders working · Active \d+ minutes? ago$/.test((await b1.getByTestId("project-meta").innerText()).trim()),
       await b1.getByTestId("project-meta").innerText());
    ck("the waiting project says so, and how long since", /Active 1 day ago/.test(await row("project-w").getByTestId("project-meta").innerText()));
    ck("the goal is two lines at most", await b1.getByTestId("project-goal").evaluate((e) => getComputedStyle(e).webkitLineClamp === "2" && getComputedStyle(e).overflow === "hidden"));
    ck("name and goal are marked as the person's own text", (await rows.first().locator("[data-testid=project-name][data-user-text]").count()) === 1
       && (await rows.first().locator("[data-testid=project-goal][data-user-text]").count()) === 1);
    const y = row("project-y");
    ck("markup in a goal is shown as text", (await y.getByTestId("project-goal").innerText()).startsWith("Let customers <b>connect</b> their own tools <img src=x"), await y.getByTestId("project-goal").innerText());
    ck("and is never run", (await y.locator("b, img").count()) === 0 && (await page.evaluate(() => window.__pwned)) === undefined);

    // 4. Close is offered only for a project with every ticket merged
    ck("two projects are ready to close, each says so", (await row("project-q").getByTestId("project-state").textContent()).trim() === "Ready to close"
       && (await row("project-z").getByTestId("project-state").textContent()).trim() === "Ready to close");
    ck("only they have a Close button", (await page.getByTestId("project-close").count()) === 2 && (await row("project-q").getByTestId("project-close").count()) === 1
       && (await row("project-b1").getByTestId("project-close").count()) === 0);
    ck("the milestone line of a ready project", (await row("project-q").getByTestId("project-milestone").innerText()).trim() === "All milestones done");

    // 5. each filter shows only its state, and lives in the address
    const reads0 = apiReads.length;
    for (const [k, state, n] of [["waiting_on_you", "waiting_on_you", 1], ["building", "building", 2], ["not_started", "not_started", 1],
                                 ["ready_to_close", "ready_to_close", 2], ["done", "done", 2], ["all", null, 8]]) {
      await pick(k);
      const got = await states();
      ck(`${k}: ${n} row${n === 1 ? "" : "s"}, ${state ? "only that state" : "every state"}`,
         (await rows.count()) === n && (state ? got.length === 1 && got[0] === state : got.length === 5), `${await rows.count()} rows, ${got}`);
      ck(`${k}: the address has it`, new URL(page.url()).searchParams.get("state") === k, page.url());
      ck(`${k}: no link to the finished`, (await page.getByTestId("show-finished").count()) === 0);
      await checkWords(page, ck, k);
    }
    ck("a filter makes no new read of the cell", apiReads.length === reads0, apiReads.slice(reads0).join(","));
    ck("a finished project is a row", (await names()).length === 8 && (await names()).includes("project-d"));
    await pick("done");
    ck("a finished project says when, and has no Close", (await row("project-d").getByTestId("project-milestone").innerText()).trim() === `Finished ${today()}`
       && (await page.getByTestId("project-close").count()) === 0, await row("project-d").getByTestId("project-milestone").innerText());
    await shot(page, "live-done-1440.png");
    await pick("open");
    await page.getByTestId("show-finished").click();
    ck("Show 2 finished opens the Done filter", (await tab("done").getAttribute("aria-selected")) === "true" && new URL(page.url()).searchParams.get("state") === "done");

    // 6. the filter survives a full load; a filter the page does not know falls back to Open
    await page.goto(`${BASE}/projects?state=building`, { waitUntil: "load" });
    await rows.first().waitFor();
    ck("?state=building opens Building on a full load", (await tab("building").getAttribute("aria-selected")) === "true" && (await rows.count()) === 2);
    await page.goto(`${BASE}/projects?state=nonsense`, { waitUntil: "load" });
    await rows.first().waitFor();
    ck("an unknown filter opens Open", (await tab("open").getAttribute("aria-selected")) === "true" && (await rows.count()) === 6);

    // 7. search by name or goal, with no read of the cell
    const search = page.getByTestId("projects-search");
    const reads1 = apiReads.length;
    await search.fill("gateway");
    ck("search finds a goal", JSON.stringify(await names()) === JSON.stringify(["project-w"]), (await names()).join(","));
    await search.fill("PROJECT-B");
    ck("search finds a name, in any case", JSON.stringify(await names()) === JSON.stringify(["project-b1", "project-b2"]), (await names()).join(","));
    await search.fill("zzzzzz");
    ck("search with no match says so", (await page.getByTestId("projects-none").innerText()).trim() === "No project matches your search.");
    ck("search makes no read of the cell", apiReads.length === reads1);
    await checkWords(page, ck, "search with no match");
    await search.fill("");
    await pick("not_started");
    await pick("done");
    await search.fill("");

    // 8. Close asks first; a refusal shows the cell's sentence in the row; Cancel closes nothing
    await page.goto(`${BASE}/projects`, { waitUntil: "load" });
    await rows.first().waitFor();
    const posts = [];
    page.on("request", (req) => { if (req.method() === "POST") posts.push({ url: new URL(req.url()).pathname, body: req.postData() }); });
    await row("project-q").getByTestId("project-close").click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    ck("the dialog names the project and says what Close does", (await dialog.innerText()).includes("Close project-q?") &&
       (await dialog.innerText()).includes("It moves to Done. Handing it to the crew again opens it."), await dialog.innerText());
    await checkWords(page, ck, "close dialog");
    await shot(page, "live-dialog-1440.png");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await dialog.waitFor({ state: "hidden" });
    ck("Cancel closes nothing", posts.length === 0 && (await row("project-q").count()) === 1);
    await page.route(`**/api/projects/${ids["project-q"]}/finish`, (r) => r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: SERVER_WORDS }) }));
    await row("project-q").getByTestId("project-close").click();
    await dialog.getByTestId("close-confirm").click();
    await row("project-q").getByTestId("project-error").waitFor();
    ck("a 409 shows the cell's sentence in the row", (await row("project-q").getByTestId("project-error").innerText()).trim() === SERVER_WORDS);
    ck("and the project is still ready to close", (await row("project-q").getByTestId("project-close").count()) === 1 && (await dialog.count()) === 0);
    await checkWords(page, ck, "close refused");
    await page.unroute(`**/api/projects/${ids["project-q"]}/finish`);

    // 9. Close, then confirm: the project moves to Done
    posts.length = 0;
    await row("project-z").getByTestId("project-close").click();
    await dialog.getByTestId("close-confirm").click();
    await dialog.waitFor({ state: "hidden" });
    await row("project-z").waitFor({ state: "detached" });
    await settle(page);
    const sent = posts.filter((p) => p.url === `/api/projects/${ids["project-z"]}/finish`);
    ck("Close sends one finish for that project, with the words", sent.length === 1 && JSON.parse(sent[0].body).words === "Closed from the Projects list", JSON.stringify(sent));
    ck("it leaves Open", !(await names()).includes("project-z") && (await rows.count()) === 5);
    ck("the counts follow: Ready to close 1, Done 3, Open 5", JSON.stringify(await counts()) === JSON.stringify(["5", "1", "2", "1", "1", "3", "8"]), (await counts()).join(","));
    ck("the link to the finished follows", (await page.getByTestId("show-finished").innerText()).trim() === "Show 3 finished");
    await pick("done");
    ck("it is in Done, finished today", (await row("project-z").count()) === 1 && (await row("project-z").getByTestId("project-state").textContent()).trim() === "Done"
       && (await row("project-z").getByTestId("project-milestone").innerText()).trim() === `Finished ${today()}`, await row("project-z").innerText());
    ck("and has no Close", (await row("project-z").getByTestId("project-close").count()) === 0);
    await ctx.close();

    // 10. 390 px: the live page does not scroll sideways, in any filter
    {
      const { ctx: c, page: p } = await newPage(390);
      await p.goto(`${BASE}/projects`, { waitUntil: "load" });
      await p.getByTestId("project-row").first().waitFor();
      ck("390: Open does not scroll sideways", (await overflow(p)) <= 0, String(await overflow(p)));
      await checkWords(p, ck, "390 open");
      await shot(p, "live-open-390.png");
      await p.goto(`${BASE}/projects?state=all`, { waitUntil: "load" });
      await p.getByTestId("project-row").first().waitFor();
      ck("390: All does not scroll sideways", (await overflow(p)) <= 0, String(await overflow(p)));
      await p.goto(`${BASE}/projects?state=done`, { waitUntil: "load" });
      await p.getByTestId("project-row").first().waitFor();
      await shot(p, "done-390.png");
      ck("390: the tabs wrap instead of scrolling", await p.getByRole("tablist").evaluate((e) => e.scrollWidth <= e.clientWidth + 1));
      await c.close();
    }
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/projects?state=done`, { waitUntil: "load" });
      await p.getByTestId("project-row").first().waitFor();
      await shot(p, "done-1440.png");
      await c.close();
    }

    // 11. the sample states
    for (const name of SAMPLES) {
      for (const width of [1440, 390]) {
        const { ctx: c, page: p } = await newPage(width, true);
        await p.goto(`${BASE}/projects?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("projects-loading").waitFor();
        else if (name === "err") await p.getByRole("alert").waitFor();
        else if (name === "empty") await p.getByTestId("projects-empty").waitFor();
        else await p.getByTestId("project-row").first().waitFor();
        await settle(p);
        const label = `${name} ${width}`;
        ck(`${label}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, label);
        const at = p.getByTestId("project-row");
        if (width === 1440) {
          if (name === "busy") {
            ck("busy: Open lists four, with the link to four finished", (await at.count()) === 4 && (await p.getByTestId("show-finished").innerText()).trim() === "Show 4 finished");
          } else if (name === "empty") {
            ck("empty: one sentence and New project", (await p.getByTestId("projects-empty").innerText()).replace(/\s+/g, " ").trim() === "No projects yet. Start one and hand it to the crew. New project"
               && (await p.getByTestId("projects-search").count()) === 0 && (await p.getByRole("tab").count()) === 0);
          } else if (name === "many") {
            ck("many: 60 projects, 50 of them open", (await at.count()) === 50 && (await counts2(p))[6] === "60", String(await at.count()));
          } else if (name === "long") {
            const long = at.first();
            const goalLen = (await long.getByTestId("project-goal").getAttribute("title")).length;
            ck("long: a 300-character goal stays at two lines", goalLen >= 300 &&
               (await long.getByTestId("project-goal").evaluate((e) => e.clientHeight < e.scrollHeight && e.clientHeight < 2.5 * parseFloat(getComputedStyle(e).lineHeight))), String(goalLen));
            ck("long: the long name wraps inside the row", (await long.getByTestId("project-name").innerText()).length >= 60 &&
               (await long.evaluate((e) => e.scrollWidth <= e.clientWidth)));
          } else if (name === "load") {
            ck("load: the loading rows, and no row", (await at.count()) === 0 && (await p.getByTestId("projects-loading").isVisible()));
          } else if (name === "err") {
            ck("err: the banner and Retry", (await p.getByRole("alert").innerText()).includes("Projects did not load. The cell did not answer. Try again in a moment.")
               && (await p.getByRole("button", { name: "Retry" }).count()) === 1);
          } else if (name === "ready") {
            ck("ready: two projects, each with Close", (await at.count()) === 2 && (await p.getByTestId("project-close").count()) === 2);
            await p.getByTestId("project-close").first().click();
            await p.getByTestId("close-confirm").click();
            await at.filter({ hasText: "Done" }).first().waitFor();
            ck("ready: Close moves the project to Done on the sample", (await p.getByTestId("project-close").count()) === 1 && (await counts2(p))[4] === "1");
          } else if (name === "dialog") {
            ck("dialog: asks to close project-v", (await p.getByRole("dialog").innerText()).includes("Close project-v?"));
          } else if (name === "fail") {
            ck("fail: the refusal is under the row", (await p.getByTestId("project-error").innerText()).trim() === SERVER_WORDS);
            await p.getByTestId("project-close").click();
            await p.getByTestId("close-confirm").click();
            await p.getByTestId("project-error").waitFor();
            ck("fail: Close answers with the same sentence", (await p.getByTestId("project-error").innerText()).trim() === SERVER_WORDS && (await p.getByTestId("project-close").count()) === 1);
          }
        }
        const shotName = { load: "loading", err: "error", ready: "ready-to-close" }[name] || name;
        if (name !== "ready" || width === 390) await shot(p, `${shotName}-${width}.png`);
        if (name === "ready" && width === 1440) {
          await p.goto(`${BASE}/projects?sample=ready`, { waitUntil: "load" });
          await p.getByTestId("project-row").first().waitFor();
          await settle(p);
          await shot(p, "ready-to-close-1440.png");
        }
        await c.close();
      }
    }
    // the address alone shows no sample on a live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/projects?sample=empty`, { waitUntil: "load" });
      await p.getByTestId("project-row").first().waitFor();
      ck("the sample address alone shows the live cell", (await p.getByTestId("projects-empty").count()) === 0);
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

function counts2(page) { return page.getByTestId("tab-count").allInnerTexts(); }
