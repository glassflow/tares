/*
 * Browser e2e for TR-735: the factory Reviews page.
 *
 * Two parts. Samples: with the browser flag taresSamples=1 each sample (l3, l4, empty, long, many, filt,
 * load, err) is opened at 1440 and 390 wide and shot as factory-reviews-<sample>-<width>.png, with the
 * words check, no horizontal scroll, and the rows, fold and counts of each state. Live: a scratch
 * factory cell seeded by tests/seed-factory-reviews.py (autonomy L3): the lists come from one read of
 * GET /api/factory/reviews, oldest first, split by the reviewer's verdict on the current commit; the
 * CI dots, the milestone line, the project filter, an unknown project, markup in a title shown as
 * text, the 20 second poll, and that ?sample= without the flag shows no sample.
 * Run:  npm run test:factory-reviews     (builds first; tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_PORT (default 8849), MCP port +1.
 * TARES_E2E_CHROME points Playwright at another Chrome binary.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8849);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["l3", "l4", "empty", "long", "many", "filt", "load", "err"];
const ROUTE = "/api/factory/reviews";

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

function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-reviews-e2e-"));
  const base = { PYTHONPATH: REPO, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                 TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off" };
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-reviews.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8", env: cleanEnv({ ...base, TARES_EDITION: "factory" }) })
    .trim().split("\n").pop());
  ck("seeded 4 projects", Object.keys(ids).length === 4, String(Object.keys(ids).length));

  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore",
    env: cleanEnv({ ...base, TARES_EDITION: "factory" }), detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const inside = (page) => page.evaluate(() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1);
    });
    const shot = async (page, name, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-reviews-${name}-${width}.png`), fullPage: true });
    };
    const fresh = async (width, flagged) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      if (flagged) await ctx.addInitScript(() => { try { window.localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const texts = (loc) => loc.allInnerTexts().then((a) => a.map((t) => t.replace(/\s+/g, " ").trim()));

    // ---- samples, at both widths
    for (const width of [1440, 390]) {
      const { ctx, page } = await fresh(width, true);
      const rows = page.getByTestId("review-row");
      const crewCount = page.getByTestId("crew-count");
      const tag = (s) => `${s} @${width}`;
      const open = async (s) => {
        await page.goto(`${BASE}/reviews?sample=${s}`, { waitUntil: "load" });
        if (s === "load") await page.getByTestId("reviews-loading").waitFor();
        else if (s === "err") await page.getByRole("alert").waitFor();
        else await page.getByTestId("reviews-autonomy").waitFor();
        await page.waitForLoadState("networkidle");
      };
      for (const s of SAMPLES) {
        await open(s);
        await shot(page, s, width);
        await checkWords(page, ck, tag(s));
        ck(`${tag(s)}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        ck(`${tag(s)}: nothing on the page is wider than the window`, await inside(page));
        ck(`${tag(s)}: the title is Reviews`, (await page.locator("h1").first().innerText()).trim() === "Reviews");
      }

      await open("l3");
      ck(`l3 @${width}: the autonomy sentence`, (await page.getByTestId("reviews-autonomy").innerText()) === "You merge every pull request after the crew's review.");
      ck(`l3 @${width}: 4 rows wait for you and the count says 4`, (await page.getByTestId("reviews-waiting").getByTestId("review-row").count()) === 4
         && (await page.getByTestId("waiting-count").innerText()) === "4");
      ck(`l3 @${width}: the crew's list is folded with its count`, (await crewCount.innerText()) === "3 +"
         && (await page.getByTestId("crew-list").count()) === 0 && (await page.getByTestId("crew-fold").getAttribute("aria-expanded")) === "false");
      ck(`l3 @${width}: a row carries project, ticket, PR, repo, wait, review, CI and what it holds up`,
         /^project-x · T14 · #250 on example-org\/service-a$/.test((await texts(rows.first().getByTestId("review-meta")))[0])
         && (await rows.first().getByTestId("review-waited").innerText()) === "10 days"
         && (await rows.first().getByTestId("review-verdict").innerText()) === "Passed, round 1"
         && (await rows.first().getByTestId("review-ci").innerText()).trim() === "CI passed"
         && (await rows.first().getByTestId("review-holds").innerText()) === "Holds up M2: Faster lists");
      await page.getByTestId("crew-fold").click();
      ck(`l3 @${width}: the fold opens to 3 more rows`, (await rows.count()) === 7 && (await crewCount.innerText()) === "3 −"
         && (await page.getByTestId("crew-fold").getAttribute("aria-expanded")) === "true");
      const crew = page.getByTestId("crew-list");
      ck(`l3 @${width}: with the reviewer's words and a CI dot per state`,
         (await texts(crew.getByTestId("review-verdict"))).join("|") === "Not reviewed yet|Changes asked, round 1|Passed on an older commit"
         && (await crew.getByTestId("review-row").evaluateAll((r) => r.map((x) => x.dataset.ci))).join(",") === "go,ok,ok"
         && (await texts(crew.getByTestId("review-ci"))).join("|") === "CI in progress|CI passed|CI passed");
      await page.getByTestId("crew-fold").click();
      ck(`l3 @${width}: and folds again`, (await rows.count()) === 4 && (await crewCount.innerText()) === "3 +");
      const link = rows.first().getByTestId("review-title");
      ck(`l3 @${width}: the title links to the ticket`, (await link.getAttribute("href")) === "/projects/project-x/tickets/T14");
      const pr = rows.first().getByTestId("review-pr");
      ck(`l3 @${width}: and the PR number to the pull request, in a new tab`, (await pr.getAttribute("href")) === "https://github.com/example-org/service-a/pull/250"
         && (await pr.getAttribute("target")) === "_blank" && /noreferrer/.test(await pr.getAttribute("rel")));

      await open("l4");
      ck(`l4 @${width}: the crew merges, said once`, (await page.getByTestId("reviews-autonomy").innerText()) === "The crew merges after its review and green CI.");
      ck(`l4 @${width}: nothing waits for you, and the crew's list is open with 7`, (await page.getByTestId("reviews-waiting-none").innerText()) === "Nothing waits for your review."
         && (await rows.count()) === 7 && (await crewCount.innerText()) === "7 −");
      await open("empty");
      ck(`empty @${width}: nothing anywhere, no crew list`, (await rows.count()) === 0 && (await page.getByTestId("reviews-crew").count()) === 0
         && (await page.getByTestId("waiting-count").innerText()) === "0");
      await open("many");
      ck(`many @${width}: 40 rows wait, all shown`, (await page.getByTestId("reviews-waiting").getByTestId("review-row").count()) === 40
         && (await page.getByTestId("waiting-count").innerText()) === "40");
      await open("long");
      ck(`long @${width}: a long name, title and milestone stay inside their row`, await rows.evaluateAll((r) => r.every((x) => x.scrollWidth <= x.clientWidth + 1)));
      await open("filt");
      ck(`filt @${width}: the filter line names the project and offers Show all`, /^Showing project-x\.\s*Show all$/.test((await texts(page.getByTestId("reviews-filter")))[0])
         && (await page.getByTestId("reviews-filter").getByRole("link", { name: "Show all" }).getAttribute("href")) === "/reviews");
      await open("load");
      ck(`load @${width}: placeholder, no rows`, (await page.getByRole("status", { name: "Loading" }).count()) === 1 && (await rows.count()) === 0);
      await open("err");
      ck(`err @${width}: the banner says what failed`, (await page.getByRole("alert").innerText()).replace(/\s+/g, " ").startsWith("Reviews did not load. The cell did not answer. Try again in a moment."));
      ck(`err @${width}: with a Retry button`, (await page.getByRole("button", { name: "Retry" }).count()) === 1);
      await ctx.close();
    }

    // ---- live, from the seeded cell, at both widths
    for (const width of [1440, 390]) {
      const { ctx, page } = await fresh(width, false);
      const rows = page.getByTestId("review-row");
      const waiting = page.getByTestId("reviews-waiting").getByTestId("review-row");
      const tag = (s) => `live ${s} @${width}`;
      const openLive = async (query = "") => {
        const done = expectOneCall(page, ROUTE);
        await page.goto(`${BASE}/reviews${query}`, { waitUntil: "load" });
        await page.getByTestId("reviews-autonomy").waitFor();
        await done(ck, tag(`/reviews${query ? " " + query : ""}`));
      };

      await openLive();
      await shot(page, "live", width);
      await checkWords(page, ck, tag("lists"));
      ck(`${tag("lists")}: no horizontal scroll`, (await overflow(page)) <= 0 && (await inside(page)));
      ck(`${tag("lists")}: the autonomy sentence is the crew's default, L3`, (await page.getByTestId("reviews-autonomy").innerText()) === "You merge every pull request after the crew's review.");
      ck(`${tag("lists")}: 5 wait for you, oldest first`, (await texts(waiting.getByTestId("review-title"))).join("|")
         === "Add the footer|Batch the lists|Add the sitemap|Tidy the nav|Add the search box", (await texts(waiting.getByTestId("review-title"))).join("|"));
      ck(`${tag("lists")}: their waits`, (await texts(waiting.getByTestId("review-waited"))).join("|") === "6 days|2 days|3 hours|1 hour|10 minutes",
         (await texts(waiting.getByTestId("review-waited"))).join("|"));
      ck(`${tag("lists")}: the reviewer's words, round 2 for the first`, (await texts(waiting.getByTestId("review-verdict"))).join("|")
         === "Passed, round 2|Passed, round 1|Passed, round 1|Passed, round 1|Passed, round 1");
      ck(`${tag("lists")}: CI words and dots: passed, failing, none`, (await texts(waiting.getByTestId("review-ci"))).join("|")
         === "CI passed|CI passed|CI failing|CI passed|No CI"
         && (await waiting.evaluateAll((r) => r.map((x) => x.dataset.ci))).join(",") === "ok,ok,bad,ok,none");
      const holds = await texts(waiting.getByTestId("review-holds"));
      ck(`${tag("lists")}: the milestone line shows only where the ticket holds one up`, holds.join("|") === "Holds up 1. Foundations|Holds up M2: Faster lists", holds.join("|"));
      ck(`${tag("lists")}: a ticket whose milestone has other open tickets, or none, shows no line`,
         (await waiting.nth(2).getByTestId("review-holds").count()) === 0);
      const dots = await waiting.getByTestId("review-ci").evaluateAll((e) => e.map((x) => getComputedStyle(x.querySelector("span, i")).backgroundColor));
      ck(`${tag("lists")}: a failing CI dot differs from a passing one`, dots[2] !== dots[0] && dots[4] !== dots[0], dots.join(" | "));
      ck(`${tag("lists")}: the crew's list is folded, with 3`, (await page.getByTestId("crew-count").innerText()) === "3 +");
      await page.getByTestId("crew-fold").click();
      const crew = page.getByTestId("crew-list").getByTestId("review-row");
      ck(`${tag("crew")}: oldest first`, (await texts(crew.getByTestId("review-title"))).join("|").startsWith("Add the news page|Fix ")
         && (await texts(crew.getByTestId("review-title"))).join("|").endsWith("|Add the archive"));
      ck(`${tag("crew")}: passed on an older commit, changes asked, not reviewed`, (await texts(crew.getByTestId("review-verdict"))).join("|")
         === "Passed on an older commit|Changes asked, round 1|Not reviewed yet");
      ck(`${tag("crew")}: a pull request still going says so`, (await texts(crew.last().getByTestId("review-ci")))[0] === "CI in progress");
      ck(`${tag("crew")}: a lone ticket in its milestone holds it up, named with its place`,
         (await texts(crew.getByTestId("review-holds"))).join("|") === "Holds up M2: <u>Speed</u>");

      // markup in the cell's text is text
      ck(`${tag("markup")}: a title with tags shows them as characters`,
         (await texts(crew.getByTestId("review-title"))).some((t) => t.includes('<b>bold</b> <img src=x onerror="window.__pwned=1">')));
      ck(`${tag("markup")}: no element came from it, and nothing ran`, (await page.locator("main img, main b, main u").count()) === 0
         && (await page.evaluate(() => window.__pwned)) === undefined);
      ck(`${tag("markup")}: a project name with tags shows as text`, (await texts(waiting.getByTestId("review-project"))).includes("project-<i>m</i>")
         && (await page.locator("main i").count()) === 0);
      await checkWords(page, ck, tag("with the crew's list open"));

      // the filter
      await openLive(`?project=${ids["project-x"]}`);
      ck(`${tag("filter")}: only project-x, 3 wait for you and 3 are with the crew`, (await waiting.count()) === 3
         && (await rows.evaluateAll((r, id) => r.every((x) => x.dataset.project === id), ids["project-x"])) && (await page.getByTestId("crew-count").innerText()) === "3 +",
         `${await waiting.count()}`);
      ck(`${tag("filter")}: the line names it and Show all goes back`, /^Showing project-x\.\s*Show all$/.test((await texts(page.getByTestId("reviews-filter")))[0])
         && (await page.getByTestId("reviews-filter").getByRole("link", { name: "Show all" }).getAttribute("href")) === "/reviews");
      await shot(page, "live-filtered", width);
      await page.getByTestId("reviews-filter").getByRole("link", { name: "Show all" }).click();
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="review-row"]').length === 5);
      ck(`${tag("filter")}: Show all lists every project again`, (await waiting.count()) === 5 && new URL(page.url()).search === "");
      await openLive(`?project=${ids["project-u"]}`);
      ck(`${tag("filter")}: a project with nothing in review shows empty lists`, (await rows.count()) === 0
         && (await page.getByTestId("reviews-waiting-none").count()) === 1 && (await page.getByTestId("reviews-filter").count()) === 1);
      await openLive("?project=nope");
      ck(`${tag("filter")}: an unknown project says so and lists nothing`, (await page.getByTestId("reviews-unknown").innerText()).startsWith("No project has that address.")
         && (await rows.count()) === 0);
      await shot(page, "live-unknown", width);

      // links
      await openLive();
      await waiting.first().getByTestId("review-title").click();
      await page.waitForLoadState("networkidle");
      ck(`${tag("links")}: a title opens its ticket inside its project`, new URL(page.url()).pathname === `/projects/${ids["project-x"]}/tickets/T1`, page.url());
      await ctx.close();
    }

    // ---- the 20 second poll, and an address that is not a sample
    {
      const { ctx, page } = await fresh(1440, false);
      let calls = 0;
      page.on("request", (r) => { if (r.method() === "GET" && new URL(r.url()).pathname === ROUTE) calls++; });
      await page.goto(`${BASE}/reviews`, { waitUntil: "load" });
      await page.getByTestId("reviews-autonomy").waitFor();
      await page.waitForLoadState("networkidle");
      const first = calls;
      await page.waitForTimeout(21_000);
      ck("the page reads again after 20 seconds", first === 1 && calls === 2, `${first} then ${calls}`);
      await ctx.close();
    }
    {
      const { ctx, page } = await fresh(1440, false);
      await page.goto(`${BASE}/reviews?sample=l4`, { waitUntil: "load" });
      await page.getByTestId("reviews-autonomy").waitFor();
      await page.waitForLoadState("networkidle");
      ck("no flag: ?sample=l4 shows the live lists", (await page.getByTestId("reviews-waiting").getByTestId("review-row").count()) === 5
         && (await page.getByTestId("reviews-autonomy").innerText()).startsWith("You merge"));
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
