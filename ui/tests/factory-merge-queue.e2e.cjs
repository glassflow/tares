/*
 * Browser e2e for the Merge queue tab on a factory project's Pull requests page
 * (/projects/:id/github?tab=queue, TR-869): the pull requests in line to merge, by repo and base branch,
 * in merge order, each with its review, CI and guardrail lines and what it waits on. The page has two
 * tabs, Merge queue and Pull requests; the tab is in the address, so a reload keeps it. The person a
 * question is addressed to answers it here ("Yes, merge" or "No, hold": the row's own desk question, sent
 * to /api/desk/<ref>/reply as A or B); nobody else sees buttons. A question that was withdrawn is approved
 * through the approve route. A row whose commit moved, someone else's question, repo rules, no GitHub
 * connection and a send back each say so in words, and the Calls page is one link away. Every state shows
 * only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-merge-queue.py, invented names), then runs the real
 * daemon on it, serving the built console. The daemon runs without auth, so the viewer is the cell's own
 * person (`local`), and the seeded questions are addressed to them or to someone else.
 * Run:  npm run build && node tests/factory-merge-queue.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px (it is not cleared here).
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
  ck("seeded the projects", ["p-wait", "p-ask", "p-block", "p-wd", "p-wd2", "p-act", "p-act2", "p-many", "p-cap", "p-empty", "p-other"].every((k) => !!U(k)));

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
    const rowShot = async (loc, name) => { if (SHOTS) await loc.screenshot({ path: path.join(SHOTS, name) }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(300); };
    const url = (name, tab = "queue") => `${BASE}/projects/${U(name)}/github${tab ? `?tab=${tab}` : ""}`;
    const open = async (page, name, wait = "mq-page") => {
      await page.goto(url(name), { waitUntil: "load" });
      await page.getByTestId(wait).waitFor();
      await settle(page);
    };
    const rowOf = (page, ref) => page.locator(`[data-testid=mq-row][data-ticket="${ref}"]`);
    const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const css = (loc, props) => loc.evaluate((el, ps) => {
      const s = getComputedStyle(el);
      return Object.fromEntries(ps.map((p) => [p, s[p]]));
    }, props);
    // the colour a token paints, as the browser computes it, to compare with what an element got
    const token = (page, name) => page.evaluate((n) => {
      const probe = document.createElement("span");
      probe.style.color = `var(${n})`;
      document.body.appendChild(probe);
      const c = getComputedStyle(probe).color;
      probe.remove();
      return c;
    }, name);
    // checkWords and the no-scroll rule for one state, then its shot
    const state = async (page, label, width, name) => {
      ck(`${label} ${width}: no sideways scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
      await checkWords(page, ck, `${label} ${width}`);
      await shot(page, `${name}-${width}.png`);
    };
    const mergeQueueRoute = `/api/projects/${U("p-wait")}/merge-queue`;

    for (const width of [1440, 390]) {
      const narrow = width < 600;
      let { ctx, page } = await newPage(width);

      console.log(`== tabs, waiting for CI, queued, merged list at ${width} ==`);
      // The Pull requests page makes three reads on its own: the project (the sidebar), the pull requests
      // and the merge queue, so expectOneCall (one route only) does not fit; this counts each of the three.
      const seen = [];
      page.on("request", (r) => { if (r.method() === "GET" && new URL(r.url()).pathname.startsWith("/api/")) seen.push(new URL(r.url()).pathname); });
      await open(page, "p-wait");
      const pwait = U("p-wait");
      const reads = (p) => seen.filter((x) => x === p).length;
      ck(`${width}: one read of the queue`, reads(mergeQueueRoute) === 1, String(reads(mergeQueueRoute)));
      ck(`${width}: one read of the pull requests`, reads(`/api/factory/projects/${pwait}/github`) === 1, String(reads(`/api/factory/projects/${pwait}/github`)));
      ck(`${width}: one read of the project`, reads(`/api/factory/projects/${pwait}`) === 1, String(reads(`/api/factory/projects/${pwait}`)));
      const extra = seen.filter((x) => ![mergeQueueRoute, `/api/factory/projects/${pwait}/github`, `/api/factory/projects/${pwait}`, "/api/licence", "/api/whoami"].includes(x));
      ck(`${width}: no other read`, extra.length === 0, extra.join(", "));

      const pages = await page.getByTestId("project-pages").locator("a").allInnerTexts();
      ck(`${width}: the sidebar has no Merge queue entry`, (await page.getByTestId("project-page-merge-queue").count()) === 0
         && !pages.some((t) => /merge queue/i.test(t)), pages.join(" | "));
      ck(`${width}: the Pull requests entry is the current page`, (await page.getByTestId("project-page-prs").getAttribute("aria-current")) === "page");
      ck(`${width}: the page is Pull requests`, (await text(page.getByTestId("page-title"))) === "Pull requests");
      const tabs = page.getByRole("tablist", { name: "Pull requests page" });
      ck(`${width}: a strip of two tabs sits in the page`, (await tabs.getByRole("tab").count()) === 2);
      ck(`${width}: the Merge queue tab is first and selected`, (await tabs.getByRole("tab").first().getAttribute("aria-selected")) === "true"
         && (await text(tabs.getByRole("tab").first())).startsWith("Merge queue"), await text(tabs));
      ck(`${width}: the tab says how many are in line`, (await text(page.getByTestId("tab-queue-n"))) === "4", await text(tabs));
      ck(`${width}: nothing needs you, so the tab has no need chip`, (await page.getByTestId("tab-queue-need").count()) === 0);
      ck(`${width}: the Pull requests tab is not selected`, (await page.getByTestId("tab-prs").getAttribute("aria-selected")) === "false");
      const h = await css(page.getByTestId("mq-heading"), ["fontSize", "fontWeight"]);
      ck(`${width}: the heading is ${narrow ? 20 : 22}px bold`, h.fontSize === (narrow ? "20px" : "22px") && Number(h.fontWeight) >= 700, JSON.stringify(h));
      ck(`${width}: the intro is 13px`, (await css(page.getByTestId("mq-intro"), ["fontSize"])).fontSize === "13px");
      const grp = page.getByTestId("mq-group");
      ck(`${width}: one group, named by repo and branch`, (await grp.count()) === 1 && (await text(grp.getByTestId("mq-group-toggle"))).includes("example-org/api into main"), await text(grp));
      const gh = await css(grp.getByTestId("mq-group-toggle"), ["fontFamily", "fontWeight"]);
      ck(`${width}: the group name is bold mono`, /mono/i.test(gh.fontFamily) && Number(gh.fontWeight) >= 600, JSON.stringify(gh));
      const into = await css(grp.getByTestId("mq-group-toggle").locator("span span").first(), ["fontFamily", "fontWeight"]);
      ck(`${width}: the word into is plain`, !/mono/i.test(into.fontFamily) && Number(into.fontWeight) <= 400, JSON.stringify(into));
      ck(`${width}: the group header has a chevron`, (await grp.getByTestId("mq-group-toggle").locator("svg").count()) === 1);
      const flat = async (loc) => { const c = await css(loc, ["borderTopWidth", "backgroundColor", "paddingLeft", "display"]); return c.borderTopWidth === "0px" && /^rgba\(0, 0, 0, 0\)$|^transparent$/.test(c.backgroundColor) && c.display === "flex"; };
      ck(`${width}: the group toggle is a flat row, not a boxed button`, await flat(grp.getByTestId("mq-group-toggle")), JSON.stringify(await css(grp.getByTestId("mq-group-toggle"), ["borderTopWidth", "backgroundColor", "paddingLeft", "display"])));
      ck(`${width}: the chevron sits on the same line as the repo`, await grp.getByTestId("mq-group-toggle").evaluate((el) => { const s = el.querySelector("svg").getBoundingClientRect(); const t = el.querySelector("span").getBoundingClientRect(); return Math.abs((s.top + s.bottom) / 2 - (t.top + t.bottom) / 2) < t.height; }));
      const chips = await page.getByTestId("mq-chip").allInnerTexts();
      ck(`${width}: waiting for CI first, then next, then queued`, chips.slice(0, 3).map((c) => c.trim().toLowerCase()).join(",") === "waiting for ci,next,queued", chips.join(","));
      const first = rowOf(page, "ORD-41");
      ck(`${width}: the pull request is a link to GitHub`, (await first.getByTestId("mq-pr").getAttribute("href")) === "https://github.com/example-org/api/pull/217");
      ck(`${width}: review, CI and guardrails each have a line`, (await first.getByTestId("mq-check-review").count()) === 1 && (await first.getByTestId("mq-check-ci").count()) === 1 && (await first.getByTestId("mq-check-guardrails").count()) === 1);
      const lab = await css(first.getByTestId("mq-check-review").locator("dt"), ["fontFamily", "textTransform"]);
      ck(`${width}: check labels are mono uppercase`, /mono/i.test(lab.fontFamily) && lab.textTransform === "uppercase", JSON.stringify(lab));
      const dot = await css(first.getByTestId("mq-dot").first(), ["borderRadius", "width", "height"]);
      ck(`${width}: check dots are squares`, dot.borderRadius === "0px" && dot.width === dot.height, JSON.stringify(dot));
      ck(`${width}: a CI line that waits says so`, (await text(first.getByTestId("mq-check-ci"))).includes("Pending"), await text(first.getByTestId("mq-check-ci")));
      ck(`${width}: a row with no question has no buttons`, (await page.getByTestId("mq-actions").count()) === 0);
      ck(`${width}: the merged list is closed until asked`, (await page.getByTestId("mq-merged-item").count()) === 0);
      ck(`${width}: the merged toggle is a flat row, not a boxed button`, await flat(page.getByTestId("mq-merged-toggle")), JSON.stringify(await css(page.getByTestId("mq-merged-toggle"), ["borderTopWidth", "backgroundColor", "display"])));
      await page.getByTestId("mq-merged-toggle").click();
      ck(`${width}: the merged list names the ticket, the pull request, when and by whom`, (await text(page.getByTestId("mq-merged-item").first())).includes("ORD-38"), await text(page.getByTestId("mq-merged")));
      const long = rowOf(page, "ORD-45");
      ck(`${width}: long text wraps inside the card`, await long.evaluate((el) => el.scrollWidth <= el.clientWidth + 1));
      ck(`${width}: the footer's Pull requests tab is a link`, (await text(page.getByTestId("mq-foot-prs"))) === "Pull requests tab");
      await state(page, "waiting for CI, long text", width, "waiting-ci");
      await rowShot(long, `long-text-${width}.png`);

      console.log(`== the tab is in the address, the footer link switches tabs at ${width} ==`);
      await page.getByTestId("mq-foot-prs").click();
      await page.getByTestId("pr-queue-note").waitFor();
      ck(`${width}: the footer link opens the Pull requests tab`, !page.url().includes("tab=queue") && (await page.getByTestId("tab-prs").getAttribute("aria-selected")) === "true", page.url());
      ck(`${width}: the Pull requests tab shows its own content and no queue rows`, (await page.getByTestId("mq-page").count()) === 0 && (await page.getByTestId("github-page").count()) === 1);
      ck(`${width}: the Pull requests tab says how many are in the queue`, (await text(page.getByTestId("pr-queue-note"))).startsWith("4 pull requests are in the merge queue."), await text(page.getByTestId("pr-queue-note")));
      await state(page, "pull requests tab", width, "tab-pull-requests");
      await page.reload({ waitUntil: "load" });
      await page.getByTestId("pr-queue-note").waitFor();
      ck(`${width}: a reload keeps the Pull requests tab`, (await page.getByTestId("tab-prs").getAttribute("aria-selected")) === "true");
      await page.getByTestId("pr-queue-link").click();
      await page.getByTestId("mq-page").waitFor();
      ck(`${width}: the note on that tab opens the queue, in the address`, page.url().endsWith("?tab=queue") && (await page.getByTestId("tab-queue").getAttribute("aria-selected")) === "true", page.url());
      await page.reload({ waitUntil: "load" });
      await page.getByTestId("mq-page").waitFor();
      ck(`${width}: a reload keeps the Merge queue tab`, (await page.getByTestId("tab-queue").getAttribute("aria-selected")) === "true");
      await page.getByTestId("tab-prs").click();
      await page.getByTestId("pr-queue-note").waitFor();
      await page.getByTestId("tab-queue").click();
      await page.getByTestId("mq-page").waitFor();
      ck(`${width}: the tabs switch back and forth`, (await page.getByTestId("tab-queue").getAttribute("aria-selected")) === "true");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      await page.goto(`${BASE}/projects/${U("p-wait")}/merge-queue`, { waitUntil: "load" });
      await page.waitForTimeout(500);
      ck(`${width}: the old Merge queue address is not a page any more`, (await page.getByTestId("mq-page").count()) === 0);
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== waiting for approval, answered, moved at ${width} ==`);
      await open(page, "p-ask");
      const mine = rowOf(page, "ASK-1");
      ck(`${width}: your question: the chip says it needs your yes`, (await text(mine.getByTestId("mq-chip"))).toLowerCase() === "needs your yes", await text(mine.getByTestId("mq-chip")));
      ck(`${width}: your question has Yes, merge and No, hold`, (await text(mine.getByTestId("mq-yes"))) === "Yes, merge" && (await text(mine.getByTestId("mq-no"))) === "No, hold");
      ck(`${width}: your question has a left accent bar`, (await mine.getAttribute("data-bar")) === "you"
         && (await css(mine, ["boxShadow"])).boxShadow !== "none", await mine.getAttribute("data-bar"));
      const lead = mine.getByTestId("mq-lead");
      ck(`${width}: the lead is bold and not red when it waits on you`, (await css(lead.getByTestId("mq-lead-b"), ["color"])).color !== (await token(page, "--err")));
      const calls = mine.getByTestId("mq-calls");
      ck(`${width}: Open it on the Calls page is a link on a row that waits on you`, (await text(calls)) === "Open it on the Calls page"
         && (await calls.getAttribute("href")) === `/projects/${U("p-ask")}/calls`, await calls.getAttribute("href"));
      ck(`${width}: that link is underlined`, (await css(calls, ["textDecorationLine"])).textDecorationLine.includes("underline"));
      if (narrow) {
        const card = await mine.boundingBox();
        const groupBox = await page.getByTestId("mq-group").boundingBox();
        ck("390: the row is an inset card inside the group", card.x > groupBox.x + 4 && card.x + card.width < groupBox.x + groupBox.width - 4, JSON.stringify([card, groupBox]));
        const chip = await mine.getByTestId("mq-chip").boundingBox();
        ck("390: the chip sits top right of the card", chip.y < card.y + 40 && chip.x + chip.width > card.x + card.width - 30, JSON.stringify([chip, card]));
        const yb = await mine.getByTestId("mq-yes").boundingBox();
        const nb = await mine.getByTestId("mq-no").boundingBox();
        const ab = await mine.getByTestId("mq-actions").boundingBox();
        ck("390: Yes and No share the full width", yb.width > ab.width * 0.4 && nb.width > ab.width * 0.4 && yb.width + nb.width > ab.width - 20, JSON.stringify([yb, nb, ab]));
      } else {
        const yb = await mine.getByTestId("mq-yes").boundingBox();
        ck("1440: the buttons keep their own width", yb.width < 220, JSON.stringify(yb));
      }
      const other = rowOf(page, "ASK-2");
      ck(`${width}: someone else's question: the chip names them, no buttons`, (await text(other.getByTestId("mq-chip"))).toLowerCase().startsWith("waiting for dana") && (await other.getByTestId("mq-actions").count()) === 0, await text(other));
      ck(`${width}: someone else's question has no Calls link to act on`, (await other.getByTestId("mq-calls").count()) === 0);
      await rowShot(other, `other-approval-${width}.png`);
      const moved = rowOf(page, "ASK-3");
      ck(`${width}: a moved commit says so and cannot be answered`, (await text(moved.getByTestId("mq-chip"))).toLowerCase() === "commit changed" && (await moved.getByTestId("mq-actions").count()) === 0, await text(moved));
      await rowShot(moved, `head-moved-${width}.png`);
      const yes = rowOf(page, "ASK-4");
      ck(`${width}: answered yes: says who, where and when, no buttons`, (await yes.getByTestId("mq-actions").count()) === 0 && (await text(yes.getByTestId("mq-status"))).includes("Yes, merge"), await text(yes.getByTestId("mq-status")));
      await rowShot(yes, `answered-yes-${width}.png`);
      const no = rowOf(page, "ASK-5");
      ck(`${width}: answered no: on hold, no buttons`, (await no.getByTestId("mq-actions").count()) === 0 && (await text(no.getByTestId("mq-status"))).includes("No, hold"), await text(no));
      await rowShot(no, `answered-no-${width}.png`);
      ck(`${width}: the tab says what needs you`, (await text(page.getByTestId("tab-queue-need"))) === "1 needs you", await text(page.getByTestId("tab-queue")));
      ck(`${width}: the aside bolds the count`, (await text(page.getByTestId("mq-aside"))).includes("needs you")
         && (await page.getByTestId("mq-aside").locator("b").count()) === 1);
      await state(page, "waiting for approval", width, "approval");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== blocked and sent back at ${width} ==`);
      await open(page, "p-block");
      ck(`${width}: two groups, one per repo`, (await page.getByTestId("mq-group").count()) === 2);
      const rules = rowOf(page, "BLK-1");
      ck(`${width}: blocked by repo rules says why`, (await text(rules.getByTestId("mq-chip"))).toLowerCase() === "blocked" && (await text(rules)).includes("two approving reviews"), await text(rules));
      ck(`${width}: a blocked row has a left accent bar in red`, (await rules.getAttribute("data-bar")) === "bad", await rules.getAttribute("data-bar"));
      ck(`${width}: a blocked row leads in red`, (await css(rules.getByTestId("mq-lead-b"), ["color"])).color === (await token(page, "--err")));
      ck(`${width}: a blocked row links to the Calls page`, (await rules.getByTestId("mq-calls").count()) === 1
         && (await rules.getByTestId("mq-calls").getAttribute("href")) === `/projects/${U("p-block")}/calls`, await text(rules));
      const noGh = rowOf(page, "BLK-2");
      ck(`${width}: blocked with no GitHub connection says so`, (await text(noGh)).includes("No GitHub connection can write to example-org/web"), await text(noGh));
      ck(`${width}: and its note links to the Calls page`, (await noGh.getByTestId("mq-calls").count()) === 1 && (await text(noGh.getByTestId("mq-calls"))) === "Calls page", await text(noGh));
      const back = rowOf(page, "BLK-3");
      ck(`${width}: sent back is out of line and says how often`, (await text(back.getByTestId("mq-chip"))).toLowerCase() === "sent back" && (await text(back.getByTestId("mq-bounce"))).includes("Sent back 2 of 3 times"), await text(back));
      ck(`${width}: sent back has a left accent bar in red and a red lead`, (await back.getAttribute("data-bar")) === "bad"
         && (await css(back.getByTestId("mq-lead-b"), ["color"])).color === (await token(page, "--err")));
      ck(`${width}: the ladder has five pips, two filled`, (await back.getByTestId("mq-pips").locator("span").count()) === 5
         && (await back.getByTestId("mq-pips").locator("span[data-on=yes]").count()) === 2);
      const back5 = rowOf(page, "BLK-4");
      ck(`${width}: sent back five times: five pips and the Calls page link`, (await back5.getByTestId("mq-pips").locator("span[data-on=yes]").count()) === 5
         && (await back5.getByTestId("mq-bounce").getByTestId("mq-calls").count()) === 1 && (await text(back5.getByTestId("mq-bounce"))).includes("Calls page"), await text(back5));
      await state(page, "blocked and sent back", width, "blocked");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== withdrawn at ${width} ==`);
      await open(page, "p-wd");
      const wd = rowOf(page, "WDR-1");
      ck(`${width}: a withdrawn question says the pull request is on hold`, (await text(wd.getByTestId("mq-lead"))).startsWith("The question was withdrawn"), await text(wd));
      ck(`${width}: it can be approved, not turned down`, (await wd.getByTestId("mq-yes").count()) === 1 && (await wd.getByTestId("mq-no").count()) === 0);
      ck(`${width}: it links to the Calls page`, (await wd.getByTestId("mq-calls").count()) === 1 && (await wd.getByTestId("mq-calls").getAttribute("href")) === `/projects/${U("p-wd")}/calls`);
      await state(page, "withdrawn", width, "withdrawn");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== many and capped at ${width} ==`);
      await open(page, "p-many");
      ck(`${width}: five rows show`, (await page.getByTestId("mq-row").count()) === 5);
      ck(`${width}: the rest wait behind Show 3 more`, (await text(page.getByTestId("mq-more-toggle"))) === "Show 3 more");
      ck(`${width}: the group says how many are behind it`, (await text(page.getByTestId("mq-hidden"))) === "3 more in this group are behind Show more below.", await text(page.getByTestId("mq-hidden")));
      await state(page, "many, capped", width, "many-capped");
      await page.getByTestId("mq-more-toggle").click();
      ck(`${width}: all eight show`, (await page.getByTestId("mq-row").count()) === 8);
      ck(`${width}: the toggle offers fewer`, (await text(page.getByTestId("mq-more-toggle"))) === "Show fewer");
      await state(page, "many, all", width, "many-all");
      await page.getByTestId("mq-group-toggle").click();
      ck(`${width}: the group closes`, (await page.getByTestId("mq-row").count()) === 0);
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      await open(page, "p-cap");
      const capped = await page.getByTestId("mq-row").evaluateAll((els) => els.map((e) => e.getAttribute("data-ticket")));
      ck(`${width}: a row that needs you shows even when it is last in line`, capped.length === 5 && capped.includes("CAP-7") && capped.join() === "CAP-1,CAP-2,CAP-3,CAP-4,CAP-7", capped.join());
      ck(`${width}: the hidden rows are counted`, (await text(page.getByTestId("mq-hidden"))) === "2 more in this group are behind Show more below.", await text(page.getByTestId("mq-hidden")));
      await state(page, "capped, needs you first", width, "capped-needs-you");
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      console.log(`== empty, not a factory project, loading, error at ${width} ==`);
      await open(page, "p-empty");
      ck(`${width}: an empty queue says so`, (await page.getByTestId("mq-empty").count()) === 1);
      await state(page, "empty", width, "empty");

      await page.goto(url("p-other"), { waitUntil: "load" });
      await page.waitForURL(`**/projects/${U("p-other")}`, { timeout: 8000 }).catch(() => {});
      ck(`${width}: a project that is not a factory project does not show the page`, (await page.getByTestId("mq-page").count()) === 0);
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      let release;
      const gate = new Promise((r) => { release = r; });
      await page.route("**/api/projects/*/merge-queue", async (route) => { await gate; await route.continue(); });
      await page.goto(url("p-wait"), { waitUntil: "load" });
      await page.getByTestId("mq-wait").waitFor();
      ck(`${width}: while loading, a placeholder shows and no rows`, (await page.getByTestId("mq-row").count()) === 0 && (await page.getByRole("status", { name: "Loading the merge queue" }).count()) === 1);
      await state(page, "loading", width, "loading");
      release();
      await page.getByTestId("mq-page").waitFor();
      await ctx.close();

      ({ ctx, page } = await newPage(width));
      await page.route("**/api/projects/*/merge-queue", (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"down"}' }));
      await page.goto(url("p-wait"), { waitUntil: "load" });
      await page.getByTestId("mq-error").waitFor();
      ck(`${width}: a failed read says so and offers to try again`, (await text(page.getByTestId("mq-error"))).includes("did not load") && (await page.getByRole("button", { name: "Try again" }).count()) === 1);
      await state(page, "error", width, "error");
      await page.getByTestId("tab-prs").click();
      await page.getByTestId("pr-queue-note").waitFor();
      ck(`${width}: the Pull requests tab still works when the queue did not load`, (await page.getByTestId("github-page").count()) === 1
         && (await text(page.getByTestId("pr-queue-note"))).includes("not shown"), await text(page.getByTestId("pr-queue-note")));
      await ctx.close();

      // A read that fails after a good one keeps what shows and says so.
      ({ ctx, page } = await newPage(width));
      let broken = false;
      await page.route("**/api/projects/*/merge-queue", (route) => (broken
        ? route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"down"}' })
        : route.continue()));
      await open(page, "p-wait");
      ck(`${width}: no stale banner while reads work`, (await page.getByTestId("mq-stale").count()) === 0);
      broken = true;
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await page.getByTestId("mq-stale").waitFor();
      ck(`${width}: a failed refresh keeps the rows and says what shows is old`, (await page.getByTestId("mq-row").count()) === 4
         && (await text(page.getByTestId("mq-stale"))).includes("could not be refreshed"), await text(page.getByTestId("mq-stale")));
      await state(page, "stale", width, "stale");
      broken = false;
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await page.getByTestId("mq-stale").waitFor({ state: "detached" });
      ck(`${width}: the banner goes when a read works again`, (await page.getByTestId("mq-stale").count()) === 0);
      await ctx.close();
    }

    // ── answering: one project per width, since an answer changes the cell ──
    for (const [width, name] of [[1440, "p-act"], [390, "p-act2"]]) {
      console.log(`== answering at ${width} ==`);
      const { ctx, page } = await newPage(width);
      await open(page, name);
      const queueRoute = `/api/projects/${U(name)}/merge-queue`;
      // each POST to /reply: the url and its JSON body
      const posts = [];
      let hold = null;            // while set, the queue read waits for it (the answer saved, the refresh not yet back)
      let replyFails = true;
      await page.route("**/api/desk/*/reply", async (route) => {
        posts.push({ url: route.request().url(), body: JSON.parse(route.request().postData() || "{}") });
        if (replyFails) return route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"no"}' });
        return route.continue();
      });
      await page.route(`**${queueRoute}`, async (route) => { if (hold) await hold; await route.continue(); });

      // 1. a failed answer says nothing changed, keeps the buttons and keeps the row open
      const failing = rowOf(page, "ACT-3");
      await failing.getByTestId("mq-yes").click();
      await failing.getByTestId("mq-failed").waitFor();
      ck(`${width}: a failed answer says nothing was saved and keeps the buttons`, (await failing.getByTestId("mq-yes").count()) === 1
         && (await text(failing.getByTestId("mq-failed"))).includes("was not saved") && (await failing.getByTestId("mq-yes").isEnabled()), await text(failing));
      ck(`${width}: the failed state is a live status region`, (await failing.getByTestId("mq-status").getAttribute("role")) === "status");
      ck(`${width}: the failed answer was sent as A, Yes, merge`, posts.length === 1 && posts[0].body.words === "A" && posts[0].body.choice === "Yes, merge", JSON.stringify(posts));
      await checkWords(page, ck, `answer failed ${width}`);
      ck(`answer failed ${width}: no sideways scroll`, (await overflow(page)) <= 0);
      await shot(page, `answer-failed-${width}.png`);
      replyFails = false;

      // 2. Yes: stays "saving" until the queue is read again, with the body A and Yes, merge
      const yes = rowOf(page, "ACT-1");
      let release;
      hold = new Promise((r) => { release = r; });
      const before = posts.length;
      await yes.getByTestId("mq-yes").click();
      await yes.getByText("Saving your answer.").waitFor();
      ck(`${width}: while it saves, the buttons are off`, (await yes.getByTestId("mq-yes").isDisabled()) && (await yes.getByTestId("mq-no").isDisabled()));
      ck(`${width}: Yes sent A, Yes, merge to the question's own reply route`, posts.length === before + 1
         && posts[before].body.words === "A" && posts[before].body.choice === "Yes, merge" && /\/api\/desk\/[^/]+\/reply$/.test(new URL(posts[before].url).pathname),
         JSON.stringify(posts[before]));
      await page.waitForTimeout(600);
      ck(`${width}: after the reply it is still saving while the queue read is out`, (await yes.getAttribute("data-phase")) === "saving"
         && (await yes.getByTestId("mq-yes").isDisabled()) && (await yes.getByTestId("mq-actions").count()) === 1, await yes.getAttribute("data-phase"));
      await checkWords(page, ck, `answering ${width}`);
      ck(`answering ${width}: no sideways scroll`, (await overflow(page)) <= 0);
      await shot(page, `answering-${width}.png`);
      release();
      hold = null;
      await yes.getByTestId("mq-actions").waitFor({ state: "detached", timeout: 8000 });
      ck(`${width}: the page read the queue again and the row shows its answer`, (await text(yes.getByTestId("mq-status"))).includes("Yes, merge"), await text(yes));
      ck(`${width}: one answer, one POST per click`, posts.length === before + 1, String(posts.length));

      // 3. No sends B, No, hold
      const no = rowOf(page, "ACT-2");
      await no.getByTestId("mq-no").click();
      await no.getByTestId("mq-actions").waitFor({ state: "detached", timeout: 8000 });
      ck(`${width}: No sent B, No, hold`, posts.length === before + 2 && posts[before + 1].body.words === "B" && posts[before + 1].body.choice === "No, hold", JSON.stringify(posts[before + 1]));
      ck(`${width}: No, hold closes the question too`, (await text(no.getByTestId("mq-status"))).includes("No, hold"), await text(no));

      // 4. the row that failed earlier: someone else answers it (straight to the cell), the next read changes
      //    the row, and its old "not saved" line goes with it. A refresh with the same answer would keep it.
      replyFails = true;
      await failing.getByTestId("mq-yes").click();
      await failing.getByTestId("mq-failed").waitFor();
      ck(`${width}: the row fails again before somebody else answers it`, (await failing.getByTestId("mq-failed").count()) === 1);
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await page.waitForTimeout(700);
      ck(`${width}: a refresh that changes nothing keeps the failed line`, (await failing.getByTestId("mq-failed").count()) === 1);
      const q = await (await fetch(`${BASE}${queueRoute}`)).json();
      const desk = q.groups.flatMap((g) => g.rows).find((r) => r.ticket.ref === "ACT-3").answer.item.id;
      const r = await fetch(`${BASE}/api/desk/${encodeURIComponent(desk)}/reply`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ words: "B", standing: false, choice: "No, hold" }) });
      ck(`${width}: the other answer was saved`, r.ok, String(r.status));
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await failing.getByTestId("mq-failed").waitFor({ state: "detached", timeout: 8000 });
      ck(`${width}: the failed line goes when the row's answer changes`, (await failing.getByTestId("mq-failed").count()) === 0
         && (await failing.getByTestId("mq-actions").count()) === 0 && (await text(failing.getByTestId("mq-status"))).includes("No, hold"), await text(failing));
      await state(page, "answered", width, "answered");
      await ctx.close();
    }

    // ── withdrawn: approving goes through the approve route, there is no No ──
    {
      const width = 1440;
      console.log(`== withdrawn approve at ${width} ==`);
      const { ctx, page } = await newPage(width);
      const posts = [];
      page.on("request", (rq) => { if (rq.method() === "POST") posts.push({ path: new URL(rq.url()).pathname, body: rq.postData() }); });
      await open(page, "p-wd2");
      const wd = rowOf(page, "WDR-1");
      ck(`${width}: the withdrawn row can be approved and not turned down`, (await wd.getByTestId("mq-yes").count()) === 1 && (await wd.getByTestId("mq-no").count()) === 0);
      await wd.getByTestId("mq-yes").click();
      await wd.getByTestId("mq-actions").waitFor({ state: "detached", timeout: 8000 });
      const ap = posts.filter((x) => /^\/api\/factory\/merge-queue\/[^/]+\/approve$/.test(x.path));
      ck(`${width}: Approve posted once to the approve route`, ap.length === 1 && posts.length === 1, JSON.stringify(posts));
      ck(`${width}: it did not use the desk reply route`, !posts.some((x) => x.path.startsWith("/api/desk/")));
      ck(`${width}: after approving, the withdrawn line is gone`, !(await text(wd)).includes("The question was withdrawn"), await text(wd));
      await state(page, "withdrawn approved", width, "withdrawn-approved");
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
