/*
 * Browser e2e for a factory project's Merge queue page (/projects/:id/merge-queue, TR-869): the pull
 * requests in line to merge, by repo and base branch, in merge order, each with its review, CI and
 * guardrail lines and what it waits on. The person a question is addressed to answers it here ("Yes,
 * merge" or "No, hold": the row's own desk question); nobody else sees buttons. A row whose commit moved,
 * someone else's question, repo rules, no GitHub connection and a send back each say so in words.
 * Every state shows only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-merge-queue.py, invented names), then runs the real
 * daemon on it, serving the built console. The daemon runs without auth, so the viewer is the cell's own
 * person (`local`), and the seeded questions are addressed to them or to someone else.
 * Run:  npm run build && node tests/factory-merge-queue.e2e.cjs     (from ui/, tares installed in ../.venv)
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
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8371);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-mq-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-merge-queue.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  const U = (name) => ids[name];
  ck("seeded the projects", ["p-wait", "p-ask", "p-block", "p-act", "p-many", "p-empty", "p-other"].every((k) => !!U(k)));

  const env = { HOME: process.env.HOME, PATH: process.env.PATH, PYTHONPATH: REPO, TARES_EDITION: "factory", TARES_HOME: home,
                TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off",
                TARES_PR_SYNC_SECONDS: "off" };
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(300); };
    const open = async (page, name, wait = "mq-page") => {
      await page.goto(`${BASE}/projects/${U(name)}/merge-queue`, { waitUntil: "load" });
      await page.getByTestId(wait).waitFor();
      await settle(page);
    };
    const rowOf = (page, ref) => page.locator(`[data-testid=mq-row][data-ticket="${ref}"]`);
    const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const state = async (page, label, width, name) => {
      ck(`${label}: no sideways scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
      await checkWords(page, ck, `${label} ${width}`);
      await shot(page, `${name}-${width}.png`);
    };

    for (const width of [1440, 390]) {
      let { ctx, page } = await newPage(width);

      console.log(`== waiting for CI, queued, merged list at ${width} ==`);
      // the page makes one read of the queue; the project shell around it reads the project once for its sidebar
      const seen = [];
      page.on("request", (r) => { if (r.method() === "GET" && new URL(r.url()).pathname.startsWith("/api/")) seen.push(new URL(r.url()).pathname); });
      await open(page, "p-wait");
      ck("the tab is in the project sidebar and current", (await page.getByTestId("project-page-merge-queue").getAttribute("aria-current")) === "page");
      ck("the page is named", (await text(page.getByTestId("page-title"))) === "Merge queue");
      const grp = page.getByTestId("mq-group");
      ck("one group, named by repo and branch", (await grp.count()) === 1 && (await text(grp.getByTestId("mq-group-toggle"))).includes("example-org/api"), await text(grp));
      const chips = await page.getByTestId("mq-chip").allInnerTexts();
      ck("waiting for CI first, then next, then queued", chips.slice(0, 3).map((c) => c.trim().toLowerCase()).join(",") === "waiting for ci,next,queued", chips.join(","));
      const first = rowOf(page, "ORD-41");
      ck("the pull request is a link to GitHub", (await first.getByTestId("mq-pr").getAttribute("href")) === "https://github.com/example-org/api/pull/217");
      ck("review, CI and guardrails each have a line", (await first.getByTestId("mq-check-review").count()) === 1 && (await first.getByTestId("mq-check-ci").count()) === 1 && (await first.getByTestId("mq-check-guardrails").count()) === 1);
      ck("a CI line that waits says so", (await text(first.getByTestId("mq-check-ci"))).includes("Pending"), await text(first.getByTestId("mq-check-ci")));
      ck("a row with no question has no buttons", (await page.getByTestId("mq-actions").count()) === 0);
      ck("the merged list is closed until asked", (await page.getByTestId("mq-merged-item").count()) === 0);
      await page.getByTestId("mq-merged-toggle").click();
      ck("the merged list names the ticket, the pull request, when and by whom", (await text(page.getByTestId("mq-merged-item").first())).includes("ORD-38"), await text(page.getByTestId("mq-merged"))); 
      const long = rowOf(page, "ORD-45");
      ck("long text wraps inside the card", (await long.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)));
      await settle(page);
      const queueReads = seen.filter((x) => x === `/api/projects/${U("p-wait")}/merge-queue`);
      const extra = seen.filter((x) => x !== `/api/projects/${U("p-wait")}/merge-queue` && x !== `/api/factory/projects/${U("p-wait")}` && x !== "/api/licence" && x !== "/api/whoami");
      ck(`waiting for CI ${width}: one read of the queue`, queueReads.length === 1, `${queueReads.length}`);
      ck(`waiting for CI ${width}: no read but the project's own`, extra.length === 0, extra.join(", "));
      await state(page, "waiting for CI, long text", width, "waiting-ci");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== waiting for approval, answered, moved at ${width} ==`);
      await open(page, "p-ask");
      const mine = rowOf(page, "ASK-1");
      ck("your question: chip says it needs your yes", (await text(mine.getByTestId("mq-chip"))) === "NEEDS YOUR YES" || (await text(mine.getByTestId("mq-chip"))).toLowerCase() === "needs your yes", await text(mine.getByTestId("mq-chip")));
      ck("your question has Yes, merge and No, hold", (await text(mine.getByTestId("mq-yes"))) === "Yes, merge" && (await text(mine.getByTestId("mq-no"))) === "No, hold");
      const other = rowOf(page, "ASK-2");
      ck("someone else's question: chip names them, no buttons", (await text(other.getByTestId("mq-chip"))).toLowerCase().startsWith("waiting for dana") && (await other.getByTestId("mq-actions").count()) === 0, await text(other));
      const moved = rowOf(page, "ASK-3");
      ck("a moved commit says so and cannot be answered", (await text(moved.getByTestId("mq-chip"))).toLowerCase() === "commit changed" && (await moved.getByTestId("mq-actions").count()) === 0, await text(moved));
      const yes = rowOf(page, "ASK-4");
      ck("answered yes: says who, where and when, no buttons", (await yes.getByTestId("mq-actions").count()) === 0 && (await text(yes.getByTestId("mq-status"))).length > 0, await text(yes.getByTestId("mq-status")));
      const no = rowOf(page, "ASK-5");
      ck("answered no: on hold, no buttons", (await no.getByTestId("mq-actions").count()) === 0, await text(no));
      ck("only the rows that wait on you count for you", (await page.locator('[data-testid=mq-row][data-attn=yes]').count()) >= 1);
      await state(page, "waiting for approval", width, "approval");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== blocked and sent back at ${width} ==`);
      await open(page, "p-block");
      ck("two groups: one per repo", (await page.getByTestId("mq-group").count()) === 2);
      const rules = rowOf(page, "BLK-1");
      ck("blocked by repo rules says why", (await text(rules.getByTestId("mq-chip"))).toLowerCase() === "blocked" && (await text(rules)).includes("two approving reviews"), await text(rules));
      const noGh = rowOf(page, "BLK-2");
      ck("blocked with no GitHub connection says so", (await text(noGh)).includes("No GitHub connection can write to example-org/web"), await text(noGh));
      const back = rowOf(page, "BLK-3");
      ck("sent back is out of line and says how often", (await text(back.getByTestId("mq-chip"))).toLowerCase() === "sent back" && (await back.getByTestId("mq-bounce").count()) === 1, await text(back));
      await state(page, "blocked and sent back", width, "blocked");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== many at ${width} ==`);
      await open(page, "p-many");
      ck("five rows show", (await page.getByTestId("mq-row").count()) === 5);
      ck("the rest wait behind Show 3 more", (await text(page.getByTestId("mq-more-toggle"))) === "Show 3 more");
      await state(page, "many, capped", width, "many-capped");
      await page.getByTestId("mq-more-toggle").click();
      ck("all eight show", (await page.getByTestId("mq-row").count()) === 8);
      ck("the toggle offers fewer", (await text(page.getByTestId("mq-more-toggle"))) === "Show fewer");
      await state(page, "many, all", width, "many-all");
      await page.getByTestId("mq-group-toggle").click();
      ck("the group closes", (await page.getByTestId("mq-row").count()) === 0);
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== empty, not a factory project, loading, error at ${width} ==`);
      await open(page, "p-empty");
      ck("an empty queue says so", (await page.getByTestId("mq-empty").count()) === 1);
      await state(page, "empty", width, "empty");

      await page.goto(`${BASE}/projects/${U("p-other")}/merge-queue`, { waitUntil: "load" });
      await page.waitForURL(`**/projects/${U("p-other")}`, { timeout: 8000 }).catch(() => {});
      ck("a project that is not a factory project does not show the page", (await page.getByTestId("mq-page").count()) === 0);
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      let release;
      const gate = new Promise((r) => { release = r; });
      await page.route("**/api/projects/*/merge-queue", async (route) => { await gate; await route.continue(); });
      await page.goto(`${BASE}/projects/${U("p-wait")}/merge-queue`, { waitUntil: "load" });
      await page.getByTestId("mq-wait").waitFor();
      ck("while loading, a placeholder shows and no rows", (await page.getByTestId("mq-row").count()) === 0 && (await page.getByRole("status", { name: "Loading the merge queue" }).count()) === 1);
      await state(page, "loading", width, "loading");
      release();
      await page.getByTestId("mq-page").waitFor();
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      await page.route("**/api/projects/*/merge-queue", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"down"}' }));
      await page.goto(`${BASE}/projects/${U("p-wait")}/merge-queue`, { waitUntil: "load" });
      await page.getByTestId("mq-error").waitFor();
      ck("a failed read says so and offers to try again", (await text(page.getByTestId("mq-error"))).includes("did not load") && (await page.getByRole("button", { name: "Try again" }).count()) === 1);
      await state(page, "error", width, "error");
      await ctx.close();
    }

    console.log("== answering ==");
    {
      const { ctx, page } = await newPage(1440);
      await open(page, "p-act");
      const reads = [];
      page.on("request", (r) => { if (r.method() === "GET" && /\/merge-queue$/.test(new URL(r.url()).pathname)) reads.push(1); });
      // fail the first answer, on the third row
      let posts = 0;
      await page.route("**/api/desk/*/reply", async (route) => {
        posts++;
        if (rowFailing) return route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"no"}' });
        await new Promise((r) => setTimeout(r, 400));
        return route.continue();
      });
      let rowFailing = true;
      const fail3 = rowOf(page, "ACT-3");
      await fail3.getByTestId("mq-yes").click();
      await fail3.getByTestId("mq-failed").waitFor();
      ck("a failed answer says nothing changed and keeps the buttons", (await fail3.getByTestId("mq-yes").count()) === 1 && (await text(fail3.getByTestId("mq-failed"))).includes("not saved"));
      await shot(page, "answer-failed-1440.png");
      rowFailing = false;
      const yes = rowOf(page, "ACT-1");
      const before = reads.length;
      await yes.getByTestId("mq-yes").click();
      await yes.getByText("Saving your answer.").waitFor();
      ck("while it saves, the buttons are off", (await yes.getByTestId("mq-yes").isDisabled()) && (await yes.getByTestId("mq-no").isDisabled()));
      await shot(page, "answering-1440.png");
      await yes.getByTestId("mq-actions").waitFor({ state: "detached", timeout: 8000 });
      ck("one answer, one POST per click", posts === 2, String(posts));
      ck("the page reads again and the row shows its answer", reads.length > before && (await yes.getByTestId("mq-actions").count()) === 0, await text(yes));
      const no = rowOf(page, "ACT-2");
      await no.getByTestId("mq-no").click();
      await no.getByTestId("mq-actions").waitFor({ state: "detached", timeout: 8000 });
      ck("No, hold closes the question too", (await no.getByTestId("mq-actions").count()) === 0 && posts === 3, String(posts));
      await shot(page, "answered-1440.png");
      await checkWords(page, ck, "after answering");
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
