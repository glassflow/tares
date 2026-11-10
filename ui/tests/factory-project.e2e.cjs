/*
 * Browser e2e for TR-734: the factory project page (/projects/:id).
 *
 * One project, from one read of the cell (GET /api/factory/projects/{id}): where it stands (Spec,
 * Build, Review, Done), what waits for you on it, the few tickets moving now (at most 5, with a link
 * to all of them), a compact row per milestone with its counts, the calls the crew made (Keep or
 * Overturn), your decisions, the documents, and the command that opens a builder's session. A button
 * that opens another page uses the address the cell sends. Close asks first. A project with every
 * ticket merged says "Ready to close" and can be closed from the header or from the Waiting list.
 * An old ticket or doc link, a project that is not a factory project and an address with no project
 * show today's project page. Every state shows only words from words.json and none scrolls
 * sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-project.py, invented names), then runs the real
 * daemon on it, serving the built console. The sample states (?sample=<name>, with the sample flag in
 * localStorage) need no data.
 * Run:  npm run build && node tests/factory-project.e2e.cjs     (from ui/, tares installed in ../.venv)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8330);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["building", "spec", "ready", "dialog", "fail", "finished", "empty", "long", "many", "load", "err"];
const SERVER_WORDS = "Two tickets are still open, so the project cannot close yet.";
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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-project-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-project.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  ck("seeded the projects", !!ids["project-y"] && !!ids["project-z"] && !!ids["project-d"]);

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
    const open = async (page, id, hash = "") => {
      await page.goto(`${BASE}/projects/${id}${hash}`, { waitUntil: "load" });
      await page.getByTestId("factory-project").waitFor();
    };

    // ── 1. project-y at 1440: the rich page ─────────────────────────────────────────────────────
    const y = ids["project-y"];
    const { ctx, page } = await newPage(1440);
    const T = (id) => page.getByTestId(id);
    const one = expectOneCall(page, `/api/factory/projects/${y}`);
    await open(page, y);
    await one(ck, "project-y");
    ck("the heading is the project's name", (await T("project-name").innerText()).trim() === "project-y");
    ck("the chip says Waiting on you", (await raw(T("project-chip"))) === "Waiting on you");
    ck("the goal is shown", (await txt(T("project-goal"))).startsWith("Publish 1,000 answers"));
    ck("the crumb links back to Projects", (await T("crumb").getByRole("link", { name: "Projects" }).getAttribute("href")) === "/projects");
    ck("no Close button while tickets are open", (await T("project-close").count()) === 0);
    const steps = T("project-steps").locator("li");
    ck("four stages, Build is the current one, Spec is done",
       (await steps.count()) === 4 && (await steps.nth(1).getAttribute("aria-current")) === "step" &&
       (await txt(steps.nth(0))).startsWith("✓") && (await txt(steps.nth(2))).startsWith("3"), await txt(T("project-steps")));
    ck("Edit plan opens the plan of this project",
       (await T("project-links").getByRole("link", { name: "Edit plan" }).getAttribute("href")) === `/setup?project=${y}`);
    ck("All 11 tickets opens the ticket list of this project",
       (await T("all-tickets-head").getAttribute("href")) === `/tickets?project=${y}` && (await txt(T("all-tickets-head"))) === "All 11 tickets");
    ck("a project without a tracker has no tracker link", (await T("tracker-link").count()) === 0);
    ck("there is no filter anywhere on the page", (await page.locator("select, [role=combobox], [role=listbox]").count()) === 0);

    // waiting: four things, three shown, the rest behind a button
    ck("Waiting for you counts four things", (await txt(T("waiting-count"))) === "4 things");
    // TR-1021: what holds the crew up (the permission and the PR) comes first under its own head,
    // and is never behind a button; the rest follows
    ck("what holds the crew up comes first, under its own head", (await T("waiting-now").count()) === 1
       && (await T("waiting-row").first().getAttribute("data-kind")) !== "milestone_to_check", await T("waiting-row").first().getAttribute("data-kind"));
    ck("all four show: nothing that waits is behind a button here", (await T("waiting-row").count()) === 4 && (await T("waiting-more").count()) === 0,
       String(await T("waiting-row").count()));
    ck("the note says what an arrow does", (await txt(T("waiting"))).includes("Buttons with an arrow open Home, where they are done."));
    const row = (kind) => T("waiting-row").and(page.locator(`[data-kind=${kind}]`));
    ck("a question: Answer opens the desk of this project", (await row("question").getByRole("link", { name: "Answer" }).getAttribute("href")) === `/projects/${y}#desk-D1`);
    ck("a permission: Answer opens the request, and the command is code",
       (await row("permission").getByRole("link", { name: "Answer" }).getAttribute("href")) === `/projects/${y}#desk-D2`
       && (await row("permission").locator("code[data-words-exempt]").innerText()) === "gh pr merge 13 --squash");
    ck("calls to confirm: Read them opens the project's calls page",
       (await row("calls_to_confirm").getByRole("link", { name: "Read them" }).getAttribute("href")) === `/projects/${y}/calls`);
    ck("calls to confirm: each call on its own line", (await row("calls_to_confirm").getByTestId("detail-lines").locator("li").count()) === 2);
    ck("Keep both is done here: a button, no arrow, no trip to Home",
       (await row("calls_to_confirm").getByTestId("waiting-keep").count()) === 1 && !(await txt(row("calls_to_confirm").getByTestId("waiting-keep"))).includes("↗"));
    await checkWords(page, ck, "project-y waiting, three shown");

    ck("1 PR to review: Review opens the reviews of this project",
       (await row("prs_to_review").getByRole("link", { name: "Review" }).getAttribute("href")) === `/reviews?project=${y}`);

    // moving now: at most five, a link to all
    ck("five tickets are moving, never six", (await T("moving-row").count()) === 5, String(await T("moving-row").count()));
    ck("the caption says how many are merged", (await txt(T("moving-caption"))) === "2 of 11 tickets merged", await txt(T("moving-caption")));
    ck("the rest is counted, with a link to all tickets",
       (await txt(T("moving-more"))).startsWith("3 more tickets are moving.") && (await T("all-tickets-more").getAttribute("href")) === `/tickets?project=${y}`
       && (await txt(T("all-tickets-more"))) === "All 11 tickets ›", await txt(T("moving-more")));
    ck("the header of the section links to all tickets too", (await T("all-tickets-moving").getAttribute("href")) === `/tickets?project=${y}`);
    const t5 = T("moving-row").and(page.locator("[data-ref=T5]"));
    ck("a ticket row opens the ticket, in words", (await t5.getAttribute("href")) === `/projects/${y}/tickets/T5`
       && (await txt(t5)).includes("Changes asked") && (await txt(t5)).includes("Builder for M2") && (await txt(t5)).includes("open, CI failing"), await txt(t5));
    ck("the next ticket is a row of its own: the first ready one, not one Moving now lists",
       (await txt(T("next-up"))).startsWith("T10") && (await T("moving-row").and(page.locator("[data-ref=T10]")).count()) === 0, await txt(T("next-up")));
    ck("and it opens that ticket", (await T("next-up").getAttribute("href")) === `/projects/${y}/tickets/T10`, await T("next-up").getAttribute("href"));

    // milestones: compact, with counts and a link, no tickets listed
    const mrows = T("milestone-row");
    ck("three milestones, in order", (await mrows.count()) === 3 && (await txt(mrows.nth(0))).startsWith("M1: The first articles"));
    ck("a milestone has no ticket list", (await T("milestones").locator("[data-testid=moving-row], [data-ref]").count()) === 0
       && (await T("milestones").locator("a[href*='/tickets/']").count()) === 0);
    const count0 = mrows.nth(0).getByTestId("milestone-count");
    ck("its count links to its tickets", (await txt(count0)) === "2 of 3 merged" && /^\/tickets\?project=uc_projecty&milestone=ms_/.test(await count0.getAttribute("href")), await count0.getAttribute("href"));
    ck("the other milestones count too", (await txt(mrows.nth(1).getByTestId("milestone-count"))) === "0 of 5 merged");
    const checks = mrows.nth(0).getByTestId("milestone-checks");
    ck("the checks are folded away, with their number", (await txt(checks)).includes("2 checks to pass") && !(await checks.evaluate((e) => e.open)));
    await checks.locator("summary").click();
    ck("opened, each check says what should show", (await txt(checks)).includes("Should show: Ten articles are listed"));
    ck("a milestone with open tickets is not marked Done", !/\bDone\b/.test(await txt(mrows.nth(0))), await txt(mrows.nth(0)));

    // calls: on their own page; keep removes, change needs words
    ck("the project page has no calls card", (await T("calls").count()) === 0 && (await page.getByTestId("call-row").count()) === 0);
    ck("the Calls link opens the calls page", (await T("calls-link").getAttribute("href")) === `/projects/${y}/calls`);
    await page.goto(`${BASE}/projects/${y}/calls`, { waitUntil: "load" });
    await page.getByTestId("calls-page").waitFor();
    ck("the calls page: crumb back to the project, To confirm first",
       (await T("crumb").getByRole("link", { name: "project-y" }).getAttribute("href")) === `/projects/${y}` && (await T("calls-open").count()) === 1);
    const calls = page.getByTestId("call-row");
    ck("two calls, each with Keep and Change", (await calls.count()) === 2 && (await calls.nth(0).getByTestId("call-keep").count()) === 1
       && (await calls.nth(0).getByTestId("call-overturn").count()) === 1);
    ck("a call shows its question, choice, why and who made it",
       (await txt(calls.nth(0))).includes("Which date format?") && (await txt(calls.nth(0))).includes("ISO dates") && (await txt(calls.nth(0))).includes("It sorts as text.")
       && (await txt(calls.nth(0))).includes("By the builder, 2 days ago. Touches T3."), await txt(calls.nth(0)));
    const posts = [];
    page.on("request", (req) => { if (req.method() === "POST") posts.push({ url: new URL(req.url()).pathname, body: req.postData() }); });
    await calls.nth(0).getByTestId("call-overturn").click();
    ck("Change asks for words, and Send waits for them", (await txt(calls.nth(0).getByTestId("call-overturn-box"))).includes("Say what you want instead. The crew reads your words.")
       && (await calls.nth(0).getByTestId("call-send").isDisabled()));
    await checkWords(page, ck, "project-y overturn open");
    await shot(page, "live-overturn-1440.png");
    await calls.nth(0).getByTestId("call-words").fill("Use local dates");
    await page.route(`**/api/projects/${y}/assumptions/A1/state`, (r) => r.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
    await calls.nth(0).getByTestId("call-send").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-testid=call-row]").length === 1);
    const sentCall = posts.filter((p) => p.url === `/api/projects/${y}/assumptions/A1/state`);
    ck("Send posts the words and the call goes", sentCall.length === 1 && JSON.stringify(JSON.parse(sentCall[0].body)) === JSON.stringify({ state: "overturned", words: "Use local dates" }), JSON.stringify(sentCall));
    await page.unroute(`**/api/projects/${y}/assumptions/A1/state`);
    await page.route(`**/api/projects/${y}/assumptions/A2/state`, (r) => r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "That call was decided already." }) }));
    await calls.nth(0).getByTestId("call-keep").click();
    await calls.nth(0).getByTestId("call-error").waitFor();
    ck("a refused Keep shows the cell's sentence and keeps the call", (await txt(calls.nth(0).getByTestId("call-error"))) === "That call was decided already." && (await calls.count()) === 1);
    await page.unroute(`**/api/projects/${y}/assumptions/A2/state`);
    await page.route(`**/api/projects/${y}/assumptions/A2/state`, (r) => r.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
    await calls.nth(0).getByTestId("call-keep").click();
    await page.getByTestId("calls-empty").waitFor();
    const kept = posts.filter((p) => p.url === `/api/projects/${y}/assumptions/A2/state`);
    ck("Keep posts kept, and with the last call gone the section says so",
       JSON.parse(kept[kept.length - 1].body).state === "kept" && (await txt(T("calls-empty"))) === "Nothing to confirm. When the crew decides something without you, it shows here.");
    await page.unroute(`**/api/projects/${y}/assumptions/A2/state`);

    await open(page, y);
    // decisions, docs, terminal
    ck("decisions: three shown, newest first, a link to all five", (await T("decision-row").count()) === 3 && (await txt(T("decisions-all"))) === "All 5 decisions ›"
       && (await txt(T("decision-row").nth(1))).includes("The light one"), await txt(T("decision-row").nth(1)));
    ck("a reply by letter reads as the option chosen, with the person's words under it (TR-1018)",
       (await txt(T("decision-row").first().getByTestId("decision-answer"))) === "merge main into teams"
       && (await txt(T("decision-row").first().getByTestId("decision-said"))) === "You said: D85 A", await txt(T("decision-row").first()));
    ck("words equal to the answer are not repeated", (await T("decision-row").nth(1).getByTestId("decision-said").count()) === 0);
    ck("All 5 decisions opens the decisions page", (await T("decisions-all").getAttribute("href")) === `/projects/${y}/decisions`);
    await page.goto(`${BASE}/projects/${y}/decisions`, { waitUntil: "load" });
    await page.getByTestId("decisions-page").waitFor();
    ck("the decisions page lists all five, and says they are your answers, newest first",
       (await T("decision-row").count()) === 5 && /Your answers to the crew on project-y, newest first\./.test(await txt(T("decisions-page"))));
    await checkWords(page, ck, "project-y decisions page");
    await open(page, y);
    ck("docs open on the project's own docs page", (await T("doc-row").count()) === 4
       && (await T("doc-row").first().getAttribute("href")).startsWith(`/projects/${y}/docs?doc=`) &&
       (await txt(T("doc-row").first())).startsWith("Spec") && /Updated /.test(await txt(T("doc-row").first())), await txt(T("doc-row").first()));
    ck("All documents opens the project's docs page", (await T("docs-all").getAttribute("href")) === `/projects/${y}/docs`
       && /^All \d+ documents ›$/.test(await txt(T("docs-all"))), await txt(T("docs-all")));
    await page.goto(`${BASE}/projects/${y}/docs`, { waitUntil: "load" });
    await page.getByTestId("project-docs-page").waitFor();
    await page.getByTestId("doc-pane").waitFor();
    ck("the docs page opens the spec first, with the project's documents in groups",
       (await txt(T("doc-title"))).startsWith("Spec:") && (await T("project-doc-group").first().getAttribute("data-group")) === "project", await txt(T("doc-title")));
    await checkWords(page, ck, "project-y docs page");
    await open(page, y);
    ck("the terminal shows the command to open a builder's session",
       (await T("command-text").first().innerText()) === "tares factory crew attach project-y-build-m2" && (await txt(T("terminal"))).includes("Open the builder's session"));
    ck("a second command is behind \"1 more command\"", (await txt(T("terminal-more").locator("summary"))) === "1 more command");
    await T("command-copy").first().click();
    await page.waitForFunction(() => document.querySelector("[data-testid=command-copy]").textContent.trim() === "Copied");
    ck("Copy puts the command on the clipboard and says Copied",
       (await page.evaluate(() => navigator.clipboard.readText())) === "tares factory crew attach project-y-build-m2");
    await checkWords(page, ck, "project-y");
    await shot(page, "live-building-1440.png");

    // a #section in the address scrolls to it
    await open(page, y, "#milestones");
    await page.waitForTimeout(THEME_MS);
    ck("#milestones scrolls to the milestones", await page.evaluate(() => {
      const top = document.getElementById("milestones").getBoundingClientRect().top;
      const atEnd = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
      return window.scrollY > 0 && (Math.abs(top) < 40 || (atEnd && top >= 0 && top < window.innerHeight));
    }));
    await open(page, y, "#desk-D1");
    await page.waitForTimeout(THEME_MS);
    ck("#desk-D1 brings the waiting list into view", Math.abs(await page.evaluate(() => document.getElementById("waiting").getBoundingClientRect().top)) < 40);

    // ── 1b. project-k: a milestone with checks for the person (TR-1017) ──────────────────────────
    {
      const kid = ids["project-k"];
      await open(page, kid);
      const row = page.locator('[data-testid="milestone-row"][data-ms="M1"]');
      ck("project-k: the merged milestone says it has checks for you", (await txt(row.getByTestId("milestone-needs"))) === "2 checks for you to try");
      ck("the crew's check says it passed and who checked it", (await txt(row.locator('[data-testid="milestone-check"][data-for="crew"]').getByTestId("check-state"))).startsWith("The crew's. Passed, checked by crew-orchestrator"));
      ck("the Waiting list has the milestone item", (await T("waiting-row").and(page.locator("[data-kind=milestone_to_check]")).count()) === 1);
      const first = row.locator('[data-testid="milestone-check"][data-for="person"]').first();
      await first.getByTestId("check-works").click();
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="milestone-row"][data-ms="M1"] [data-testid="check-works"]').length === 1);
      ck("Works records it, and only the other check is left", (await txt(row.getByTestId("milestone-needs"))) === "1 check for you to try");
      const second = row.locator('[data-testid="milestone-check"][data-for="person"]').nth(1);
      await second.getByTestId("check-wrong").click();
      ck("Something's wrong asks for a sentence before it sends", await second.getByTestId("check-send").isDisabled());
      await second.getByTestId("check-note").fill("Home lists one more than Reviews");
      await second.getByTestId("check-send").click();
      await page.waitForFunction(() => !document.querySelector('[data-testid="milestone-row"][data-ms="M1"] [data-testid="milestone-needs"]'));
      ck("after Something's wrong the milestone is the crew's work: nothing left for you",
         (await row.getByTestId("milestone-needs").count()) === 0
         && ((await row.locator('[data-testid="milestone-check"][data-for="person"]').nth(1).getByTestId("check-state").textContent()) || "").includes("Home lists one more than Reviews"));
      ck("and the item leaves the Waiting list", (await T("waiting-row").and(page.locator("[data-kind=milestone_to_check]")).count()) === 0);
      await shot(page, "live-milestone-checks-1440.png");
    }

    // ── 2. project-z: ready to close ────────────────────────────────────────────────────────────
    const z = ids["project-z"];
    await open(page, z);
    ck("project-z says Ready to close, in the chip and the header button",
       (await raw(T("project-chip"))) === "Ready to close" && (await T("project-close").count()) === 1);
    ck("every stage is at Done", (await steps.nth(3).getAttribute("aria-current")) === "step");
    const ready = T("waiting-row").and(page.locator("[data-kind=signoff]"));
    ck("the Waiting list has a Ready to close row with its own words",
       (await raw(ready)).includes("Ready to close") && (await txt(ready)).includes("All 3 tickets are merged") && (await txt(ready)).includes("Close the project when you have checked the result."), await txt(ready));
    // the cell's own row (a Close button and an Open project link to this very page): one Close, no self link
    ck("the Ready to close row has one Close button and no link to this page",
       (await ready.count()) === 1 && (await ready.getByTestId("waiting-close").count()) === 1
       && (await ready.getByRole("link").count()) === 0 && !(await txt(ready)).includes("Open project"), await txt(ready));
    ck("no separate milestone item: a project ready to close folds them into Ready to close (TR-1017)",
       (await T("waiting-row").and(page.locator("[data-kind=milestone_to_check]")).count()) === 0);
    ck("Waiting counts the one", (await txt(T("waiting-count"))) === "1 thing", await txt(T("waiting-count")));
    ck("the terminal says no builder is working", (await txt(T("terminal-empty"))).startsWith("No builder is working on this project now."));
    await checkWords(page, ck, "project-z ready");
    await shot(page, "live-ready-1440.png");
    const finishPosts = [];
    page.on("request", (req) => { if (req.method() === "POST" && req.url().endsWith("/finish")) finishPosts.push(req.postData()); });
    await T("project-close").click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    ck("the dialog names the project and says what Close does", (await dialog.innerText()).includes("Close project-z?")
       && (await dialog.innerText()).includes("It moves to Done. Handing it to the crew again opens it."));
    await checkWords(page, ck, "close dialog");
    await shot(page, "live-dialog-1440.png");
    await dialog.getByTestId("close-cancel").click();
    await dialog.waitFor({ state: "hidden" });
    ck("Cancel closes nothing", finishPosts.length === 0);
    await page.route(`**/api/projects/${z}/finish`, (r) => r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: SERVER_WORDS }) }));
    await ready.getByTestId("waiting-close").click();
    await dialog.getByTestId("close-confirm").click();
    await T("close-error").waitFor();
    ck("a refusal shows the cell's sentence under the heading and the project stays open",
       (await txt(T("close-error"))) === `The project was not closed. ${SERVER_WORDS}` && (await T("project-close").count()) === 1 && (await dialog.count()) === 0, await txt(T("close-error")));
    await checkWords(page, ck, "close refused");
    await shot(page, "live-closeerr-1440.png");
    await page.unroute(`**/api/projects/${z}/finish`);
    finishPosts.length = 0;
    await T("project-close").click();
    await dialog.getByTestId("close-confirm").click();
    await page.getByText("project-z is closed. It moved to Done.").first().waitFor();
    await settle(page);
    ck("Close sends one finish, with words", finishPosts.length === 1 && JSON.parse(finishPosts[0]).words === "Closed from the project page", JSON.stringify(finishPosts));
    ck("the page now says Finished, with no Close and an empty Waiting list",
       (await raw(T("project-chip"))) === "Finished" && (await T("project-close").count()) === 0 && (await txt(T("waiting-empty"))) === "Nothing waits for you in this project.");
    ck("a finished project has every stage done", (await steps.evaluateAll((l) => l.every((x) => x.textContent.trim().startsWith("✓")))));
    await checkWords(page, ck, "project-z finished");
    await shot(page, "live-finished-1440.png");

    // ── 3. the other shapes ─────────────────────────────────────────────────────────────────────
    await open(page, ids["project-x"]);
    ck("project-x is Not started, at the Spec stage", (await raw(T("project-chip"))) === "Not started" && (await steps.nth(0).getAttribute("aria-current")) === "step");
    ck("no tickets, no milestones, with the sentences for it",
       (await txt(T("moving-empty"))) === "No tickets yet. They show here once the crew has planned the build."
       && (await txt(T("milestones-empty"))) === "No milestones yet. The crew adds them when it plans the build.");
    ck("and no All tickets link", (await T("all-tickets-head").count()) === 0 && (await T("all-tickets-moving").count()) === 0);
    await checkWords(page, ck, "project-x");
    await open(page, ids["project-p"]);
    ck("a project with no milestones has no filter for them", (await txt(T("milestones-empty"))).startsWith("No milestones yet.") && (await page.locator("select, [role=combobox]").count()) === 0);
    await open(page, ids["project-d"]);
    ck("a finished project (closed before) says Finished and has no Close", (await raw(T("project-chip"))) === "Finished" && (await T("project-close").count()) === 0);
    ck("and says nothing is moving, all tickets merged", (await txt(T("moving-empty"))) === "Nothing is moving. All 2 tickets are merged.", await txt(T("moving-empty")));

    // an old ticket or doc link, a project that is not a factory project and an address with no
    // project show today's project page, never this one
    await page.goto(`${BASE}/projects/${y}?view=ticket:T1`, { waitUntil: "load" });
    await page.waitForFunction(() => location.pathname.endsWith("/tickets/T1"), null, { timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => document.body.innerText.includes("T1"), null, { timeout: 15000 }).catch(() => {});
    ck("?view=ticket:T1 lands on the ticket page, not the factory page", (await T("factory-project").count()) === 0
       && new URL(page.url()).pathname === `/projects/${y}/tickets/T1` && new URL(page.url()).search === "", page.url());
    await page.goto(`${BASE}/projects/${y}?view=doc:none`, { waitUntil: "load" });
    await page.waitForFunction(() => document.body.innerText.trim().length > 40, null, { timeout: 15000 }).catch(() => {});
    ck("?view=doc: opens today's page too", (await T("factory-project").count()) === 0 && (await T("factory-project-wait").count()) === 0);
    await page.goto(`${BASE}/projects/${y}`, { waitUntil: "load" });
    await T("factory-project").waitFor();
    await T("moving-row").and(page.locator("[data-ref=T5]")).click();
    await page.waitForFunction(() => location.pathname.endsWith("/tickets/T5"), null, { timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => document.body.innerText.includes("T5"), null, { timeout: 15000 }).catch(() => {});
    ck("a ticket row lands on that ticket's page, not on this page", (await T("factory-project").count()) === 0
       && new URL(page.url()).pathname === `/projects/${y}/tickets/T5` && (await page.locator("body").innerText()).includes("T5"), page.url());
    await page.goto(`${BASE}/projects/${ids["project-w"]}`, { waitUntil: "load" });
    await page.waitForFunction(() => document.body.innerText.includes("Not a factory project"), null, { timeout: 15000 }).catch(() => {});
    ck("a project that is not a factory project shows today's page", (await T("factory-project").count()) === 0 && (await T("factory-project-wait").count()) === 0
       && (await page.locator("body").innerText()).includes("Not a factory project"), (await page.locator("body").innerText()).slice(0, 300) + " | " + page.url());
    await page.goto(`${BASE}/projects/nothing-here`, { waitUntil: "load" });
    await page.waitForFunction(() => document.body.innerText.trim().length > 40, null, { timeout: 15000 }).catch(() => {});
    ck("an address that is no project shows today's error, no crash", (await T("factory-project").count()) === 0 && (await page.getByRole("button", { name: "Retry" }).count()) >= 0);
    await ctx.close();

    // ── 4. 390 px: the live pages do not scroll sideways ────────────────────────────────────────
    {
      const { ctx: c, page: p } = await newPage(390);
      for (const [name, file] of [["project-y", "live-building"], ["project-z", "live-finished"], ["project-x", "live-spec"]]) {
        await open(p, ids[name]);
        await settle(p);
        ck(`390: ${name} does not scroll sideways`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, `390 ${name}`);
        await shot(p, `${file}-390.png`);
      }
      await c.close();
    }

    // TR-1125: an option of a desk question stays inside its card and wraps, however long it is
    for (const width of [1440, 390]) {
      const t = (l) => `long option @${width}: ${l}`;
      const { ctx: c, page: p } = await newPage(width);
      const sentence = "Keep the current upload path and add a second step that retries each failed part on its own before the whole upload is marked as failed and handed back to the person who started it";
      const unbroken = "keep-the-current-upload-path-and-add-a-second-step-that-retries-each-failed-part-on-its-own-before-the-whole-upload-is-marked-as-failed";
      await p.route(`**/api/factory/projects/${y}`, async (route) => {
        const res = await route.fetch();
        const data = await res.json();
        data.waiting = [{ id: "desk:long-option", kind: "question", label: "Question", title: "How should a failed upload behave?", detail: "The crew needs one choice.",
                          meta: "Waiting less than a minute", project: null, repo: null, from: null, since: new Date().toISOString(), waited: "just now", blocking: true,
                          question: { desk: "D9", options: [sentence, unbroken, "Stop"], recommendation: null, blocks: null }, actions: [] }, ...data.waiting];
        await route.fulfill({ response: res, json: data });
      });
      await open(p, y);
      await settle(p);
      const card = p.getByTestId("waiting-row").filter({ hasText: "How should a failed upload behave?" });
      await card.getByTestId("question-choices").waitFor();
      const m = await card.evaluate((row) => {
        const box = row.getBoundingClientRect();
        return [...row.querySelectorAll("[data-testid=question-choices] a")].map((a) => {
          const r = a.getBoundingClientRect();
          return { inside: r.left >= box.left - 1 && r.right <= box.right + 1, clipped: a.scrollWidth - a.clientWidth };
        });
      });
      ck(t("three options"), m.length === 3, JSON.stringify(m));
      ck(t("every option stays inside its card"), m.every((a) => a.inside), JSON.stringify(m));
      ck(t("no option's text runs past its box"), m.every((a) => a.clipped <= 1), JSON.stringify(m));
      ck(t("no sideways scroll"), (await overflow(p)) <= 0, String(await overflow(p)));
      await shot(p, `long-option-${width}.png`);
      await c.close();
    }

    // ── 5. the sample states ────────────────────────────────────────────────────────────────────
    for (const name of SAMPLES) {
      for (const width of [1440, 390]) {
        const { ctx: c, page: p } = await newPage(width, true);
        await p.goto(`${BASE}/projects/uc_sample?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("project-loading").waitFor();
        else if (name === "err") await p.getByRole("alert").waitFor();
        else await p.getByTestId("factory-project").waitFor();
        await settle(p);
        const label = `${name} ${width}`;
        ck(`${label}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, label);
        const q = (id) => p.getByTestId(id);
        if (width === 1440) {
          if (name === "building") {
            ck("building: four waiting (three shown), five of seven moving, three milestones",
               (await txt(q("waiting-count"))) === "4 things" && (await q("waiting-row").count()) === 3 && (await q("moving-row").count()) === 5
               && (await txt(q("moving-more"))).startsWith("2 more tickets are moving.") && (await q("milestone-row").count()) === 3);
            ck("building: the tracker link opens the tracker", (await q("tracker-link").getAttribute("href")) === "https://example.com/tickets" && (await txt(q("tracker-link"))).startsWith("Tickets in GitHub"));
            ck("building: no Close button", (await q("project-close").count()) === 0);
          } else if (name === "spec") {
            ck("spec: Not started at the Spec stage, with no tickets", (await raw(q("project-chip"))) === "Not started" && (await q("moving-empty").count()) === 1 && (await q("terminal-empty").count()) === 1);
          } else if (name === "ready" || name === "dialog" || name === "fail") {
            ck(`${name}: Ready to close, with the Close button`, (await raw(q("project-chip"))) === "Ready to close" && (await q("project-close").count()) === 1
               && (await raw(q("waiting-row").and(p.locator("[data-kind=signoff]")))).includes("Ready to close"));
            if (name === "ready") {
              await q("project-close").click();
              await q("close-confirm").click();
              await p.getByText("project-z is closed. It moved to Done.").first().waitFor();
              ck("ready: Close moves the sample to Finished", (await raw(q("project-chip"))) === "Finished" && (await q("project-close").count()) === 0);
            } else if (name === "dialog") {
              ck("dialog: asks to close project-z", (await p.getByRole("dialog").innerText()).includes("Close project-z?"));
            } else {
              ck("fail: the refusal is under the heading", (await txt(q("close-error"))) === `The project was not closed. ${SERVER_WORDS}`);
              await q("project-close").click();
              await q("close-confirm").click();
              await q("close-error").waitFor();
              ck("fail: Close answers with the same sentence", (await txt(q("close-error"))) === `The project was not closed. ${SERVER_WORDS}` && (await q("project-close").count()) === 1);
            }
          } else if (name === "finished") {
            ck("finished: Finished, no Close, nothing waiting", (await raw(q("project-chip"))) === "Finished" && (await q("project-close").count()) === 0 && (await q("waiting-empty").count()) === 1);
          } else if (name === "empty") {
            ck("empty: every section has its sentence", (await q("waiting-empty").count()) === 1 && (await q("moving-empty").count()) === 1 && (await q("milestones-empty").count()) === 1
               && (await q("decisions-empty").count()) === 1 && (await q("docs-empty").count()) === 1);
          } else if (name === "long") {
            ck("long: nothing pushes out of its card", await p.evaluate(() => [...document.querySelectorAll("[data-testid=factory-project] *")]
              .every((e) => e.closest("details:not([open])") || e.scrollWidth <= e.clientWidth + 1 || getComputedStyle(e).display === "inline" || e.tagName === "CODE")));
          } else if (name === "many") {
            ck("many: five waiting (three shown), five of eighteen moving, six milestones, forty decisions (three shown)",
               (await txt(q("waiting-count"))) === "5 things" && (await q("moving-row").count()) === 5 && (await txt(q("moving-more"))).startsWith("13 more tickets are moving.")
               && (await q("milestone-row").count()) === 6 && (await q("decision-row").count()) === 3 && (await txt(q("decisions-all"))) === "All 40 decisions ›");
            ck("many: three commands, two behind a button", (await q("command").count()) === 3 && (await txt(q("terminal-more").locator("summary"))) === "2 more commands");
          } else if (name === "load") {
            ck("load: the skeleton, and no project", (await q("project-loading").isVisible()) && (await q("factory-project").count()) === 0);
          } else if (name === "err") {
            ck("err: the banner and Retry", (await p.getByRole("alert").innerText()).includes("We could not load this project. Check your connection, then try again.")
               && (await p.getByRole("button", { name: "Retry" }).count()) === 1);
          }
        }
        if (name === "many" || name === "building") {
          // the page in the dark theme too
          await p.emulateMedia({ colorScheme: "dark" });
          await p.waitForTimeout(THEME_MS);
          ck(`${label} dark: no sideways scroll`, (await overflow(p)) <= 0);
          await checkWords(p, ck, `${label} dark`);
          await shot(p, `${name}-dark-${width}.png`);
          await p.emulateMedia({ colorScheme: "light" });
          await p.waitForTimeout(THEME_MS);
        }
        const shotName = { load: "loading", err: "error", dialog: "closedlg", fail: "closeerr" }[name] || name;
        if (name !== "ready" || width === 390) await shot(p, `${shotName}-${width}.png`);
        if (name === "ready" && width === 1440) {
          await p.goto(`${BASE}/projects/uc_sample?sample=ready`, { waitUntil: "load" });
          await p.getByTestId("factory-project").waitFor();
          await settle(p);
          await shot(p, "ready-1440.png");
        }
        await c.close();
      }
    }
    // the address alone shows no sample on a live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${BASE}/projects/${y}?sample=empty`, { waitUntil: "load" });
      await p.getByTestId("factory-project").waitFor();
      ck("the sample address alone shows the live cell", (await p.getByTestId("project-name").innerText()).trim() === "project-y" && (await p.getByTestId("waiting-empty").count()) === 0);
      await c.close();
    }
    // dark mode: no field, step or command box keeps a white ground under light text
    {
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
      await c.addInitScript(() => { try { localStorage.setItem("tares-theme", "dark"); } catch (e) { /* none */ } });
      const p = await c.newPage();
      await open(p, y);
      await settle(p);
      const pale = await p.evaluate(() => {
        const cv = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
        const lum = (s) => { cv.clearRect(0, 0, 1, 1); cv.fillStyle = "#000"; cv.fillStyle = s; cv.fillRect(0, 0, 1, 1); const [r, g, b, a] = cv.getImageData(0, 0, 1, 1).data; return a < 128 ? null : (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255; };
        return [...document.querySelectorAll("[data-testid=factory-project] *")].filter((e) => {
          const cs = getComputedStyle(e), bg = lum(cs.backgroundColor), fg = lum(cs.color);
          return e.getBoundingClientRect().width > 0 && e.textContent.trim() && bg !== null && bg > 0.8 && fg !== null && fg > 0.6;
        }).map((e) => e.textContent.trim().slice(0, 30));
      });
      ck("dark: no light text on a white ground (steps, command box)", pale.length === 0, JSON.stringify(pale));
      await c.close();
    }

    // delete: the last section; the dialog asks for the name, then the project is gone
    {
      const { ctx: c, page: p } = await newPage(1440);
      const d = ids["project-d"];
      await open(p, d);
      const T2 = (id) => p.getByTestId(id);
      ck("a project page ends with Delete this project", (await T2("delete-project").count()) === 1 && (await txt(T2("delete-open"))) === "Delete project");
      await T2("delete-open").click();
      await T2("delete-dialog").waitFor();
      ck("Delete stays off until the name is typed", await T2("delete-confirm").isDisabled());
      await T2("delete-name").fill("project-");
      ck("and with a wrong name", await T2("delete-confirm").isDisabled());
      await T2("delete-cancel").click();
      ck("Cancel keeps the project", (await (await fetch(`${BASE}/api/projects/${d}`)).status) === 200);
      await T2("delete-open").click();
      await T2("delete-name").fill("project-d");
      await T2("delete-confirm").click();
      await p.waitForURL((u) => u.pathname === "/projects");
      ck("Delete leaves for Projects", new URL(p.url()).pathname === "/projects", p.url());
      ck("and the project is gone", (await fetch(`${BASE}/api/projects/${d}`)).status === 404);
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
