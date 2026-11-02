/*
 * Browser e2e for TR-760: the factory ticket page (/projects/:id/tickets/:ref).
 *
 * One ticket, from one read of the cell (GET /api/factory/projects/{id}/tickets/{ref}): its state,
 * where it lives, who works on it, what it depends on and what waits for it, its pull request, every
 * review round with its findings (newest first, the latest open, three shown), the checks the builder
 * ran (eight shown), the calls and decisions, its history (ten shown), its working notes (a cap with
 * "Show the whole document") and the command that opens the builder's session. A ticket the project
 * does not have, and a project that is not a factory project, say so. The old ?view=ticket:<ref>
 * address lands here. Every state shows only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-ticket.py, invented names), then runs the real
 * daemon on it, serving the built console. The sample states (?sample=<name>, with the sample flag in
 * localStorage) need no data.
 * Run:  npm run test:factory-ticket     (from ui/; builds first; tares installed in ../.venv or on PATH)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8847);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["merged", "changes", "blocked", "empty", "long", "many", "load", "err", "missing"];
const THEME_MS = 600;

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-ticket-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-ticket.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  ck("seeded the projects", !!ids["project-y"] && !!ids["project-w"]);

  // a clean environment: only what the daemon is told
  const env = { HOME: process.env.HOME, PATH: process.env.PATH, PYTHONPATH: REPO, TARES_EDITION: "factory", TARES_HOME: home,
                TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off" };
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, sampleFlag = false) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
      if (sampleFlag) await ctx.addInitScript(() => { try { localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(THEME_MS); };
    const txt = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const raw = async (loc) => (await loc.textContent()).replace(/\s+/g, " ").trim();   // before the stylesheet's capitals
    const y = ids["project-y"];
    const open = async (page, ref, project = y) => {
      await page.goto(`${BASE}/projects/${project}/tickets/${ref}`, { waitUntil: "load" });
      await page.getByTestId("factory-ticket").waitFor();
    };

    // ── 1. T2 at 1440: the rich ticket ──────────────────────────────────────────────────────────
    const { ctx, page } = await newPage(1440);
    const T = (id) => page.getByTestId(id);
    const one = expectOneCall(page, `/api/factory/projects/${y}/tickets/T2`);
    await open(page, "T2");
    await one(ck, "T2");
    ck("only a blocked ticket shows a reason", (await T("ticket-blocked-reason").count()) === 0);
    ck("the reference, the state and the title", (await txt(T("ticket-ref"))) === "T2" && (await txt(T("ticket-state"))) === "Changes asked"
       && (await txt(T("ticket-title"))) === "Add the footer");
    ck("the milestone is named, and there is no link out for a ticket kept in Tares",
       (await txt(T("ticket-milestone"))) === "Milestone M1: The first articles" && (await T("ticket-lives-in").count()) === 0);
    ck("the back link names the project and goes to it",
       (await T("ticket-back").getAttribute("href")) === `/projects/${y}` && (await txt(T("ticket-back"))).includes("project-y"));
    ck("who works on it is a role", (await txt(T("fact-who"))).includes("Builder for M1"));
    ck("it depends on T1, with a link to that ticket",
       (await T("depends-row").count()) === 1 && (await T("depends-row").getByRole("link").getAttribute("href")) === `/projects/${y}/tickets/T1`
       && (await txt(T("depends-row"))).includes("Add the header") && (await txt(T("depends-row"))).includes("Merged"));
    ck("it blocks T3, with a link to that ticket",
       (await T("blocks-row").count()) === 1 && (await T("blocks-row").getByRole("link").getAttribute("href")) === `/projects/${y}/tickets/T3`
       && (await txt(T("blocks-row"))).includes("Blocked"));
    const prLink = T("fact-pr").getByRole("link");
    ck("the pull request opens in a new tab and says how it stands",
       (await prLink.getAttribute("href")) === "https://github.com/example-org/docs-site/pull/12" && (await prLink.getAttribute("target")) === "_blank"
       && (await txt(T("fact-pr"))).includes("PR #12, open, CI failing"));
    ck("the terminal command is the one that opens the builder's session",
       (await raw(T("command-text"))) === "tares factory crew attach project-y-build-m1");
    await T("command-copy").click();
    await page.getByRole("button", { name: "Copied" }).waitFor();
    ck("Copy puts the command on the clipboard", (await page.evaluate(() => navigator.clipboard.readText())) === "tares factory crew attach project-y-build-m1");

    // reviews: four rounds, three shown, the latest open
    ck("four rounds, three shown, newest first", (await T("ticket-reviews").locator("small, span").filter({ hasText: /^4 rounds$/ }).count()) === 1
       && (await T("round").count()) === 3 && (await T("round").first().getAttribute("data-round")) === "4");
    ck("the latest round is open and the older two are closed",
       (await T("round").nth(0).getAttribute("data-open")) === "true" && (await T("round").nth(1).getAttribute("data-open")) === "false"
       && (await T("round").nth(2).getAttribute("data-open")) === "false");
    ck("a closed round shows no findings", (await T("round").nth(2).getByTestId("finding").count()) === 0);
    await T("reviews-more").waitFor();
    ck("the button offers the one earlier round", (await txt(T("reviews-more"))) === "Show 1 earlier round");
    await T("reviews-more").click();
    ck("it shows round 1 and the button goes", (await T("round").count()) === 4 && (await T("reviews-more").count()) === 0
       && (await T("round").nth(3).getAttribute("data-round")) === "1");
    await T("round-toggle").nth(3).click();
    const round1 = T("round").nth(3);
    ck("opening round 1 shows its two findings", (await round1.getAttribute("data-open")) === "true" && (await round1.getByTestId("finding").count()) === 2);
    const fixed = round1.locator("[data-testid=finding][data-state=fixed]"), openF = round1.locator("[data-testid=finding][data-state=open]");
    ck("one is fixed in round 3, the other is open",
       (await fixed.count()) === 1 && (await txt(fixed)).includes("Fixed in round 3") && (await openF.count()) === 1 && (await txt(openF)).includes("A colour could be a token"));
    ck("a finding has its file and line, and says if it must be fixed",
       (await txt(fixed)).includes("src/footer.ts:12") && (await txt(fixed)).includes("Must fix") && (await txt(openF)).includes("Suggestion"));
    ck("a round says who looked, what was checked and what was not",
       (await txt(T("round").nth(3))).includes("Checked: ran the tests. Not checked: did not open the build in a browser"));
    await T("round-toggle").nth(3).click();
    ck("closing it again hides its findings", (await T("round").nth(3).getAttribute("data-open")) === "false" && (await T("round").nth(3).getByTestId("finding").count()) === 0);

    // checks: twelve, eight shown
    ck("twelve checks, eight shown", (await txt(T("ticket-checks").locator("span").filter({ hasText: /^12 ran$/ }))) === "12 ran" && (await T("check-row").count()) === 8
       && (await txt(T("checks-more"))) === "Show 4 more");
    const colour = (loc) => loc.evaluate((e) => getComputedStyle(e).color);
    const failing = T("check-row").filter({ hasText: "2 failed" }).locator("span").last();
    const passing = T("check-row").filter({ hasText: "passed, 0 failed" }).first().locator("span").last();
    ck("a failing result is shown in a different colour from a passing one", (await colour(failing)) !== (await colour(passing)), `${await colour(failing)} / ${await colour(passing)}`);
    await T("checks-more").click();
    ck("Show 4 more shows all twelve", (await T("check-row").count()) === 12 && (await T("checks-more").count()) === 0);

    // calls and decisions
    ck("three calls, in words", (await T("call-row").count()) === 3
       && (await txt(T("call-row").nth(0))).includes("Waiting for you") && (await txt(T("call-row").nth(1))).includes("Confirmed")
       && (await txt(T("call-row").nth(2))).includes("You changed it") && (await txt(T("call-row").nth(2))).includes("Use the brand font"));
    ck("one decision of yours", (await T("decision-row").count()) === 1 && (await txt(T("decision-row"))).startsWith("Tabs or spaces? Tabs"));

    // history: fourteen, ten shown
    ck("fourteen lines of history, ten shown", (await txt(T("ticket-history").locator("span").filter({ hasText: /^14 lines$/ }))) === "14 lines"
       && (await T("history-row").count()) === 10 && (await txt(T("history-more"))) === "Show 4 more");
    ck("a history line says what happened, who did it, and the reason as written",
       (await T("history-row").filter({ hasText: "asked for changes" }).count()) >= 1 && (await txt(T("history-row").first())).includes("by"));
    await T("history-more").click();
    ck("Show 4 more shows all fourteen", (await T("history-row").count()) === 14 && (await T("history-more").count()) === 0);

    // working notes: a cap, the document rendered as markdown, never raw HTML or pictures
    ck("the notes are capped, with the line count", (await T("notes-box").getAttribute("data-open")) === "false"
       && (await txt(T("ticket-notes").locator("span").filter({ hasText: /lines$/ }))).endsWith("lines") && (await txt(T("notes-toggle"))) === "Show the whole document");
    ck("the capped notes scroll inside their box", await T("notes-text").evaluate((e) => e.scrollHeight > e.clientHeight && getComputedStyle(e).overflowY === "auto"));
    ck("headings, a list and a table are shown as such", (await T("notes-text").locator("h1").count()) === 1 && (await T("notes-text").locator("li").count()) > 50
       && (await T("notes-text").locator("table").count()) === 1);
    ck("a picture in the notes is not loaded, and a link opens in a new tab", (await T("notes-text").locator("img").count()) === 0
       && (await T("notes-text").locator('a[href="https://example.com/style"]').getAttribute("target")) === "_blank");
    await checkWords(page, ck, "T2 at 1440");
    ck("T2: no sideways scroll at 1440", (await overflow(page)) <= 0, String(await overflow(page)));
    await T("notes-toggle").click();
    ck("Show the whole document opens it, and Show less closes it", (await T("notes-box").getAttribute("data-open")) === "true"
       && (await txt(T("notes-toggle"))) === "Show less" && (await T("notes-text").evaluate((e) => e.scrollHeight <= e.clientHeight + 1)));
    await settle(page);
    await shot(page, "live-T2-1440.png");
    await T("notes-toggle").click();

    // the old address, and the other tickets
    await page.goto(`${BASE}/projects/${y}?view=ticket:T2`, { waitUntil: "load" });
    await T("factory-ticket").waitFor({ timeout: 10000 }).catch(() => {});
    ck("?view=ticket:T2 lands on the ticket page, with no query left", new URL(page.url()).pathname === `/projects/${y}/tickets/T2` && new URL(page.url()).search === "", page.url());
    await T("depends-row").getByRole("link").click();
    await page.waitForFunction(() => document.querySelector('[data-testid=factory-ticket]')?.getAttribute("data-ref") === "T1", null, { timeout: 15000 }).catch(() => {});
    ck("a dependency link opens that ticket's page", new URL(page.url()).pathname === `/projects/${y}/tickets/T1` && (await T("ticket-ref").innerText()).trim() === "T1", page.url());
    ck("T1 is merged: the builder who finished it, no terminal, one passing round open",
       (await txt(T("ticket-state"))) === "Merged" && (await txt(T("fact-who"))).includes("Builder for M1") && (await T("fact-terminal").count()) === 0
       && (await T("round").count()) === 1 && (await T("round").getAttribute("data-open")) === "true" && (await txt(T("round"))).toLowerCase().includes("passed")
       && (await txt(T("fact-pr"))).includes("PR #11, merged"));
    ck("T1 has short notes, shown whole", (await T("notes-box").getAttribute("data-open")) === "true" && (await T("notes-toggle").count()) === 0);
    await checkWords(page, ck, "T1");
    await shot(page, "live-T1-1440.png");

    await open(page, "T4");
    ck("T4 has a sentence for every section that is empty",
       (await T("reviews-empty").count()) === 1 && (await T("checks-empty").count()) === 1 && (await T("calls-empty").count()) === 1
       && (await T("history-empty").count()) === 1 && (await T("notes-empty").count()) === 1);
    ck("and for the facts: not picked up, nothing depends on it, no pull request",
       (await txt(T("fact-who"))).includes("Not picked up yet") && (await txt(T("fact-depends"))).includes("Nothing") && (await txt(T("fact-blocks"))).includes("Nothing")
       && (await txt(T("fact-pr"))).includes("No pull request yet") && (await T("ticket-milestone").count()) === 0);
    await checkWords(page, ck, "T4");
    await shot(page, "live-T4-1440.png");

    await open(page, "T5");
    const lives = T("ticket-lives-in").getByRole("link");
    ck("T5 lives in Linear, with a link out", (await txt(lives)) === "Linear" && (await lives.getAttribute("href")) === "https://linear.example.com/project-y/PY-5"
       && (await lives.getAttribute("target")) === "_blank");

    await open(page, "T3");
    ck("T3 is blocked, held by the lead, with no terminal and a blocking review",
       (await txt(T("ticket-state"))) === "Blocked" && (await T("fact-terminal").count()) === 0 && (await txt(T("round"))).toLowerCase().includes("blocked"));
    ck("T3 says what it waits for, as the API words it",
       (await txt(T("ticket-blocked-reason"))).startsWith("Waiting for T2, "));
    await checkWords(page, ck, "T3");

    // a ticket the project does not have, and a project that is not a factory project
    await page.goto(`${BASE}/projects/${y}/tickets/T99`, { waitUntil: "load" });
    await T("ticket-notfound").waitFor();
    ck("a ticket the project does not have says so, with a way back", (await txt(T("ticket-notfound"))).includes("This ticket was not found")
       && (await T("ticket-back").getAttribute("href")) === `/projects/${y}` && (await T("factory-ticket").count()) === 0);
    await checkWords(page, ck, "T99");
    await page.goto(`${BASE}/projects/${ids["project-w"]}/tickets/T1`, { waitUntil: "load" });
    await T("ticket-notfound").waitFor();
    ck("a project that is not a factory project says the same", (await T("factory-ticket").count()) === 0);
    await ctx.close();

    // ── 2. 390 px: the live pages do not scroll sideways ────────────────────────────────────────
    {
      const { ctx: c, page: p } = await newPage(390);
      for (const ref of ["T2", "T1", "T4", "T6"]) {
        await open(p, ref);
        await settle(p);
        ck(`390: ${ref} does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, `390 ${ref}`);
        if (ref === "T2") {
          const facts = await p.getByTestId("ticket-facts").boundingBox(), reviews = await p.getByTestId("ticket-reviews").boundingBox();
          ck("390: the facts come before the reviews", facts.y < reviews.y, `${facts.y} vs ${reviews.y}`);
        }
        await shot(p, `live-${ref}-390.png`);
      }
      await c.close();
    }

    // ── 3. the sample states ────────────────────────────────────────────────────────────────────
    for (const name of SAMPLES) {
      for (const width of [1440, 390]) {
        const { ctx: c, page: p } = await newPage(width, true);
        await p.goto(`${BASE}/projects/uc_sample/tickets/T99?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("ticket-loading").waitFor();
        else if (name === "err") await p.getByRole("alert").waitFor();
        else if (name === "missing") await p.getByTestId("ticket-notfound").waitFor();
        else await p.getByTestId("factory-ticket").waitFor();
        await settle(p);
        const label = `${name} ${width}`;
        ck(`${label}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, label);
        const q = (id) => p.getByTestId(id);
        if (width === 1440) {
          if (name === "merged") {
            ck("merged: three rounds, all open, a link out to Linear, nobody works on it",
               (await q("round").count()) === 3 && (await q("round").evaluateAll((r) => r.every((e) => e.getAttribute("data-open") === "true")))
               && (await txt(q("ticket-lives-in"))) === "Lives in Linear" && (await txt(q("fact-who"))).includes("Nobody, it is finished"));
          } else if (name === "changes") {
            ck("changes: the builder's command, an open call, a pull request that passes",
               (await q("command-text").count()) === 1 && (await txt(q("call-row"))).includes("Waiting for you") && (await txt(q("fact-pr"))).includes("open, CI green"));
          } else if (name === "blocked") {
            ck("blocked: the Blocked state, no review, no pull request", (await txt(q("ticket-state"))) === "Blocked" && (await q("reviews-empty").count()) === 1
               && (await txt(q("fact-pr"))).includes("No pull request yet"));
          } else if (name === "empty") {
            ck("empty: a sentence for every section", (await q("reviews-empty").count()) === 1 && (await q("checks-empty").count()) === 1
               && (await q("calls-empty").count()) === 1 && (await q("notes-empty").count()) === 1);
          } else if (name === "long") {
            ck("long: the notes are capped and the 2,000 lines are counted", (await q("notes-box").getAttribute("data-open")) === "false"
               && (await txt(q("ticket-notes"))).includes("2,000 lines") && (await q("notes-toggle").count()) === 1);
            ck("long: nothing pushes out of its card", await p.evaluate(() => [...document.querySelectorAll("[data-testid=factory-ticket] *")]
              .every((e) => e.closest("[data-testid=notes-text]") || e.scrollWidth <= e.clientWidth + 1 || getComputedStyle(e).display === "inline" || e.tagName === "CODE")));
          } else if (name === "many") {
            ck("many: twelve rounds (three shown), forty lines of history (ten shown), twelve checks (eight shown)",
               (await q("round").count()) === 3 && (await txt(q("reviews-more"))) === "Show 9 earlier rounds" && (await q("history-row").count()) === 10
               && (await txt(q("history-more"))) === "Show 30 more" && (await q("check-row").count()) === 8 && (await txt(q("checks-more"))) === "Show 4 more");
          } else if (name === "load") {
            ck("load: the skeleton, and no ticket", (await q("ticket-loading").isVisible()) && (await q("factory-ticket").count()) === 0);
          } else if (name === "err") {
            ck("err: the banner and Retry", (await p.getByRole("alert").innerText()).includes("The ticket page did not load.") && (await p.getByRole("button", { name: "Retry" }).count()) === 1);
          } else if (name === "missing") {
            ck("missing: the ticket was not found", (await txt(q("ticket-notfound"))).includes("This ticket was not found") && (await q("factory-ticket").count()) === 0);
          }
        }
        if (name === "changes" || name === "many") {
          // the page in the dark theme too
          await p.emulateMedia({ colorScheme: "dark" });
          await p.waitForTimeout(THEME_MS);
          ck(`${label} dark: no sideways scroll`, (await overflow(p)) <= 0);
          await checkWords(p, ck, `${label} dark`);
          await shot(p, `${name}-dark-${width}.png`);
          await p.emulateMedia({ colorScheme: "light" });
          await p.waitForTimeout(THEME_MS);
        }
        await shot(p, `${{ load: "loading", err: "error" }[name] || name}-${width}.png`);
        await c.close();
      }
    }
    // the address alone shows no sample on a live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/projects/${y}/tickets/T2?sample=empty`, { waitUntil: "load" });
      await p.getByTestId("factory-ticket").waitFor();
      ck("the sample address alone shows the live cell", (await p.getByTestId("ticket-title").innerText()).trim() === "Add the footer" && (await p.getByTestId("reviews-empty").count()) === 0);
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
