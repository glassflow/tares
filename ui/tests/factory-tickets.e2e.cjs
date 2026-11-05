/*
 * Browser e2e for TR-806: the factory Tickets page.
 *
 * Two parts. Samples: with the browser flag taresSamples=1 each sample (many, long, empty, load, err) is
 * opened in the three layouts at 1440 and 390 wide, with the words check and no horizontal scroll.
 * Live: a scratch factory cell seeded by tests/seed-factory-tickets.py: one read of
 * GET /api/factory/tickets per load; the Board (columns, counts, the 8 card cap, Show more, the hidden
 * canceled line and Show), the List (25 at a time, Show more, sort, search, project and milestone and
 * state filters, Clear filters, the zero box), the Swimlanes (grouping by project, milestone and who,
 * collapsing, the 5 card cap), the layout the person picks (kept, and the address says so only when the
 * filters would not open it), the state tabs at 390 wide, markup in a title shown as text, the error
 * banner with Retry.
 * Run:  npm run test:factory-tickets     (builds first; tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_PORT (default 8851), MCP port +1.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8851);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["many", "long", "empty", "load", "err"];
const LAYOUTS = ["list", "board", "lanes"];
const ROUTE = "/api/factory/tickets";

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-tickets-e2e-"));
  const base = { PYTHONPATH: REPO, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                 TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off" };
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-tickets.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8", env: cleanEnv({ ...base, TARES_EDITION: "factory" }) })
    .trim().split("\n").pop());
  ck("seeded 4 projects", Object.keys(ids).length === 4, String(Object.keys(ids).length));
  const X = ids["project-x"], Y = ids["project-y"];

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
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-tickets-${name}-${width}.png`), fullPage: true });
    };
    const fresh = async (width, flagged) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      if (flagged) await ctx.addInitScript(() => { try { window.localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ } });
      return { ctx, page: await ctx.newPage() };
    };
    const texts = (loc) => loc.allInnerTexts().then((a) => a.map((t) => t.replace(/\s+/g, " ").trim()));
    const text = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const query = (page) => Object.fromEntries(new URL(page.url()).searchParams);
    const idle = (page) => page.waitForLoadState("networkidle");
    // The filters are themed menus (Radix), not <select>s: open one, read or click its options.
    const opts = async (page, id) => {
      await page.getByTestId(id).click();
      await page.getByRole("listbox").waitFor();
      const o = await page.getByRole("option").evaluateAll((els) => els.map((x) => ({ v: x.getAttribute("data-value"), t: x.textContent.replace(/\s+/g, " ").trim() })));
      await page.keyboard.press("Escape");
      await page.getByRole("listbox").waitFor({ state: "detached" });
      return o;
    };
    const pick = async (page, id, value) => {
      await page.getByTestId(id).click();
      await page.locator(`[role="option"][data-value="${value}"]`).click();
    };

    // ---- samples, in every layout at both widths
    for (const width of [1440, 390]) {
      const { ctx, page } = await fresh(width, true);
      for (const s of SAMPLES) {
        for (const l of LAYOUTS) {
          const tag = `${s} ${l} @${width}`;
          await page.goto(`${BASE}/tickets?sample=${s}&layout=${l}`, { waitUntil: "load" });
          if (s === "load") await page.getByTestId("tickets-loading").waitFor();
          else if (s === "err") await page.getByTestId("tickets-retry").waitFor();
          else if (s === "empty") await page.getByTestId("tickets-empty").waitFor();
          else await page.getByTestId("tickets-count").waitFor();
          await idle(page);
          await shot(page, `${s}-${l}`, width);
          await checkWords(page, ck, tag);
          ck(`${tag}: no horizontal scroll`, (await overflow(page)) <= 0 && (await inside(page)), `${await overflow(page)}px over`);
          ck(`${tag}: the title is Tickets`, (await page.locator("h1").first().innerText()).trim() === "Tickets");
        }
      }
      await page.goto(`${BASE}/tickets?sample=empty`, { waitUntil: "load" });
      await page.getByTestId("tickets-empty").waitFor();
      ck(`empty @${width}: no projects, with Go to Projects`, (await page.getByRole("link", { name: "Go to Projects" }).getAttribute("href")) === "/projects");
      await page.goto(`${BASE}/tickets?sample=err`, { waitUntil: "load" });
      await page.getByTestId("tickets-retry").waitFor();
      ck(`err @${width}: the banner says what failed, with Retry`, /could not load the tickets/i.test(await page.getByRole("alert").innerText())
         && (await page.getByTestId("tickets-retry").count()) === 1);
      await page.goto(`${BASE}/tickets?sample=load`, { waitUntil: "load" });
      await page.getByTestId("tickets-loading").waitFor();
      ck(`load @${width}: a placeholder and no tickets`, (await page.getByRole("status", { name: "Loading" }).count()) === 1
         && (await page.getByTestId("ticket-card").count()) === 0);
      await page.goto(`${BASE}/tickets?sample=long&layout=list`, { waitUntil: "load" });
      await page.getByTestId("tickets-count").waitFor();
      ck(`long list @${width}: long titles stay inside their rows`, await page.getByTestId("ticket-row")
        .evaluateAll((r) => r.every((x) => x.scrollWidth <= x.clientWidth + 1)));
      await ctx.close();
    }

    // ---- live, from the seeded cell
    for (const width of [1440, 390]) {
      const narrow = width === 390;
      const { ctx, page } = await fresh(width, false);
      const tag = (s) => `live ${s} @${width}`;
      const cards = page.getByTestId("ticket-card");
      const rows = page.getByTestId("ticket-row");
      const openLive = async (search = "", ready = "tickets-count") => {
        const done = expectOneCall(page, ROUTE);
        await page.goto(`${BASE}/tickets${search}`, { waitUntil: "load" });
        await page.getByTestId(ready).waitFor();
        await done(ck, tag(`/tickets${search}`));
      };
      const settle = async () => { await page.waitForFunction(() => document.querySelector('[data-testid="tickets-body"]')?.getAttribute("class")?.includes("opacity-60") === false); await idle(page); };
      const tab = (k) => page.locator(`[data-testid="state-tab"][data-state="${k}"]`);
      const col = (k) => page.locator(`[data-testid="board-col"][data-state="${k}"]`);

      // the Board is where an address with no filter opens
      await openLive();
      await shot(page, "live-board", width);
      await checkWords(page, ck, tag("board"));
      ck(`${tag("board")}: no horizontal scroll`, (await overflow(page)) <= 0 && (await inside(page)), `${await overflow(page)}px over`);
      ck(`${tag("board")}: the board is the layout and the address stays clean`, (await page.getByTestId("tickets-body").getAttribute("data-layout")) === "board"
         && new URL(page.url()).search === "");
      ck(`${tag("board")}: the sentence under the title`, (await text(page.getByTestId("tickets-sub"))) === "34 tickets in 4 projects", await text(page.getByTestId("tickets-sub")));
      ck(`${tag("board")}: the canceled ticket is hidden, said with a Show link`, /^1 canceled ticket is hidden\.\s*Show$/.test(await text(page.getByTestId("canceled-hint"))));
      ck(`${tag("board")}: a layout switch with List, Board, Swimlanes`, (await texts(page.getByRole("group", { name: "Layout" }).getByRole("button"))).join("|") === "List|Board|Swimlanes");
      ck(`${tag("board")}: Milestone waits for a project`, await page.getByTestId("f-milestone").isDisabled());
      ck(`${tag("board")}: no sort on the board`, (await page.getByTestId("f-sort").count()) === 0);

      if (!narrow) {
        ck(`${tag("board")}: six columns in order`, (await page.getByTestId("board-col").evaluateAll((c) => c.map((x) => x.dataset.state))).join(",")
           === "to_do,in_progress,in_review,changes_asked,blocked,merged");
        ck(`${tag("board")}: the counts on the column heads`, (await texts(page.getByTestId("col-count"))).join(",") === "16,2,1,1,1,13", (await texts(page.getByTestId("col-count"))).join(","));
        ck(`${tag("board")}: 8 cards of the 16 to do, and the line says so`, (await col("to_do").getByTestId("ticket-card").count()) === 8
           && (await text(col("to_do").getByTestId("cell-showing"))) === "Showing 8 of 16");
        await col("to_do").getByTestId("cell-more").click();
        ck(`${tag("board")}: Show 8 more shows the rest`, (await col("to_do").getByTestId("ticket-card").count()) === 16
           && (await col("to_do").getByTestId("cell-more").count()) === 0);
        ck(`${tag("board")}: the blocked card names what blocks it, in red`, (await text(col("blocked").getByTestId("ticket-card"))).includes("Blocked by T13"));
        ck(`${tag("board")}: a card in progress with a pull request says so`, /PR 113 open/.test(await text(col("in_progress").getByTestId("ticket-card").first()))
           || /PR 21 open/.test(await text(col("in_progress").getByTestId("ticket-card").first())));
      } else {
        ck(`${tag("board")}: six state tabs and no columns`, (await page.getByTestId("state-tab").count()) === 6 && (await page.getByTestId("board-col").count()) === 0);
        ck(`${tag("board")}: each tab carries its count`, (await texts(page.getByTestId("state-tab"))).join("|")
           === "To do 16|In progress 2|In review 1|Changes asked 1|Blocked 1|Merged 13", (await texts(page.getByTestId("state-tab"))).join("|"));
        ck(`${tag("board")}: one state at a time, In progress first`, (await tab("in_progress").getAttribute("aria-selected")) === "true"
           && (await cards.count()) === 2);
        await tab("to_do").click();
        ck(`${tag("board")}: To do shows 8 of 16`, (await cards.count()) === 8 && (await text(page.getByTestId("cell-showing"))) === "Showing 8 of 16");
        await page.getByTestId("cell-more").click();
        ck(`${tag("board")}: Show 8 more shows 16`, (await cards.count()) === 16);
        ck(`${tag("board")}: no horizontal scroll with the tabs`, (await overflow(page)) <= 0 && (await inside(page)));
        const box = await page.getByTestId("state-tabs").boundingBox();
        const first = await tab("to_do").boundingBox(), fourth = await tab("changes_asked").boundingBox();
        ck(`${tag("board")}: the tabs sit three across, two rows`, first && fourth && Math.abs(first.x - fourth.x) < 2 && fourth.y > first.y && box.width <= width);
        await shot(page, "live-board-todo", width);
      }

      // Show canceled
      await page.getByTestId("show-canceled").click();
      await settle();
      ck(`${tag("canceled")}: Show asks for the canceled tickets in the address`, query(page).state === "canceled");
      ck(`${tag("canceled")}: one column, and the line is gone`, (narrow ? (await page.getByTestId("state-tab").count()) === 1 : (await page.getByTestId("board-col").count()) === 1)
         && (await page.getByTestId("canceled-hint").count()) === 0);
      ck(`${tag("canceled")}: the ticket is struck through`, (await cards.count()) === 1
         && /line-through/.test(await cards.first().locator("[data-user-text]").first().getAttribute("class")));
      ck(`${tag("canceled")}: Clear filters is offered`, await page.getByTestId("clear-filters").isVisible());
      await page.getByTestId("clear-filters").click();
      await settle();
      ck(`${tag("canceled")}: cleared`, Object.keys(query(page)).length === 0 && (await page.getByTestId("canceled-hint").count()) === 1);

      // moving: three states
      await openLive("?state=moving", "tickets-count");
      ck(`${tag("moving")}: a filter opens the List`, (await page.getByTestId("tickets-body").getAttribute("data-layout")) === "list" && (await rows.count()) === 4);
      await page.getByTestId("layout-board").click();
      await settle();
      ck(`${tag("moving")}: Board with Moving now shows three states`, (narrow ? (await page.getByTestId("state-tab").count()) : (await page.getByTestId("board-col").count())) === 3
         && query(page).layout === "board" && query(page).state === "moving", JSON.stringify(query(page)));
      await shot(page, "live-moving", width);

      // the layout the person picks is kept
      await openLive();
      await page.getByTestId("layout-list").click();
      await settle();
      ck(`${tag("layout")}: List is asked for in the address, and the list shows`, query(page).layout === "list" && (await rows.count()) === 25);
      await shot(page, "live-list", width);
      await checkWords(page, ck, tag("list"));
      ck(`${tag("list")}: no horizontal scroll`, (await overflow(page)) <= 0 && (await inside(page)), `${await overflow(page)}px over`);
      await page.goto(`${BASE}/tickets`, { waitUntil: "load" });
      await page.getByTestId("tickets-count").waitFor();
      await idle(page);
      ck(`${tag("layout")}: the choice is kept: a visit with no layout opens the List`, (await page.getByTestId("tickets-body").getAttribute("data-layout")) === "list");
      await page.getByTestId("layout-board").click();
      await settle();
      ck(`${tag("layout")}: the Board matches the unfiltered default, so the address carries no layout`, query(page).layout === undefined
         && (await page.getByTestId("tickets-body").getAttribute("data-layout")) === "board");
      await page.goto(`${BASE}/tickets?project=${X}`, { waitUntil: "load" });
      await page.getByTestId("tickets-count").waitFor();
      await idle(page);
      ck(`${tag("layout")}: a person who picked the Board keeps it with a filter`, (await page.getByTestId("tickets-body").getAttribute("data-layout")) === "board");

      // list: paging, sorting, filters
      await page.evaluate(() => window.localStorage.removeItem("tares-tickets-layout"));
      await openLive(`?project=${X}`);
      ck(`${tag("list")}: a project filter opens the List with 25 of 30`, (await rows.count()) === 25
         && /^30 tickets match, showing 25\.?/.test(await text(page.getByTestId("tickets-count-line"))), await text(page.getByTestId("tickets-count-line")));
      ck(`${tag("list")}: the sentence under the title for a filter`, (await text(page.getByTestId("tickets-sub"))) === "Every ticket of every project the crew holds");
      ck(`${tag("list")}: newest change first`, (await rows.first().getAttribute("data-ref")) === "T1", await rows.first().getAttribute("data-ref"));
      ck(`${tag("list")}: Milestone is open now, with the project's three`, !(await page.getByTestId("f-milestone").isDisabled())
         && (await opts(page, "f-milestone")).length === 4);
      ck(`${tag("list")}: the State menu carries counts`, /^All states\|Moving now \(3\)\|In progress \(1\)\|In review \(1\)\|Changes asked \(1\)\|Blocked \(1\)\|To do \(14\)\|Merged \(12\)\|Canceled \(1\)$/
         .test((await opts(page, "f-state")).map((o) => o.t).join("|")), (await opts(page, "f-state")).map((o) => o.t).join("|"));
      await page.getByTestId("list-more").click();
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="ticket-row"]').length === 30);
      ck(`${tag("list")}: Show more appends the last 5, then the button is gone`, (await rows.count()) === 30 && (await page.getByTestId("list-more").count()) === 0);
      ck(`${tag("list")}: no ticket twice`, new Set(await rows.evaluateAll((r) => r.map((x) => x.dataset.ref))).size === 30);
      await shot(page, "live-list-project", width);

      // a row: words and one link
      await openLive(`?project=${X}&state=blocked`);
      const row = rows.first();
      ck(`${tag("row")}: the blocked row says what blocks it as its state`, /Blocked by T13/.test(await text(row.getByTestId("ticket-state"))));
      ck(`${tag("row")}: it is one link to the ticket`, (await row.getAttribute("href")) === `/projects/${X}/tickets/T16`);
      await openLive(`?project=${X}&state=in-review`);
      ck(`${tag("row")}: ref, title, state, pull request`, (await text(rows.first().getByTestId("ticket-ref"))) === "T14" && (await text(rows.first().getByTestId("ticket-title"))) === "Cache the counts"
         && (await text(rows.first().getByTestId("ticket-state"))) === "In review" && /PR 114 /.test(await text(rows.first())), await text(rows.first()));
      await rows.first().click();
      await page.waitForURL(new RegExp(`/projects/${X}/tickets/T14`));
      ck(`${tag("row")}: the row opens the ticket inside its project`, new URL(page.url()).pathname === `/projects/${X}/tickets/T14`, page.url());
      await page.goto("about:blank"); // leave the ticket page, it polls every 15 s and would put its read into the next screen's count

      // sort
      await openLive(`?project=${X}&layout=list`);
      await pick(page, "f-sort", "old");
      await settle();
      ck(`${tag("sort")}: oldest change first (the canceled T31 is hidden)`, query(page).sort === "old" && (await rows.first().getAttribute("data-ref")) === "T30",
         `${query(page).sort} ${await rows.first().getAttribute("data-ref")}`);
      await pick(page, "f-sort", "ref");
      await settle();
      ck(`${tag("sort")}: by ref`, query(page).sort === "ref" && (await rows.first().getAttribute("data-ref")) === "T1" && (await rows.nth(1).getAttribute("data-ref")) === "T2");

      // search
      await openLive(`?layout=list`);
      await page.getByTestId("f-q").fill("speed");
      await page.waitForFunction(() => new URL(location.href).searchParams.get("q") === "speed");
      await settle();
      ck(`${tag("search")}: typing waits, then the address and the list follow`, (await rows.count()) === 2 && (await texts(page.getByTestId("ticket-ref"))).sort().join(",") === "T13,T16"
         && /^2 tickets match\./.test(await text(page.getByTestId("tickets-count-line"))), await text(page.getByTestId("tickets-count-line")));
      ck(`${tag("search")}: and the box keeps what was typed`, (await page.getByTestId("f-q").inputValue()) === "speed");
      await page.getByTestId("f-q").fill("zzzz");
      await page.waitForFunction(() => document.querySelector('[data-testid="tickets-zero"]'));
      ck(`${tag("search")}: nothing matches: a box that names the search, with Clear filters`, /No ticket matches "zzzz"/.test(await text(page.getByTestId("tickets-zero")))
         && (await page.getByTestId("zero-clear").count()) === 1);
      await shot(page, "live-zero", width);
      await page.getByTestId("zero-clear").click();
      await page.waitForFunction(() => !document.querySelector('[data-testid="tickets-zero"]'));
      ck(`${tag("search")}: Clear filters empties the box and the address`, (await page.getByTestId("f-q").inputValue()) === "" && query(page).q === undefined, JSON.stringify(query(page)));

      // milestone
      await openLive(`?layout=list`);
      await pick(page, "f-project", X);
      await settle();
      const msOptions = await opts(page, "f-milestone");
      ck(`${tag("milestone")}: choosing a project opens Milestone with M1, M2, M3`, msOptions.map((o) => o.t).join("|") === "All milestones|M1 1. Foundations|M2 Speed|M3 Launch", msOptions.map((o) => o.t).join("|"));
      await pick(page, "f-milestone", msOptions[3].v);
      await settle();
      ck(`${tag("milestone")}: a milestone narrows the list`, query(page).milestone === msOptions[3].v && /^\d+ tickets match/.test(await text(page.getByTestId("tickets-count-line"))));
      const n3 = await rows.count();
      ck(`${tag("milestone")}: only that milestone's tickets`, n3 > 0 && (await texts(page.getByTestId("ticket-row").locator("[data-user-text]"))).some((t) => t.includes("M3 Launch")));
      await pick(page, "f-project", Y);
      await settle();
      ck(`${tag("milestone")}: another project clears the milestone`, query(page).milestone === undefined && query(page).project === Y);
      await pick(page, "f-project", "");
      await settle();
      ck(`${tag("milestone")}: no project: Milestone waits again`, await page.getByTestId("f-milestone").isDisabled());

      // markup
      await openLive(`?layout=list&q=bold`);
      ck(`${tag("markup")}: a title with tags shows them as characters, nothing runs`, (await texts(page.getByTestId("ticket-title"))).some((t) => t.includes('<b>bold</b> <img src=x onerror="window.__pwned=1">'))
         && (await page.locator("main img, main b, main u, main i:not([aria-hidden])").count()) === 0 && (await page.evaluate(() => window.__pwned)) === undefined);
      ck(`${tag("markup")}: a project name with tags shows as text`, (await texts(page.getByTestId("ticket-row").locator("span[data-user-text]"))).includes("project-<i>m</i>"));

      // swimlanes
      await openLive(`?layout=lanes`);
      await shot(page, "live-lanes", width);
      await checkWords(page, ck, tag("lanes"));
      ck(`${tag("lanes")}: no horizontal scroll`, (await overflow(page)) <= 0 && (await inside(page)), `${await overflow(page)}px over`);
      const lanes = page.getByTestId("lane");
      const laneNames = await lanes.evaluateAll((l) => l.map((x) => x.dataset.lane));
      if (!narrow) {
        ck(`${tag("lanes")}: a lane per project that has tickets, an empty project is not drawn`, laneNames.length === 3 && laneNames.includes("project-x")
           && laneNames.includes("project-y") && !laneNames.includes("project-w"), laneNames.join("|"));
      }
      ck(`${tag("lanes")}: the Group by menu stands where Sort stood`, (await page.getByTestId("f-group").count()) === 1 && (await page.getByTestId("f-sort").count()) === 0);
      if (!narrow) {
        const lx = page.locator('[data-testid="lane"][data-lane="project-x"]');
        ck(`${tag("lanes")}: the lane names its tickets and how many move`, (await text(lx.getByTestId("lane-sub"))) === "30 tickets, 3 moving", await text(lx.getByTestId("lane-sub")));
        ck(`${tag("lanes")}: 5 cards of the 14 to do in a cell`, (await lx.locator('[data-cell$="to_do"] [data-testid="ticket-card"]').count()) === 5
           && (await text(lx.locator('[data-cell$="to_do"] [data-testid="cell-showing"]'))) === "Showing 5 of 14");
        await lx.locator('[data-cell$="to_do"] [data-testid="cell-more"]').click();
        ck(`${tag("lanes")}: Show 5 more adds five`, (await lx.locator('[data-cell$="to_do"] [data-testid="ticket-card"]').count()) === 10);
        ck(`${tag("lanes")}: the column heads stay on top while the lanes scroll`, /sticky/.test(await page.getByTestId("lane-heads").getAttribute("class")));
        await lx.getByTestId("lane-toggle").click();
        ck(`${tag("lanes")}: a lane folds`, (await lx.getByTestId("ticket-card").count()) === 0 && (await lx.getByTestId("lane-toggle").getAttribute("aria-expanded")) === "false");
        await lx.getByTestId("lane-toggle").click();
        ck(`${tag("lanes")}: and opens again`, (await lx.getByTestId("ticket-card").count()) > 0);
      } else {
        ck(`${tag("lanes")}: the state tabs, the lanes of the chosen state`, (await page.getByTestId("state-tab").count()) === 6 && (await tab("in_progress").getAttribute("aria-selected")) === "true");
        ck(`${tag("lanes")}: In progress has a lane in project-x and one in project-y`, (await lanes.count()) === 2 && (await cards.count()) === 2);
        await lanes.first().getByTestId("lane-toggle").click();
        ck(`${tag("lanes")}: a lane folds`, (await lanes.first().getByTestId("ticket-card").count()) === 0);
        await tab("to_do").click();
        ck(`${tag("lanes")}: To do shows a lane per project with to do tickets, the cap is 8`, (await lanes.count()) === 3
           && (await page.locator('[data-testid="lane"][data-lane="project-x"]').getByTestId("ticket-card").count()) === 8
           && (await text(page.locator('[data-testid="lane"][data-lane="project-x"]').getByTestId("cell-showing"))) === "Showing 8 of 14");
        await shot(page, "live-lanes-todo", width);
        ck(`${tag("lanes")}: no horizontal scroll on a tab`, (await overflow(page)) <= 0 && (await inside(page)));
      }

      // grouping
      await pick(page, "f-group", "milestone");
      await settle();
      const byMs = await page.getByTestId("lane").evaluateAll((l) => l.map((x) => x.dataset.lane));
      ck(`${tag("group")}: by milestone, in order, project by project${narrow ? " (the lanes of To do)" : ""}`, query(page).group === "milestone"
         && (narrow || byMs.indexOf("project-x, M1 1. Foundations") >= 0 && byMs.indexOf("project-x, M1 1. Foundations") < byMs.indexOf("project-x, M2 Speed"))
         && byMs.indexOf("project-x, M2 Speed") < byMs.indexOf("project-x, M3 Launch") && byMs.includes("project-y, M1 Faster lists")
         && byMs.includes("project-<i>m</i>, no milestone"), byMs.join("|"));
      await shot(page, "live-lanes-milestone", width);
      await pick(page, "f-group", "who");
      await settle();
      const byWho = await page.getByTestId("lane").evaluateAll((l) => l.map((x) => x.dataset.lane));
      ck(`${tag("group")}: by who: ${narrow ? "the To do tickets wait for no one yet" : "Builder for M1, Builder for M2, Reviewer, Lead, Not picked up yet, then the finished"}`, query(page).group === "who"
         && byWho.join("|") === (narrow ? "Not picked up yet" : "Builder for M1|Builder for M2|Reviewer|Lead|Not picked up yet|Finished"), byWho.join("|"));
      await checkWords(page, ck, tag("lanes by who"));
      await shot(page, "live-lanes-who", width);
      await pick(page, "f-group", "project");
      await settle();
      ck(`${tag("group")}: by project is the default and leaves the address`, query(page).group === undefined);

      // a card is one link
      await openLive(`?layout=board&state=in-review`);
      const card = cards.first();
      ck(`${tag("card")}: a card is one link to its ticket`, (await card.getAttribute("href")) === `/projects/${X}/tickets/T14` && (await card.evaluate((e) => e.tagName)) === "A");
      ck(`${tag("card")}: ref, title and who`, /T14/.test(await text(card)) && /Cache the counts/.test(await text(card)) && /Reviewer/.test(await text(card)), await text(card));

      // error and retry: the first read fails, Retry reads again
      {
        let fail1 = true;
        await page.route(`**${ROUTE}**`, (route) => {
          if (fail1) { fail1 = false; return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "no" }) }); }
          return route.continue();
        });
        await page.goto(`${BASE}/tickets`, { waitUntil: "load" });
        await page.getByTestId("tickets-retry").waitFor();
        ck(`${tag("error")}: a banner and Retry when the cell does not answer`, /could not load the tickets/i.test(await page.getByRole("alert").innerText()));
        await checkWords(page, ck, tag("error"));
        await shot(page, "live-error", width);
        await page.getByTestId("tickets-retry").click();
        await page.getByTestId("tickets-count").waitFor();
        ck(`${tag("error")}: Retry reads again and the tickets show`, (await page.getByTestId("tickets-retry").count()) === 0 && (await page.getByTestId("tickets-sub").count()) === 1);
        await page.unroute(`**${ROUTE}**`);
      }

      // a board read that stops short of a column: the board asks for open tickets first, and a
      // column that counts more than came back points to the List instead of a Show more that cannot
      {
        let asked = "";
        await page.route(`**${ROUTE}**`, async (route) => {
          asked = route.request().url();
          const body = await (await route.fetch()).json();
          body.tickets = body.tickets.filter((t) => t.state_key !== "to_do");
          body.more = true;
          return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
        });
        await page.goto(`${BASE}/tickets?layout=board&q=`, { waitUntil: "load" });
        await page.getByTestId("tickets-capped").waitFor();
        ck(`${tag("short read")}: the board asks for open tickets first`, new URL(asked).searchParams.get("sort") === "open", asked);
        if (narrow) await tab("to_do").click();
        const cell = narrow ? page.locator('[data-cell="b|to_do"]') : col("to_do");
        const link = cell.getByTestId("cell-list");
        ck(`${tag("short read")}: nothing loaded, so no Show more`, (await cell.getByTestId("cell-more").count()) === 0);
        ck(`${tag("short read")}: a link to the List for that state`, (await text(link)) === "See all 16 in the List"
           && new URL(await link.getAttribute("href"), BASE).searchParams.get("state") === "to-do"
           && new URL(await link.getAttribute("href"), BASE).searchParams.get("layout") === "list", await link.getAttribute("href"));
        await page.unroute(`**${ROUTE}**`);
        await link.click();
        await page.getByTestId("tickets-count").waitFor();
        ck(`${tag("short read")}: it opens the List on To do`, query(page).layout === "list" && query(page).state === "to-do", page.url());
      }

      // the menu entry
      await openLive();
      if (!narrow) {
        ck(`${tag("menu")}: Tickets is in the menu, after Projects and before Reviews`, (await page.getByTestId("menu-tickets").getAttribute("href")) === "/tickets"
           && (await page.evaluate(() => {
             const k = [...document.querySelectorAll('[data-testid^="menu-"]')].map((e) => e.getAttribute("data-testid"));
             return k.indexOf("menu-tickets") === k.indexOf("menu-projects") + 1 && k.indexOf("menu-reviews") === k.indexOf("menu-tickets") + 1;
           })));
      }
      await ctx.close();
    }

    // ---- an address that is not a sample, without the flag
    {
      const { ctx, page } = await fresh(1440, false);
      await page.goto(`${BASE}/tickets?sample=empty`, { waitUntil: "load" });
      await page.getByTestId("tickets-count").waitFor();
      ck("no flag: ?sample=empty shows the live tickets", (await page.getByTestId("tickets-empty").count()) === 0);
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
