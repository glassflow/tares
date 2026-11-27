/*
 * Browser e2e for TR-1667 (the factory Report page's frame), TR-1668 (its Delivered section),
 * TR-1669 (its Time section), TR-1670 (its Review section) and TR-1672 (its Merges section).
 *
 * A scratch factory cell seeded by tests/seed-factory-report.py (8 weeks hold 9 merged tickets, 4 weeks
 * 5). Checked: the menu entry; the page from one read of GET /api/factory/report; the six section
 * headings, the "cannot see" line and the "API-equivalent" label; each range and each kind of scope
 * changing the address and the figures; Merges' ledger, weekly counts by level, current levels, CSV
 * download and empty state; a copied address opening the same view; two dates; a range with
 * no merged ticket; Delivered's four tiles, their change on the weeks before, the lines under them and
 * the two weekly charts, also with reviews and no merge and with nothing delivered; Time's stage table,
 * its two weekly charts, the start-to-merge histogram and "stuck now" with its links to the question
 * (inside "Waiting for others" on the project page) and to the ticket, also empty; Review's two tiles
 * and two weekly charts, its findings table sorted by each column, its projects with the weeks before,
 * and its recurring findings with the accepted rule marked, also empty and before the first pass; a
 * scope or a date the cell refuses; a read that fails; at 1440 and 390 wide, and embedded in the desktop
 * (?embed=desktop).
 * Run:  npm run build && TARES_E2E_PORT=<free port> node tests/report.e2e.cjs
 * (tares installed in ../.venv or on PATH). Set SHOTS_DIR to keep screenshots. TARES_E2E_PORT
 * (default 8883), MCP port +1. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require("./browser.cjs");
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8883);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROUTE = "/api/factory/report";
const SECTIONS = ["delivered:Delivered", "time:Time", "review:Review", "cost:Cost", "merges:Merges", "architecture:Architecture"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const waited = (n) => `Waiting on a person: ${n}% of stage time.`;
const CANNOT_SEE = "What this report cannot see: Work done outside Tares. Pushes that skipped the merge queue. "
  + "Your bill: cost is API-equivalent (list prices), not what a subscription charges.";

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

/** A UTC day as the address writes it, and as the page says it. */
const iso = (ms) => new Date(ms).toISOString().slice(0, 10);
const said = (day) => { const d = new Date(`${day}T00:00:00Z`); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`; };
const plus = (day, days) => iso(Date.parse(`${day}T00:00:00Z`) + days * 86_400_000);

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-report-e2e-"));
  const base = { PYTHONPATH: REPO, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                 TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", TARES_EDITION: "factory" };
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-report.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8", env: cleanEnv(base) }).trim().split("\n").pop());
  ck("seeded 3 projects and 2 leads", ["project-a", "project-b", "project-c", "Ada", "Bo"].every((k) => ids[k]), JSON.stringify(ids));

  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore",
    env: cleanEnv(base), detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const errors = [];
    const fresh = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      const page = await ctx.newPage();
      page.on("pageerror", (e) => errors.push(e.message));
      const asked = [];
      page.on("request", (r) => { const u = new URL(r.url()); if (u.pathname === ROUTE) asked.push(u.searchParams); });
      return { ctx, page, asked };
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    // what a chart's own sideways scroll box holds may reach past the window; the box may not
    const inside = (page) => page.evaluate(() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1
        || !!e.parentElement.closest('[data-week-chart] [role="region"], [data-testid^="merges-"] [role="region"]'));
    });
    const shot = async (page, name) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `report-${name}.png`), fullPage: true });
    };
    const text = async (page, id) => (await page.getByTestId(id).innerText()).replace(/\s+/g, " ").trim();
    const count = (page, id) => page.getByTestId(id).count();
    const attr = (page, a) => page.evaluate((n) => document.documentElement.getAttribute(n), a);
    const query = (page) => Object.fromEntries(new URL(page.url()).searchParams);
    const same = (a, b) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());
    const sections = (page) => page.locator('[data-testid^="report-section-"]')
      .evaluateAll((els) => els.map((e) => `${e.dataset.testid.replace("report-section-", "")}:${e.querySelector("h2")?.textContent.trim()}`));
    const pick = async (page, id, name) => {
      await page.getByTestId(id).click();
      await page.getByRole("option", { name, exact: true }).click();
    };
    /** The summary line, once it holds `part`; what it holds after 5 seconds when it never does. */
    const summary = async (page, part) => {
      const found = page.getByTestId("report-summary").filter({ hasText: part });
      try { await found.waitFor({ timeout: 5000 }); } catch { /* the check says what is there */ }
      return (await count(page, "report-summary")) ? text(page, "report-summary") : "(no summary)";
    };
    const says = async (page, label, start) => {
      const got = await summary(page, start);
      ck(label, got.startsWith(start), got);
    };
    /** The address of the one week `back` weeks before this one. */
    const days = (back) => `/report?range=dates&from=${plus(ids.monday, -7 * back)}&to=${plus(ids.monday, -7 * back + 6)}`;
    const open = async (page, to) => {
      await page.goto(`${BASE}${to}`, { waitUntil: "load" });
      await page.getByTestId("factory-report").waitFor();
      await page.waitForLoadState("networkidle");
    };

    // ---- from the menu, and the frame
    {
      const { ctx, page } = await fresh(1440);
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("menu-report").click();
      await page.getByTestId("report-read").waitFor();
      ck("menu: the Report entry opens /report", new URL(page.url()).pathname === "/report"
         && (await text(page, "menu-report")) === "Report", page.url());
      await ctx.close();
    }
    {
      const { ctx, page, asked } = await fresh(1440);
      const oneCall = expectOneCall(page, ROUTE);
      await open(page, "/report");
      await page.getByTestId("report-read").waitFor();
      await oneCall(ck, "frame");
      ck("frame: the title is Report", (await page.locator("h1").first().innerText()).trim() === "Report");
      ck("frame: 8 weeks of the whole factory by default, with a bare address",
         (await text(page, "report-range")) === "Last 8 weeks" && (await text(page, "report-scope")) === "The whole factory"
         && new URL(page.url()).search === "" && asked[0].get("scope") === "factory" && !asked[0].has("from") && !asked[0].has("to"),
         `${page.url()} ${asked[0]}`);
      const first = plus(ids.monday, -49);
      await says(page, "frame: the summary counts the merged tickets and says which weeks",
                 `9 tickets merged. ${waited(14)} ${said(first)} to ${said(plus(ids.monday, 6))}, in weeks that start on Monday (UTC). This week is not over yet.`);
      ck("frame: what the report cannot see, in full", (await text(page, "report-cannot-see")) === CANNOT_SEE,
         await text(page, "report-cannot-see"));
      ck("frame: six section headings in order", (await sections(page)).join("|") === SECTIONS.join("|"), (await sections(page)).join("|"));
      ck("frame: no empty line while tickets were merged", (await count(page, "report-empty")) === 0);
      ck("frame: each section but Delivered, Time, Review, Cost and Merges says it is not built yet",
         (await page.locator('[data-testid^="report-section-"]').filter({ hasText: "This section is not built yet." }).count()) === 1
         && !(await text(page, "report-section-delivered")).includes("not built")
         && !(await text(page, "report-section-time")).includes("not built")
         && !(await text(page, "report-section-review")).includes("not built")
         && !(await text(page, "report-section-cost")).includes("not built")
         && !(await text(page, "report-section-merges")).includes("not built"));
      ck("frame: the Cost section says what its amounts are",
         (await text(page, "cost-label")) === "API-equivalent cost: list prices from tares/pricing.py. A subscription crew pays a flat fee.",
         await text(page, "cost-label"));
      ck("frame: no date fields until two dates are asked for", (await count(page, "report-from")) === 0);
      await checkWords(page, ck, "frame @1440");
      ck("frame @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, "frame-1440");

      // ---- range and scope live in the address
      await pick(page, "report-range", "Last 4 weeks");
      await says(page, "range: 4 weeks count 5 merged tickets", `5 tickets merged. ${waited(16)} ${said(plus(ids.monday, -21))}`);
      ck("range: the address says 4 weeks and the read starts three Mondays back or later",
         same(query(page), { range: "4w" }) && asked.at(-1).get("from") >= plus(ids.monday, -21), `${page.url()} ${asked.at(-1)}`);

      await pick(page, "report-scope", "Project: project-b");
      await says(page, "scope: one project counts its own", "2 tickets merged.");
      ck("scope: the address names the project and keeps the range",
         same(query(page), { range: "4w", scope: `project:${ids["project-b"]}` }) && asked.at(-1).get("scope") === `project:${ids["project-b"]}`, page.url());

      const copied = page.url();
      const seen = await text(page, "report-read");
      {
        const other = await fresh(1440);
        await open(other.page, copied.slice(BASE.length));
        await says(other.page, "copied address: the same figures", "2 tickets merged.");
        ck("copied address: the same range, scope and page",
           (await text(other.page, "report-range")) === "Last 4 weeks" && (await text(other.page, "report-scope")) === "Project: project-b"
           && (await text(other.page, "report-read")) === seen, await text(other.page, "report-read"));
        await other.ctx.close();
      }

      await page.goBack();
      await says(page, "back: the view before", "5 tickets merged.");
      ck("back: the address and the pickers follow", same(query(page), { range: "4w" })
         && (await text(page, "report-scope")) === "The whole factory", page.url());

      await pick(page, "report-scope", "Team led by Ada");
      await says(page, "scope: a team counts what its lead's projects merged", "5 tickets merged.");
      ck("scope: the address names the team", same(query(page), { range: "4w", scope: `team:${ids.Ada}` })
         && asked.at(-1).get("scope") === `team:${ids.Ada}`, page.url());

      await pick(page, "report-scope", "My projects");
      await says(page, "scope: my projects on an open cell are all of them", "5 tickets merged.");
      ck("scope: the address says mine", query(page).scope === "mine" && asked.at(-1).get("scope") === "mine", page.url());
      await pick(page, "report-scope", "Team led by Ada");
      await says(page, "scope: back to the team", "5 tickets merged.");

      const now = new Date();
      const quarter = Date.UTC(now.getUTCFullYear(), Math.floor(now.getUTCMonth() / 3) * 3, 1);
      const quarterMonday = iso(quarter - ((new Date(quarter).getUTCDay() + 6) % 7) * 86_400_000);
      await pick(page, "report-range", "This quarter");
      const got = await summary(page, `${said(quarterMonday)} to `);
      ck("range: this quarter starts in the week of its first day and runs to this week",
         got.includes(`${said(quarterMonday)} to ${said(plus(ids.monday, 6))}`) && query(page).range === "quarter"
         && asked.at(-1).get("from") === iso(quarter), `${got} ${asked.at(-1)}`);

      // ---- two dates
      await pick(page, "report-range", "Last 4 weeks");
      await says(page, "dates: starting from 4 weeks", "5 tickets merged.");
      await pick(page, "report-range", "Two dates");
      await page.getByTestId("report-from").waitFor();
      const start = query(page);
      ck("dates: the two fields start from the weeks in view and end today at the latest",
         start.range === "dates" && start.from === plus(ids.monday, -21) && start.to <= iso(Date.now()) && start.to >= ids.monday
         && (await page.getByTestId("report-from").inputValue()) === start.from
         && (await page.getByTestId("report-to").inputValue()) === start.to, JSON.stringify(start));
      await page.getByTestId("report-from").fill(ids.old_monday);
      await says(page, "dates: an earlier first date counts the old ticket too", `10 tickets merged. ${waited(13)} ${said(ids.old_monday)}`);
      ck("dates: the address holds the new date", query(page).from === ids.old_monday, page.url());
      await page.getByTestId("report-to").fill(plus(ids.old_monday, 2));
      await says(page, "dates: one week long, one ticket, said in the singular",
                 `1 ticket merged. ${waited(0)} ${said(ids.old_monday)} to ${said(plus(ids.old_monday, 6))}, in weeks that start on Monday (UTC).`);
      ck("dates: a past range does not say this week is running",
         !(await text(page, "report-summary")).includes("not over"), await text(page, "report-summary"));
      {
        const other = await fresh(1440);
        await open(other.page, page.url().slice(BASE.length));
        await says(other.page, "copied dates: the same figures", "1 ticket merged.");
        ck("copied dates: the same two dates in the fields",
           (await other.page.getByTestId("report-from").inputValue()) === ids.old_monday
           && (await other.page.getByTestId("report-to").inputValue()) === plus(ids.old_monday, 2)
           && (await text(other.page, "report-range")) === "Two dates");
        await other.ctx.close();
      }
      {
        const to = page.getByTestId("report-to");
        const away = () => page.getByRole("heading", { name: "Report", exact: true }).click();
        await to.focus();
        await page.keyboard.press("Delete");
        await away();
        await page.waitForTimeout(300);
        ck("dates: a half-typed date changes nothing and the field comes back",
           query(page).to === plus(ids.old_monday, 2) && (await to.inputValue()) === plus(ids.old_monday, 2),
           `${page.url()} ${await to.inputValue()}`);
        await to.focus();
        for (let part = 0; part < 3; part++) {
          await page.keyboard.press("Delete");
          await page.keyboard.press("ArrowRight");
        }
        await away();
        await says(page, "dates: a last date cleared by hand means today again", `10 tickets merged. ${waited(13)} ${said(ids.old_monday)}`);
        ck("dates: and it leaves the address", !("to" in query(page)) && query(page).from === ids.old_monday, page.url());
        await page.getByTestId("report-from").fill("");
        await says(page, "dates: an emptied first date means the usual weeks", `9 tickets merged. ${waited(14)} ${said(plus(ids.monday, -49))} to `);
        ck("dates: and it leaves the address too", same(query(page), { range: "dates", scope: `team:${ids.Ada}` }), page.url());
      }
      await pick(page, "report-range", "Last 8 weeks");
      await says(page, "range: a preset again, 8 weeks of Ada's team", "9 tickets merged.");
      ck("range: a preset drops the two dates from the address",
         same(query(page), { range: "8w", scope: `team:${ids.Ada}` }) && (await count(page, "report-from")) === 0, page.url());

      // ---- nothing merged
      await pick(page, "report-scope", "Team led by Bo");
      await page.getByTestId("report-empty").waitFor();
      ck("empty: a scope with no merged ticket says so",
         (await text(page, "report-empty")).includes("No ticket was merged in these weeks")
         && !(await text(page, "report-summary")).includes("merged.")
         && !(await text(page, "report-summary")).includes("Waiting on a person"), await text(page, "report-summary"));
      ck("empty: the six sections stay under that line", (await sections(page)).join("|") === SECTIONS.join("|")
         && await page.evaluate(() => {
           const line = document.querySelector('[data-testid="report-empty"]');
           const first = document.querySelector('[data-testid^="report-section-"]');
           return !!(line && first && line.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING);
         }), (await sections(page)).join("|"));
      ck("empty: the dates and what it cannot see stay", (await text(page, "report-summary")).includes("in weeks that start on Monday (UTC).")
         && (await text(page, "report-cannot-see")) === CANNOT_SEE);
      await checkWords(page, ck, "empty @1440");
      await shot(page, "empty-1440");
      await ctx.close();
    }

    // ---- addresses the cell refuses, and ones it reads past
    {
      const { ctx, page } = await fresh(1440);
      await open(page, "/report?scope=project:uc_nope&range=4w");
      await page.getByTestId("report-refused").waitFor();
      ck("refused: an unknown project says so and shows no figure",
         (await text(page, "report-refused")).includes("The report did not load. That project or team does not exist, or you cannot see it.")
         && (await count(page, "report-read")) === 0 && (await text(page, "report-scope")) === "Pick a scope",
         await text(page, "report-refused"));
      await checkWords(page, ck, "refused @1440");
      await shot(page, "refused-1440");
      await page.getByTestId("report-refused").getByRole("button", { name: "Start over" }).click();
      await says(page, "refused: Start over opens the default view", "9 tickets merged.");
      ck("refused: with a bare address", new URL(page.url()).search === "", page.url());

      await open(page, "/report?range=dates&from=2026-13-01");
      await page.getByTestId("report-refused").waitFor();
      ck("refused: a date that is none is said in the cell's words",
         (await text(page, "report-refused")).includes("A date is written as year-month-day, for example 2026-11-25."),
         await text(page, "report-refused"));
      await checkWords(page, ck, "bad date @1440");

      await open(page, "/report?range=forever");
      await says(page, "an unknown range reads the default 8 weeks", "9 tickets merged.");
      ck("an unknown range shows as the default", (await text(page, "report-range")) === "Last 8 weeks", await text(page, "report-range"));
      await ctx.close();
    }

    // ---- a read that fails, and "mine" with no project
    {
      const { ctx, page } = await fresh(1440);
      const failing = /\/api\/factory\/report\?/;
      await page.route(failing, (r) => r.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
      await open(page, "/report");
      await page.getByTestId("report-error").waitFor();
      ck("failed read: says so with Retry", (await text(page, "report-error")).includes("The report did not load. The cell did not answer. Try again in a moment.")
         && (await count(page, "report-read")) === 0, await text(page, "report-error"));
      await checkWords(page, ck, "failed read @1440");
      await page.unroute(failing);
      await page.getByTestId("report-error").getByRole("button", { name: "Retry" }).click();
      await says(page, "failed read: Retry reads again", "9 tickets merged.");

      // the open cell's "mine" is every project, so the answer for someone with none is made here
      await page.route(failing, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        body.scope = { ...body.scope, value: "mine", count: 0 };
        body.range = { ...body.range, merged: 0 };
        await r.fulfill({ response: res, json: body });
      });
      await open(page, "/report?scope=mine");
      await page.getByTestId("report-none-mine").waitFor();
      ck("mine with no project: says what mine means, with no section",
         (await text(page, "report-none-mine")).includes("No project here is yours")
         && (await text(page, "report-none-mine")).includes("Mine means the projects you lead or hold a ticket in. Pick the whole factory to see everything.")
         && (await sections(page)).length === 0 && (await count(page, "report-empty")) === 0, await text(page, "report-none-mine"));
      await checkWords(page, ck, "mine with no project @1440");
      await ctx.close();
    }

    // ---- Delivered: the four tiles and the two charts
    {
      const { ctx, page, asked } = await fresh(1440);
      const TILES = ["merged", "milestones", "projects", "start-to-merge"];
      /** Each tile's lines as read: the label (shown in capitals), the value, then what is under it;
       * nothing for a tile that is not there. */
      const lines = async (id) =>
        ((await count(page, `delivered-${id}`)) ? await page.getByTestId(`delivered-${id}`).innerText() : "")
          .split("\n").map((l) => l.trim()).filter(Boolean);
      const tiles = () => Promise.all(TILES.map(lines));
      const is = async (label, ...want) => {
        const got = await tiles();
        ck(label, JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got));
      };
      /** A chart's value for each week, oldest first; "-" for a week with none. */
      const weekly = (id) => page.getByTestId(`delivered-${id}-weeks`).locator("tr[data-series] td")
        .evaluateAll((cells) => cells.map((c) => (c.querySelector(".sr-only") ? "-" : c.textContent.replace(/\s+/g, " ").trim())));
      const marks = (id, mark) => page.getByTestId(`delivered-${id}-weeks`).locator(`[data-plot] ${mark}`).count();
      const view = async (to) => {
        await open(page, to);
        await page.getByTestId("report-section-delivered").waitFor();
      };
      await view("/report");
      await is("delivered: 8 weeks, with nothing in the 8 weeks before",
        ["MERGED", "9", "No figures for the 8 weeks before", "67% passed first review, of 3 tickets first reviewed"],
        ["MILESTONES ACCEPTED", "3", "No figures for the 8 weeks before"],
        ["PROJECTS WITH A MERGE", "2", "No figures for the 8 weeks before"],
        ["START TO MERGE, P50", "1 hour 30 minutes", "p90 10 hours 36 minutes", "from 8 of 9 merged tickets",
         "No figures for the 8 weeks before", "1.3 review rounds per ticket reviewed"]);
      ck("delivered: a change line on each of the four tiles and nowhere else",
         (await page.getByTestId("report-section-delivered").locator("[data-change]").count()) === 4
         && (await page.locator("[data-change]").count()) === 4);
      ck("delivered: the four tiles sit in one row", new Set(await Promise.all(TILES.map(async (id) =>
        Math.round((await page.getByTestId(`delivered-${id}`).boundingBox()).y)))).size === 1);
      ck("delivered: merged per week, 0 in a week without a merge",
         (await weekly("merged")).join("|") === "0|4|0|0|0|2|3|0" && (await marks("merged", "[data-bar]")) === 3,
         `${await weekly("merged")} ${await marks("merged", "[data-bar]")}`);
      ck("delivered: start to merge per week, no point in a week without a merge",
         (await weekly("p50")).join("|") === "-|1 hour|-|-|-|1 day 2 hours|3 hours|-" && (await marks("p50", "[data-dot]")) === 3,
         `${await weekly("p50")} ${await marks("p50", "[data-dot]")}`);
      const charts = await page.getByTestId("report-section-delivered").locator("[data-week-chart]")
        .evaluateAll((els) => els.map((e) => `${e.dataset.weekChart}:${e.querySelector("figcaption").textContent}`));
      ck("delivered: bars for merged, a line for start to merge",
         charts.join("|") === "bars:Merged per week|lines:Start to merge, p50 per week", charts.join("|"));
      ck("delivered: the running week's column says so far in both charts",
         (await page.getByTestId("report-section-delivered").locator(`th[data-week="${ids.monday}"]`).filter({ hasText: "so far" }).count()) === 2);
      ck("delivered: the whole page still comes from one read", asked.length === 1, `${asked.length} reads`);
      await shot(page, "delivered-8w-1440");

      await view("/report?range=4w");
      await is("delivered: 4 weeks against the 4 weeks before",
        ["MERGED", "5", "+1 on the 4 weeks before", "67% passed first review, of 3 tickets first reviewed"],
        ["MILESTONES ACCEPTED", "1", "-1 on the 4 weeks before"],
        ["PROJECTS WITH A MERGE", "2", "+1 on the 4 weeks before"],
        ["START TO MERGE, P50", "3 hours 30 minutes", "p90 19 hours 24 minutes", "from 4 of 5 merged tickets",
         "+2 hours 30 minutes on the 4 weeks before", "1.3 review rounds per ticket reviewed"]);
      ck("delivered: the running week is said to be unfinished once, in the summary",
         (await text(page, "report-summary")).endsWith("This week is not over yet.")
         && (await page.getByText("This week is not over yet.").count()) === 1,
         `${await page.getByText("This week is not over yet.").count()} times`);
      await checkWords(page, ck, "delivered @1440");
      await shot(page, "delivered-4w-1440");

      await view(`/report?range=4w&scope=project:${ids["project-b"]}`);
      await is("delivered: one project's own figures, the same count, and one of each in the singular",
        ["MERGED", "2", "-2 on the 4 weeks before", "100% passed first review, of 1 ticket first reviewed"],
        ["MILESTONES ACCEPTED", "0", "-1 on the 4 weeks before"],
        ["PROJECTS WITH A MERGE", "1", "Same as the 4 weeks before"],
        ["START TO MERGE, P50", "1 day 2 hours", "p90 1 day 2 hours", "from 1 of 2 merged tickets",
         "+1 day 1 hour on the 4 weeks before", "1 review round per ticket reviewed"]);

      await view(`/report?range=4w&scope=project:${ids["project-a"]}`);
      await is("delivered: every ticket with a start, and earlier weeks with no merge",
        ["MERGED", "3", "+3 on the 4 weeks before", "50% passed first review, of 2 tickets first reviewed"],
        ["MILESTONES ACCEPTED", "1", "Same as the 4 weeks before"],
        ["PROJECTS WITH A MERGE", "1", "+1 on the 4 weeks before"],
        ["START TO MERGE, P50", "3 hours", "p90 3 hours 48 minutes", "No figures for the 4 weeks before",
         "1.5 review rounds per ticket reviewed"]);

      await view(days(1));
      ck("delivered: one week against the one before it",
         (await tiles())[0][2] === "+1 on the week before", JSON.stringify((await tiles())[0]));
      ck("delivered: a past range says nothing about this week",
         (await page.getByText("This week is not over yet.").count()) === 0 && !(await text(page, "report-section-delivered")).includes("so far"));

      await view(days(30));
      await is("delivered: reviews and no merge read 0 and no value",
        ["MERGED", "0", "No figures for the week before", "0% passed first review, of 1 ticket first reviewed"],
        ["MILESTONES ACCEPTED", "0", "No figures for the week before"],
        ["PROJECTS WITH A MERGE", "0", "No figures for the week before"],
        ["START TO MERGE, P50", "no value", "No figures for the week before", "1 review round per ticket reviewed"]);
      ck("delivered: and the charts hold a 0 and no point",
         (await weekly("merged")).join("|") === "0" && (await marks("merged", "[data-bar]")) === 0
         && (await marks("p50", "[data-dot]")) === 0);
      await checkWords(page, ck, "delivered with no merge @1440");
      await shot(page, "delivered-no-merge-1440");

      await view(`/report?scope=team:${ids.Bo}`);
      ck("delivered: an empty range says nothing was delivered, with no tile and no chart",
         (await count(page, "delivered-empty")) === 1 && (await text(page, "delivered-empty")) === "Nothing was delivered in these weeks."
         && (await count(page, "delivered-merged")) === 0 && (await page.locator("[data-week-chart]").count()) === 0,
         await text(page, "report-section-delivered"));

      await view(`/report?range=dates&from=${plus(ids.monday, -7 * 4)}&to=${plus(ids.monday, -7 * 3 + 6)}`);
      await is("delivered: nothing in these weeks after merges in the weeks before reads as the drop",
        ["MERGED", "0", "-4 on the 2 weeks before"],
        ["MILESTONES ACCEPTED", "0", "-2 on the 2 weeks before"],
        ["PROJECTS WITH A MERGE", "0", "-1 on the 2 weeks before"],
        ["START TO MERGE, P50", "no value"]);
      ck("delivered: and the drop keeps its charts, with no sentence",
         (await count(page, "delivered-empty")) === 0 && (await weekly("merged")).join("|") === "0|0"
         && (await marks("p50", "[data-dot]")) === 0, await text(page, "report-section-delivered"));
      await checkWords(page, ck, "delivered after a drop to nothing @1440");
      await shot(page, "delivered-drop-1440");

      // earlier medians the seed has none of: a longer one, and one seconds away; then no merge now
      const reading = /\/api\/factory\/report\?/;
      await page.route(reading, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        body.sections.delivered.previous = { merged: 0, milestones_accepted: 0, projects_merged: 0,
          start_to_merge: { p50: null, p90: null, tickets: 0 }, first_pass: { passed: 0, reviewed: 0 }, rounds_per_ticket: null };
        await r.fulfill({ response: res, json: body });
      });
      await view(`/report?scope=team:${ids.Bo}`);
      ck("delivered: earlier figures with nothing delivered still read as nothing delivered",
         (await count(page, "delivered-empty")) === 1 && (await count(page, "delivered-merged")) === 0,
         await text(page, "report-section-delivered"));
      await page.unroute(reading);
      // an accepted milestone is a delivery: with no merge and no review the figures still show
      await page.route(reading, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        body.sections.delivered.milestones_accepted = 1;
        body.sections.delivered.previous = { merged: 0, milestones_accepted: 0, projects_merged: 0,
          start_to_merge: { p50: null, p90: null, tickets: 0 }, first_pass: { passed: 0, reviewed: 0 }, rounds_per_ticket: null };
        await r.fulfill({ response: res, json: body });
      });
      await view(`/report?scope=team:${ids.Bo}`);
      await is("delivered: an accepted milestone and nothing else shows the figures",
        ["MERGED", "0", "Same as the 8 weeks before"],
        ["MILESTONES ACCEPTED", "1", "+1 on the 8 weeks before"],
        ["PROJECTS WITH A MERGE", "0", "Same as the 8 weeks before"],
        ["START TO MERGE, P50", "no value", "No figures for the 8 weeks before"]);
      ck("delivered: and a milestone alone keeps the charts, with no empty sentence",
         (await count(page, "delivered-empty")) === 0
         && (await page.getByTestId("report-section-delivered").locator("[data-week-chart]").count()) === 2,
         await text(page, "report-section-delivered"));
      await page.unroute(reading);
      for (const [label, alter, want] of [
        ["delivered: a shorter start to merge reads as a minus", (d) => { d.previous.start_to_merge.p50 = 30.5; },
         "-1 day 3 hours on the 4 weeks before"],
        ["delivered: under a minute apart reads the same", (d) => { d.previous.start_to_merge.p50 = 3.505; },
         "Same as the 4 weeks before"],
        ["delivered: earlier figures and no start time now give no change line",
         (d) => { d.start_to_merge = { p50: null, p90: null, tickets: 0 }; }, undefined],
      ]) {
        await page.route(reading, async (r) => {
          const res = await r.fetch();
          const body = await res.json();
          // an answer with nothing earlier passes as it is and fails the check: a throw in here
          // would end the process with the daemon still running
          if (body.sections.delivered.previous) alter(body.sections.delivered);
          await r.fulfill({ response: res, json: body });
        });
        await view("/report?range=4w");
        const line = page.getByTestId("delivered-start-to-merge").locator("[data-change]");
        const got = (await line.count()) ? (await line.innerText()).trim() : undefined;
        ck(label, got === want, `${got}`);
        await page.unroute(reading);
      }
      await ctx.close();
    }

    // ---- Time: where the stage time went, how long a ticket took, and what is stuck now
    {
      const { ctx, page, asked } = await fresh(1440);
      const view = async (to) => {
        await open(page, to);
        await page.getByTestId("time-stuck").waitFor();
      };
      const flat = (s) => s.replace(/\s+/g, " ").trim();
      /** The rows of a table as read, a "|" between cells; "-" for a cell with no value. */
      const table = (id, rows) => page.getByTestId(id).locator(rows).evaluateAll((trs) => trs.map((tr) =>
        [...tr.cells].map((c) => (c.querySelector(".sr-only") ? "-" : c.textContent.trim())).join("|")));
      const rows = async (id) => (await page.getByTestId(`stuck-${id}-row`).allInnerTexts()).map(flat);
      const of = async (id) => ((await count(page, `stuck-${id}-count`)) ? text(page, `stuck-${id}-count`) : "(no count)");
      const q = ids.questions, t = ids.tickets;

      await view("/report");
      ck("time: the four stages with share, time and the split",
         JSON.stringify(await table("time-stages", "tbody tr")) === JSON.stringify([
           "In progress|67%|1 day 2 hours|0%|100%", "In review|6%|2 hours 30 minutes|20%|80%",
           "Changes asked|1%|30 minutes|0%|100%", "Blocked|26%|10 hours|50%|50%"]), JSON.stringify(await table("time-stages", "tbody tr")));
      ck("time: headed as shares of these four stages, with the note on agent time",
         (await page.getByRole("table", { name: "Share of time in these four stages" }).count()) === 1
         && (await text(page, "time-agent-note")) === "Agent time: Everything not covered by an answered question, including checks and queues. Unfinished stages are not counted.",
         await text(page, "time-agent-note"));
      const share = await table("time-share-weeks", "tr[data-series]");
      ck("time: share per week, a gap in a week with nothing", JSON.stringify(share) === JSON.stringify([
        "In progress|-|100%|-|-|-|62%|67%|-", "In review|-|0%|-|-|-|0%|28%|-",
        "Changes asked|-|0%|-|-|-|0%|5%|-", "Blocked|-|0%|-|-|-|38%|0%|-"]), JSON.stringify(share));
      const held = await table("time-waiting-weeks", "tr[data-series]");
      ck("time: waiting per week and stage, no value where a stage had no time", JSON.stringify(held) === JSON.stringify([
        "In progress|-|0%|-|-|-|0%|0%|-", "In review|-|-|-|-|-|-|20%|-",
        "Changes asked|-|-|-|-|-|-|0%|-", "Blocked|-|-|-|-|-|50%|-|-"]), JSON.stringify(held));
      ck("time: all four lines are drawn in both charts, and the running week says so",
         (await page.getByTestId("time-share-weeks").locator("[data-plot] [data-line]").count()) === 4
         && (await page.getByTestId("time-waiting-weeks").locator("[data-plot] [data-line]").count()) === 4
         && (await page.getByTestId("report-section-time").locator(`th[data-week="${ids.monday}"]`).filter({ hasText: "so far" }).count()) === 2);
      const buckets = await table("time-histogram", "tr[data-bucket]");
      ck("time: one histogram for the range", JSON.stringify(buckets) === JSON.stringify(
        ["Under 1 h||0", "1 to 4 h||6", "4 to 24 h||1", "1 to 3 days||1", "Over 3 days||0"]), JSON.stringify(buckets));
      const bars = await page.getByTestId("time-histogram").locator("rect").evaluateAll((r) => r.map((e) => Math.round(e.getBoundingClientRect().width)));
      ck("time: the histogram's bars follow its counts", bars[0] === 0 && bars[1] > 100 && bars[2] === bars[3]
         && Math.abs(bars[1] - 6 * bars[2]) <= 6 && bars[4] === 0, JSON.stringify(bars));
      ck("time: and says how many merged tickets it holds", (await text(page, "time-histogram-of")) === "from 8 of 9 merged tickets",
         await text(page, "time-histogram-of"));
      ck("time: no change line and no second word about the unfinished week",
         (await page.getByTestId("report-section-time").locator("[data-change]").count()) === 0
         && !(await text(page, "report-section-time")).includes("not over"));
      ck("stuck: as of the last pass, as a stamp, how long ago in the tooltip",
         /^As of \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z$/.test(await text(page, "stuck-as-of"))
         && /^(just now|\d+\w* ago)$/.test((await page.getByTestId("stuck-as-of").getAttribute("title")) || ""),
         `${await text(page, "stuck-as-of")} ${await page.getByTestId("stuck-as-of").getAttribute("title")}`);
      const asking = await rows("questions");
      ck("stuck: the five oldest blocking questions, none addressed to someone else",
         asking.map((r) => (r.match(/(D\d+) · /) || [])[1]).join() === [100, 80, 55, 30, 10].map((h) => q[h]).join()
         && (await of("questions")) === "5 of 7", `${await of("questions")} ${JSON.stringify(asking)}`);
      ck("stuck: a question shows its text, number, project and age",
         asking[0] === `May the crew rotate the signing key? ${q[100]} · No project · Asked 4 days ago`
         && asking[1] === `Which database should the page read? ${q[80]} · project-a · Asked 3 days ago`, JSON.stringify(asking.slice(0, 2)));
      const longest = await rows("tickets");
      ck("stuck: the five tickets longest in one stage",
         longest.map((r) => r.split(" ")[0]).join() === [190, 150, 120, 90, 50].map((h) => t[h]).join()
         && (await of("tickets")) === "5 of 7", `${await of("tickets")} ${JSON.stringify(longest)}`);
      ck("stuck: a ticket shows its number, title, project, stage and age",
         longest[0] === `${t[190]} Move the sync project-a · Moved to in progress 7 days ago`
         && longest[1] === `${t[150]} Read the logs project-b · Moved to review 6 days ago`, JSON.stringify(longest.slice(0, 2)));
      ck("stuck: a question of no project links to the Crew page",
         (await page.getByTestId("stuck-questions-row").first().getAttribute("href")) === `/crew#desk-${q[100]}`,
         await page.getByTestId("stuck-questions-row").first().getAttribute("href"));
      ck("time: the whole page still comes from one read", asked.length === 1, `${asked.length} reads`);
      await checkWords(page, ck, "time @1440");
      ck("time @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, "time-8w-1440");

      await view(`/report?scope=project:${ids["project-a"]}`);
      ck("stuck: one project shows its own questions and tickets",
         (await rows("questions")).map((r) => (r.match(/(D\d+) · /) || [])[1]).join() === [80, 30, 5].map((h) => q[h]).join()
         && (await of("questions")) === "3 of 3"
         && (await rows("tickets")).map((r) => r.split(" ")[0]).join() === [190, 120, 50, 5].map((h) => t[h]).join()
         && (await of("tickets")) === "4 of 4", `${JSON.stringify(await rows("questions"))} ${JSON.stringify(await rows("tickets"))}`);

      await view(days(30));
      ck("time: a past range with no stage ended says so, with no table, chart or histogram",
         (await count(page, "time-empty")) === 1 && (await text(page, "time-empty")) === "No stage ended in these weeks."
         && (await count(page, "time-stages")) === 0 && (await count(page, "time-share-weeks")) === 0
         && (await count(page, "time-histogram")) === 0, await text(page, "report-section-time"));
      ck("time: and still shows what is stuck now", (await rows("questions")).length === 5 && (await rows("tickets")).length === 5,
         await text(page, "report-section-time"));

      await view(`/report?scope=team:${ids.Bo}`);
      ck("time: a scope with nothing says all three, with no row and no count",
         (await text(page, "time-empty")) === "No stage ended in these weeks."
         && (await text(page, "stuck-questions")).endsWith("No blocking question is open.")
         && (await text(page, "stuck-tickets")).endsWith("No ticket is in a stage now.")
         && (await count(page, "stuck-questions-row")) === 0 && (await count(page, "stuck-tickets-row")) === 0
         && (await of("questions")) === "(no count)" && (await of("tickets")) === "(no count)",
         await text(page, "report-section-time"));
      await checkWords(page, ck, "time with nothing @1440");
      await shot(page, "time-empty-1440");

      // before the cell's first pass there is no list to show; made here, the seed has passed
      const reading = /\/api\/factory\/report\?/;
      await page.route(reading, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        if (body.sections.time) body.sections.time.stuck = null;
        await r.fulfill({ response: res, json: body });
      });
      await open(page, "/report?range=4w");
      const none = (await count(page, "stuck-not-yet")) ? await text(page, "stuck-not-yet") : "(no line)";
      ck("stuck: before the first pass it says it is not worked out, not that nothing is stuck",
         none === "Stuck now is not worked out yet." && (await count(page, "stuck-questions")) === 0
         && (await count(page, "stuck-as-of")) === 0, none);
      await ctx.close();
    }

    // ---- Review: pass rate and rounds, findings by kind and severity, projects, what keeps coming back
    {
      const { ctx, page, asked } = await fresh(1440);
      const rulesRead = [];     // the page asks for no rules: the report says which row is marked
      page.on("request", (r) => { const u = new URL(r.url()); if (u.pathname === "/api/rule-proposals") rulesRead.push(u.search); });
      const view = async (to) => {
        await open(page, to);
        await page.getByTestId("review-recurring").waitFor();
      };
      const eq = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got));
      const split = (s) => s.split("\n").map((l) => l.trim()).filter(Boolean);
      const lines = async (id) => ((await count(page, id)) ? split(await page.getByTestId(id).innerText()) : []);
      const tiles = async () => [await lines("review-first-pass"), await lines("review-rounds")];
      /** A chart's value for each week, oldest first; "-" for a week with none. */
      const weekly = (id) => page.getByTestId(`review-${id}-weeks`).locator("tr[data-series] td")
        .evaluateAll((cells) => cells.map((c) => (c.querySelector(".sr-only") ? "-" : c.textContent.replace(/\s+/g, " ").trim())));
      /** The findings table's rows as a screen reader hears them, a "|" between columns. */
      const findings = () => page.getByTestId("review-findings").locator("[data-finding]")
        .evaluateAll((rows) => rows.map((row) => [...row.children].map((c) => c.textContent.trim()).join("|")));
      /** The sorted column's header: its title, its arrow and what a screen reader hears. */
      const sortedBy = () => page.getByTestId("review-findings").locator("[data-sort][data-sorted]")
        .evaluateAll((buttons) => buttons.map((b) => b.textContent.trim()));
      const press = (column) => page.getByTestId("review-findings").locator(`[data-sort="${column}"]`).click();
      const projects = async () => (await page.getByTestId("review-project-row").allInnerTexts()).map(split);
      const recurring = async () => (await page.getByTestId("recurring-row").allInnerTexts()).map((s) => s.replace(/\s+/g, " ").trim());
      /** A findings row from "Kind|Severity|fixed|open". */
      const row = (s) => { const [kind, severity, fixed, open] = s.split("|"); return `${kind}|${severity}|Fixed ${fixed}|Still open ${open}`; };
      const rows = (...s) => s.map(row);

      await view("/report");
      const REPEATED = ["Error handling example-org/web · 3 tickets", "Missing test example-org/web · 3 tickets · Rule accepted"];
      eq("review: the pass rate and rounds of the range", await tiles(),
         [["PASSED FIRST REVIEW", "67%", "of 3 tickets first reviewed"], ["REVIEW ROUNDS PER TICKET", "1.3"]]);
      eq("review: pass rate per week, no value in a week without a first review", await weekly("pass"),
         ["-", "-", "-", "-", "-", "100%", "50%", "-"]);
      eq("review: rounds per week, no value in a week without a first review", await weekly("rounds"),
         ["-", "-", "-", "-", "-", "1", "1.5", "-"]);
      const charts = await page.getByTestId("report-section-review").locator("[data-week-chart]")
        .evaluateAll((els) => els.map((e) => `${e.dataset.weekChart}:${e.querySelector("figcaption").textContent}`));
      eq("review: two lines, the running week saying so far", [charts,
         await page.getByTestId("report-section-review").locator(`th[data-week="${ids.monday}"]`).filter({ hasText: "so far" }).count()],
         [["lines:Passed first review per week", "lines:Review rounds per ticket per week"], 2]);

      eq("findings: four columns", await page.getByTestId("review-findings").locator("[data-sort]")
         .evaluateAll((buttons) => buttons.map((b) => b.dataset.sort)), ["kind", "severity", "fixed", "open"]);
      eq("findings: by kind and severity, fixed and still open, most open first", [await findings(), await sortedBy()],
         [rows("Missing test|P2|1|2", "Error handling|P3|0|2", "Security|P1|0|1", "Error handling|P2|0|1"),
          ["Still open↓, sorted descending"]]);
      await press("open");
      eq("findings: the sorted column again turns it round, equal rows in the same order", [await findings(), await sortedBy()],
         [rows("Security|P1|0|1", "Error handling|P2|0|1", "Missing test|P2|1|2", "Error handling|P3|0|2"),
          ["Still open↑, sorted ascending"]]);
      await press("kind");
      eq("findings: sorted by kind, A first", [await findings(), await sortedBy()],
         [rows("Error handling|P3|0|2", "Error handling|P2|0|1", "Missing test|P2|1|2", "Security|P1|0|1"),
          ["Kind↑, sorted ascending"]]);
      await press("severity");
      eq("findings: sorted by severity, P1 first", [await findings(), await sortedBy()],
         [rows("Security|P1|0|1", "Missing test|P2|1|2", "Error handling|P2|0|1", "Error handling|P3|0|2"),
          ["Severity↑, sorted ascending"]]);
      await press("fixed");
      eq("findings: sorted by fixed, most first", [await findings(), await sortedBy()],
         [rows("Missing test|P2|1|2", "Error handling|P3|0|2", "Security|P1|0|1", "Error handling|P2|0|1"),
          ["Fixed↓, sorted descending"]]);
      await press("fixed");
      eq("findings: and fewest first", [await findings(), await sortedBy()],
         [rows("Error handling|P3|0|2", "Security|P1|0|1", "Error handling|P2|0|1", "Missing test|P2|1|2"),
          ["Fixed↑, sorted ascending"]]);

      eq("projects: lowest pass rate first, each with the weeks before", await projects(), [
        ["project-a", "50% passed first review · 1.5 review rounds per ticket reviewed · 2 tickets first reviewed",
         "The 8 weeks before: no ticket first reviewed"],
        ["project-b", "100% passed first review · 1 review round per ticket reviewed · 1 ticket first reviewed",
         "The 8 weeks before: no ticket first reviewed"]]);

      eq("recurring: highest count first, its repo, the accepted rule marked and the open one not", await recurring(), REPEATED);
      ck("recurring: headed as the last 30 days, as of the last pass",
         (await page.getByRole("heading", { level: 3, name: "Recurring findings, last 30 days", exact: true }).count()) === 1
         && /^As of \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z$/.test(await text(page, "recurring-as-of")), await text(page, "review-recurring"));
      eq("review: one report read and no read of the rules, sorting included", [asked.length, rulesRead.length], [1, 0]);
      await checkWords(page, ck, "review @1440");
      ck("review @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, "review-8w-1440");

      await view(`/report?range=dates&from=${plus(ids.monday, -7 * 19)}`);
      eq("projects: the weeks before with figures, and without", await projects(), [
        ["project-a", "50% passed first review · 1.5 review rounds per ticket reviewed · 2 tickets first reviewed",
         "The 20 weeks before: no ticket first reviewed"],
        ["project-b", "100% passed first review · 1 review round per ticket reviewed · 1 ticket first reviewed",
         "The 20 weeks before: 0% passed first review · 1 review round per ticket reviewed · 1 ticket first reviewed"]]);

      await view(days(30));
      eq("review: a past week with a review and no finding", [await tiles(), await count(page, "review-findings"),
         (await count(page, "review-no-findings")) ? await text(page, "review-no-findings") : "(no line)", await projects()],
         [[["PASSED FIRST REVIEW", "0%", "of 1 ticket first reviewed"], ["REVIEW ROUNDS PER TICKET", "1"]], 0,
          "No review finding was first raised in these weeks.",
          [["project-b", "0% passed first review · 1 review round per ticket reviewed · 1 ticket first reviewed",
            "The week before: no ticket first reviewed"]]]);
      eq("recurring: the same list whatever range is picked", await recurring(), REPEATED);

      await view(`/report?scope=project:${ids["project-a"]}`);
      eq("recurring: counted over the scope's projects, here under 3 tickets for every kind",
         [(await lines("review-first-pass"))[1], await recurring(), (await count(page, "recurring-none"))], ["50%", [], 1]);

      await view(`/report?scope=team:${ids.Bo}`);
      eq("review: a scope with nothing says all three, with no tile, chart, table or project",
         [await text(page, "review-empty"), await text(page, "review-no-findings"), await text(page, "recurring-none"),
          await count(page, "review-first-pass"), await page.getByTestId("report-section-review").locator("[data-week-chart]").count(),
          await count(page, "review-findings"), await count(page, "review-projects")],
         ["No ticket had its first review in these weeks.", "No review finding was first raised in these weeks.",
          "No finding kind came back on 3 or more tickets in the last 30 days.", 0, 0, 0, 0]);
      await checkWords(page, ck, "review with nothing @1440");
      await shot(page, "review-empty-1440");

      // before the cell's first pass there is no list to show; made here, the seed has passed
      const reading = /\/api\/factory\/report\?/;
      await page.route(reading, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        if (body.sections.review) body.sections.review.recurring = null;
        await r.fulfill({ response: res, json: body });
      });
      await open(page, "/report?range=4w");
      eq("recurring: before the first pass it says it is not worked out, not that nothing came back",
         [(await count(page, "recurring-not-yet")) ? await text(page, "recurring-not-yet") : "(no line)",
          await count(page, "recurring-none"), await count(page, "recurring-row"), await count(page, "recurring-as-of")],
         ["Recurring findings are not worked out yet.", 0, 0, 0]);
      await ctx.close();
    }
    {
      const { ctx, page } = await fresh(390);
      await open(page, "/report");
      await page.getByTestId("review-findings").waitFor();
      const within = await inside(page);
      const widths = await page.getByTestId("review-findings").locator("[data-findings-head], [data-finding]")
        .evaluateAll((els) => els.map((e) => [e.scrollWidth, e.clientWidth]));
      ck("390: the review tiles, findings table, projects and recurring findings fit",
         (await overflow(page)) <= 0 && within && widths.length === 5 && widths.every(([s, c]) => s <= c),
         `${await overflow(page)}px over, inside ${within}, rows ${JSON.stringify(widths)}`);
      await checkWords(page, ck, "review @390");
      await shot(page, "review-8w-390");
      await ctx.close();
    }

    // ---- the Cost section
    {
      const { ctx, page } = await fresh(1440);
      const eq = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got));
      const flat = (s) => s.replace(/\s+/g, " ").trim();
      const said_ = async (id) => ((await count(page, id)) ? text(page, id) : "(not there)");
      const rows = async (id) => (await page.getByTestId(id).locator("tbody tr").allInnerTexts()).map(flat);
      const week = async (id, back) => flat((await page.getByTestId(id)
        .locator(`tr[data-week="${plus(ids.monday, -7 * back)}"]`).locator("td").allInnerTexts()).join(" | "));
      const top = async (id) => (await page.getByTestId(`cost-top-${id}-row`).allInnerTexts()).map(flat);
      const view = async (to) => { await open(page, to); await page.getByTestId("cost-counted").waitFor(); };
      const [second, first, third] = ids.costed;
      const a = ids["project-a"], b = ids["project-b"];

      await view("/report");
      eq("cost: the total counts a reply with no price beside it",
         [(await said_("cost-total")).toLowerCase(), await count(page, "cost-narrow"), await count(page, "cost-empty")],
         ["api-equivalent cost at least $23.00 1 reply with no price", 0, 0]);
      eq("cost: the amounts are labelled API-equivalent, with the reason", await said_("cost-label"),
         "API-equivalent cost: list prices from tares/pricing.py. A subscription crew pays a flat fee.");
      eq("cost: spend per week, with the projects in it",
         [await week("cost-weeks", 1), await week("cost-weeks", 2), await week("cost-weeks", 3),
          await page.getByTestId("cost-weeks").locator(`tr[data-week="${plus(ids.monday, -7)}"] [data-bar]`).count(),
          await page.getByTestId("cost-weeks").locator("tbody tr").count()],
         ["project-a $17.00 | $17.00", "project-b at least $3.75 No project $2.25 | at least $6.00", "| $0", 1, 8]);
      eq("cost: spend by project, highest first, adding up to the total", await rows("cost-projects"),
         ["project-a $17.00", "project-b at least $3.75 1 reply with no price", "No project $2.25",
          "Total at least $23.00 1 reply with no price"]);
      eq("cost: spend by role, adding up to the total", await rows("cost-roles"),
         ["Builder at least $18.00 1 reply with no price", "Reviewer $1.50", "Orchestrator $0.5000", "Releaser $0.2500",
          "Own sessions $0.7500", "Other $2.00", "Total at least $23.00 1 reply with no price"]);
      eq("cost: per merged ticket per week, no value in a week with none",
         [await week("cost-per-ticket", 1), await week("cost-per-ticket", 2), await week("cost-per-ticket", 6),
          (await page.getByTestId("cost-per-ticket").locator(`tr[data-week="${plus(ids.monday, -21)}"] td`).first().innerText()).trim()],
         ["3 | $5.67", "2 | at least $1.50 1 ticket with replies with no price", "4 | $0", "0"]);
      eq("cost: per accepted milestone, and what is tied to no ticket",
         [(await said_("cost-per-milestone")).toLowerCase().includes("$5.67 3 milestones accepted in these weeks"),
          (await said_("cost-untied")).toLowerCase().includes("not tied to a ticket, api-equivalent cost $3.00"),
          await said_("cost-no-project")], [true, true, "No project: $2.25"]);
      eq("cost: the most expensive merged tickets, with rounds and time", await top("tickets"), [
        `${second} Add a page project-a · $10.00 · 1 review round · 3 hours from first start to merge`,
        `${first} Add a page project-a · $7.00 · 2 review rounds · 2 hours from first start to merge`,
        `${third} Add a page project-b · at least $3.00 · 1 reply with no price · 1 review round · 1 day 2 hours from first start to merge`]);
      eq("cost: the most expensive milestones", await top("milestones"),
         ["Step 1 project-a · $17.00 · 2 tickets · 3 review rounds · 3 hours from first start to acceptance"]);
      const counted = await said_("cost-counted");
      ck("cost: how it is counted says it is approximate, and the rules",
         ["approximate", "in progress or with changes asked", "A reviewer's reply is split", "open to everyone and in its own project",
          "misses crew replies that carry no project", "mean cost of the tickets first merged that week"].every((p) => counted.includes(p)),
         counted);
      ck("cost: every figure says what it cannot see",
         (await page.getByTestId("report-section-cost").locator("[data-note]").count()) === 8
         && (await page.getByTestId("report-section-cost").locator("[data-note]").evaluateAll((els) => els.every((e) => e.textContent.trim().length > 20))),
         `${await page.getByTestId("report-section-cost").locator("[data-note]").count()}`);
      await checkWords(page, ck, "cost @1440");
      ck("cost @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      if (SHOTS) await page.getByTestId("report-section-cost").screenshot({ path: path.join(SHOTS, "report-cost-1440.png") });

      await page.getByTestId("cost-top-tickets-row").first().click();
      await page.getByTestId("ticket-title").waitFor();
      eq("cost: an expensive ticket opens the ticket", new URL(page.url()).pathname, `/projects/${a}/tickets/${second}`);
      await view("/report");
      await page.getByTestId("cost-top-milestones-row").first().click();
      const row = page.locator(`#milestone-${ids.step}`);
      try { await row.waitFor({ state: "visible", timeout: 5000 }); } catch { /* the check says what is there */ }
      eq("cost: an expensive milestone opens its row on the milestones page",
         [new URL(page.url()).pathname, new URL(page.url()).hash, await row.isVisible()],
         [`/projects/${a}/milestones`, `#milestone-${ids.step}`, true]);

      await view(`/report?scope=project:${b}`);
      eq("cost: one project leaves out what has no project, and says what it misses",
         [await said_("cost-narrow"), await rows("cost-projects"), await count(page, "cost-no-project"),
          (await said_("cost-untied")).includes("$0.7500"), (await top("tickets")).length, await count(page, "cost-top-milestones-row")],
         ["This scope misses crew replies that carry no project.",
          ["project-b at least $3.75 1 reply with no price", "Total at least $3.75 1 reply with no price"], 0, true, 1, 0]);

      await view(`/report?scope=team:${ids.Bo}`);
      eq("cost: a scope with nothing spent says so, with no table of spend and no ticket",
         [await said_("cost-empty"), await count(page, "cost-total"), await count(page, "cost-weeks"),
          await count(page, "cost-roles"), await count(page, "cost-top-tickets-row"), await count(page, "cost-top-milestones-row"),
          (await said_("cost-per-milestone")).toLowerCase().includes("no value 0 milestones accepted")],
         ["No spend was received for these weeks.", 0, 0, 0, 0, 0, true]);
      await checkWords(page, ck, "cost with nothing @1440");

      // before the cell's first pass there are no ticket costs; made here, the seed has passed
      await page.route(/\/api\/factory\/report\?/, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        if (body.sections.cost) body.sections.cost.by_ticket = null;
        await r.fulfill({ response: res, json: body });
      });
      await view("/report?range=4w");
      eq("cost: before the first pass it says ticket costs are not worked out, with the spend shown",
         [await said_("cost-not-yet"), await count(page, "cost-tickets"), await count(page, "cost-total")],
         ["Ticket and milestone costs are not worked out yet.", 0, 1]);
      await ctx.close();
    }
    {
      const { ctx, page } = await fresh(390);
      await open(page, "/report");
      await page.getByTestId("cost-top-milestones-row").first().waitFor();
      const tables = await page.getByTestId("report-section-cost").locator('figure [role="region"]')
        .evaluateAll((els) => els.map((e) => [e.scrollWidth, e.clientWidth]));
      ck("390: the cost tiles, tables and expensive lists fit",
         (await overflow(page)) <= 0 && await inside(page) && tables.length === 4 && tables.every(([s, c]) => s <= c),
         `${await overflow(page)}px over, tables ${JSON.stringify(tables)}`);
      await checkWords(page, ck, "cost @390");
      if (SHOTS) await page.getByTestId("report-section-cost").screenshot({ path: path.join(SHOTS, "report-cost-390.png") });
      await ctx.close();
    }

    // ---- the Merges section
    {
      const { ctx, page } = await fresh(1440);
      const eq = (label, got, want) => ck(label, JSON.stringify(got) === JSON.stringify(want), JSON.stringify(got));
      const flat = (s) => s.replace(/\s+/g, " ").trim();
      const said_ = async (id) => ((await count(page, id)) ? text(page, id) : "(not there)");
      const view = async (to) => { await open(page, to); await page.getByTestId("merges-counted").waitFor(); };
      const [unrecorded, second, first, third] = ids.merged;
      const a = ids["project-a"], b = ids["project-b"];
      const lines = page.getByTestId("merges-ledger").locator("tbody tr");
      const keys = () => lines.evaluateAll((els) => els.map((e) => e.dataset.merge.replace("example-org/web", "")));
      const column = async (name) => (await lines.locator(`[data-${name}]`).allInnerTexts()).map(flat);
      const levels = async () => (await page.getByTestId("merges-levels").locator("li").allInnerTexts()).map(flat);
      const byWeek = async (back) => flat((await page.getByTestId("merges-weeks")
        .locator(`tr[data-week="${plus(ids.monday, -7 * back)}"] td`).allInnerTexts()).join(" | "));

      await view("/report");
      eq("merges: newest first, one line for each merge through the queue", await keys(), ["#4", "#2", "#1", "#3"]);
      eq("merges: each change names its ticket, project and pull request", await column("merged-ticket"), [
        `${unrecorded} Add a page project-b example-org/web #4`, `${second} Add a page project-a example-org/web #2`,
        `${first} Add a page project-a example-org/web #1`, `${third} Add a page project-b example-org/web #3`]);
      eq("merges: the level of each merge, as it was then", await column("level"),
         ["Level not recorded", "L4: merges alone", "L3: a person approves", "L3: a person approves"]);
      eq("merges: the gate on the reviewed commit, and what CI said", await column("gate"),
         ["not recorded", "Reviewer's pass, No CI", "Reviewer's pass, CI passed", "Merged outside Tares, CI passed"]);
      eq("merges: the reviewed and the merged commit", await column("commits"),
         ["reviewed not recorded merged not recorded", "reviewed abc1234 merged fee0002",
          "reviewed abc1234 merged fee0001", "reviewed abc1234 merged fee0003"]);
      eq("merges: review rounds", await column("rounds"), ["not recorded", "1", "2", "not recorded"]);
      eq("merges: who approved", await column("approver"),
         ["Approver not recorded", "None needed", "Ada", "Approver not recorded"]);
      eq("merges: merges per week by level",
         [await byWeek(0), await byWeek(1), await byWeek(2), await byWeek(3),
          await page.getByTestId("merges-weeks").locator("tbody tr").count(),
          await page.getByTestId("merges-weeks").locator(`tr[data-week="${ids.monday}"] th`).filter({ hasText: "so far" }).count()],
         ["0 | 0 | 1", "1 | 1 | 0", "1 | 0 | 0", "0 | 0 | 0", 8, 1]);
      eq("merges: the count, with no empty text", [await said_("merges-shown"), await count(page, "merges-empty")],
         ["4 merges in these weeks, newest first.", 0]);
      eq("merges: each handed-over project's level now", await levels(), ["project-a · L4: merges alone"]);
      const counted = await said_("merges-counted");
      ck("merges: how it is counted says what is left out and what each column means",
         ["Pushes that skipped the queue", "can differ from Delivered", "the one that passed the gate", "At L4 no approval is needed"]
           .every((p) => counted.includes(p)), counted);

      const [file] = await Promise.all([page.waitForEvent("download", { timeout: 10000 }),
                                        page.getByTestId("merges-csv").click()]);
      const csv = fs.readFileSync(await file.path(), "utf8").trim().split(/\r?\n/);
      ck("merges: the CSV downloads with a header and every merge, whole commit ids included",
         /^merges-\d{4}-\d{2}-\d{2}-to-\d{4}-\d{2}-\d{2}\.csv$/.test(file.suggestedFilename()) && csv.length === 5
         && csv[0].startsWith("Merged at (UTC),Project,Ticket,Title,Repository,Pull request,Level,Gate,")
         && csv[3].includes(`project-a,${first},Add a page,example-org/web,1,`)
         && csv[3].includes(`${"abc1234".padEnd(40, "0")},${"fee0001".padEnd(40, "0")},2,Ada`),
         `${file.suggestedFilename()} ${JSON.stringify(csv)}`);
      await checkWords(page, ck, "merges @1440");
      ck("merges @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      if (SHOTS) await page.getByTestId("report-section-merges").screenshot({ path: path.join(SHOTS, "report-merges-1440.png") });

      await lines.filter({ hasText: "#1" }).getByRole("link", { name: first }).click();
      await page.getByTestId("ticket-title").waitFor();
      eq("merges: a ticket opens the ticket", new URL(page.url()).pathname, `/projects/${a}/tickets/${first}`);

      await view(`/report?scope=project:${b}`);
      eq("merges: one project's own merges, and no level for a project not handed over",
         [await keys(), await count(page, "merges-levels")], [["#4", "#3"], 0]);

      await view(`/report?scope=team:${ids.Bo}`);
      eq("merges: a scope with no merge says so, with no table and no download",
         [await said_("merges-empty"), await count(page, "merges-ledger"), await count(page, "merges-weeks"),
          await count(page, "merges-csv"), await count(page, "merges-levels")],
         ["Nothing was merged through the merge queue in these weeks.", 0, 0, 0, 0]);
      await checkWords(page, ck, "merges with nothing @1440");

      // more merges than the page lists
      await page.route(/\/api\/factory\/report\?/, async (r) => {
        const res = await r.fetch();
        const body = await res.json();
        if (body.sections.merges) body.sections.merges.total = 1234;
        await r.fulfill({ response: res, json: body });
      });
      await view("/report?range=4w");
      eq("merges: when there are more than the page lists, it says so", await said_("merges-shown"),
         "Showing the newest 4 of 1,234 merges. The CSV holds all of them.");
      await ctx.close();
    }
    {
      const { ctx, page } = await fresh(390);
      await open(page, "/report");
      await page.getByTestId("merges-ledger").waitFor();
      const box = await page.getByTestId("merges-ledger").locator('[role="region"]')
        .evaluate((e) => [e.scrollWidth, e.clientWidth]);
      const button = await page.getByTestId("merges-csv").boundingBox();
      ck("390: the ledger scrolls inside its own box and the CSV button is in the window",
         (await overflow(page)) <= 0 && await inside(page) && box[0] > box[1]
         && !!button && button.x >= 0 && button.x + button.width <= 390,
         `${await overflow(page)}px over, box ${JSON.stringify(box)}, button ${JSON.stringify(button)}`);
      await checkWords(page, ck, "merges @390");
      if (SHOTS) await page.getByTestId("report-section-merges").screenshot({ path: path.join(SHOTS, "report-merges-390.png") });
      await ctx.close();
    }

    // ---- the links of "stuck now", on a phone so the row has to be scrolled to
    {
      const { ctx, page } = await fresh(390);
      const q = ids.questions, t = ids.tickets;
      const project = `/projects/${ids["project-a"]}`;
      await open(page, "/report");
      await page.getByTestId("stuck-tickets").waitFor();
      ck("390: the stage table, histogram and stuck lists fit",
         (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      const broken = await page.getByTestId("time-stages").locator("th, td").evaluateAll((cells) => cells.flatMap((c) => {
        const out = [];
        const walk = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
        for (let n = walk.nextNode(); n; n = walk.nextNode()) {
          if (n.parentElement.closest(".sr-only")) continue;
          for (const m of n.textContent.matchAll(/\S+/g)) {
            const r = document.createRange();
            r.setStart(n, m.index);
            r.setEnd(n, m.index + m[0].length);
            if (r.getClientRects().length > 1) out.push(m[0]);
          }
        }
        return out;
      }));
      ck("390: no word of the stage table is split over two lines", broken.length === 0, broken.join(" "));
      await shot(page, "time-8w-390");

      await page.goto(`${BASE}${project}`, { waitUntil: "networkidle" });
      await page.getByTestId("waiting-others-toggle").waitFor();
      ck("links: the project page starts with Waiting for others closed",
         (await page.getByTestId("waiting-other-row").filter({ visible: true }).count()) === 0);

      await open(page, "/report");
      await page.getByTestId("stuck-questions-row").filter({ hasText: `${q[30]} · ` }).click();
      const row = page.locator(`[data-desk~="${q[30]}"]`);
      let shown = false;
      try {
        await row.waitFor({ state: "visible", timeout: 5000 });
        await page.waitForFunction((label) => {
          const el = document.querySelector(`[data-desk~="${label}"]`);
          const r = el?.getBoundingClientRect();
          // gone to the row (near the top, or the page scrolls no further), and not under the top bar
          return !!r && (r.top < 120 || document.documentElement.scrollHeight - innerHeight - scrollY <= 1)
            && r.top < innerHeight - 40 && el.contains(document.elementFromPoint(r.left + 4, r.top + 2));
        }, q[30], { timeout: 5000 });
        shown = true;
      } catch { /* the check says where it ended */ }
      ck("links: a question opens its project page at the row, clear of the top bar",
         new URL(page.url()).pathname === project && new URL(page.url()).hash === `#desk-${q[30]}` && shown
         && (await row.innerText()).includes("May the crew delete"),
         `${page.url()} ${JSON.stringify(await row.count() ? await row.boundingBox() : null)}`);
      await shot(page, "link-question-390");
      ck("links: the opened rows fit a phone, a long unbroken name included",
         (await page.getByTestId("waiting-other-row").filter({ visible: true }).count()) === 3
         && (await overflow(page)) <= 0, `${await overflow(page)}px over`);

      await open(page, "/report");
      await page.getByTestId("stuck-tickets-row").filter({ hasText: "Move the sync" }).click();
      await page.getByTestId("ticket-title").waitFor();
      ck("links: a ticket opens the ticket",
         new URL(page.url()).pathname === `${project}/tickets/${t[190]}` && (await text(page, "ticket-title")) === "Move the sync",
         `${page.url()} ${await text(page, "ticket-title")}`);

      await open(page, "/report");
      await page.getByTestId("stuck-questions-row").first().click();
      const heading = page.getByRole("heading", { level: 1, name: "Crew", exact: true });
      try { await heading.waitFor({ timeout: 10000 }); } catch { /* the check says what is there */ }
      ck("links: a question of no project opens the Crew page",
         new URL(page.url()).pathname === "/crew" && await heading.isVisible(),
         `${page.url()} ${JSON.stringify(await page.locator("h1").allInnerTexts())}`);
      await ctx.close();
    }

    // ---- phone width
    {
      const { ctx, page } = await fresh(390);
      await open(page, "/report?range=4w");
      await says(page, "390: the page reads", "5 tickets merged.");
      ck("390: six section headings", (await sections(page)).join("|") === SECTIONS.join("|"), (await sections(page)).join("|"));
      ck("390: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      const at = await Promise.all(["merged", "milestones", "projects", "start-to-merge"].map((id) => page.getByTestId(`delivered-${id}`).boundingBox()));
      ck("390: the tiles sit two to a row",
         at[0].y === at[1].y && at[2].y === at[3].y && at[2].y > at[0].y && at[0].x === at[2].x, JSON.stringify(at));
      await page.goto(`${BASE}/report`, { waitUntil: "networkidle" });
      await page.getByTestId("delivered-p50-weeks").waitFor();
      ck("390: 8 weeks of charts scroll inside their own box, not the page",
         await page.locator('[data-week-chart] [role="region"]').evaluateAll((els) => els.length === 6 && els.every((e) => e.scrollWidth > e.clientWidth))
         && (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      ck("390: a start-to-merge value in two units takes two lines and fits its column",
         await page.getByTestId("delivered-p50-weeks").locator("tr[data-series] td").evaluateAll((cells) => {
           const long = cells.find((c) => c.textContent.replace(/\s/g, " ") === "1 day 2 hours");
           return !!long && long.innerText.trim() === "1 day\n2 hours" && cells.every((c) => c.scrollWidth <= c.clientWidth);
         }));
      await shot(page, "delivered-8w-390");
      await page.getByTestId("delivered-p50-weeks").locator('[role="region"]').evaluate((e) => { e.scrollLeft = e.scrollWidth; });
      if (SHOTS) await page.getByTestId("delivered-p50-weeks").screenshot({ path: path.join(SHOTS, "report-p50-390-end.png") });
      await open(page, "/report?range=4w");
      await checkWords(page, ck, "frame @390");
      await shot(page, "frame-390");
      await pick(page, "report-range", "Two dates");
      await page.getByTestId("report-to").waitFor();
      await says(page, "390: two dates", "5 tickets merged.");
      ck("390: two dates fit the window", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, "dates-390");
      await ctx.close();
    }

    // ---- embedded in the desktop
    for (const [width, theme] of [[1440, "dark"], [390, "light"]]) {
      const { ctx, page } = await fresh(width);
      const tag = `embedded @${width}`;
      await open(page, `/report?embed=desktop&theme=${theme}&range=4w`);
      await says(page, `${tag}: the page reads`, "5 tickets merged.");
      ck(`${tag}: no header, the desktop's theme`, (await count(page, "edition-header")) === 0
         && (await attr(page, "data-embed")) === "desktop" && (await attr(page, "data-theme")) === theme);
      ck(`${tag}: the title, pickers and six headings are there`,
         await page.getByRole("heading", { level: 1, name: "Report", exact: true }).isVisible()
         && await page.getByTestId("report-pickers").isVisible() && (await sections(page)).join("|") === SECTIONS.join("|"));
      if (width === 390) {
        ck(`${tag}: the slim bar keeps Menu and search`, await page.getByTestId("menu-button").isVisible()
           && await page.getByTestId("edition-search-button").isVisible());
      } else {
        ck(`${tag}: the menu stays, with Report in it`, await page.getByTestId("edition-menu").getByTestId("menu-report").isVisible());
      }
      ck(`${tag}: no horizontal scroll`, (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, `embedded-${width}`);
      await pick(page, "report-range", "Last 8 weeks");
      await says(page, `${tag}: the range changes`, "9 tickets merged.");
      ck(`${tag}: and the page stays embedded`, (await count(page, "edition-header")) === 0
         && (await attr(page, "data-embed")) === "desktop" && query(page).range === "8w", page.url());
      await ctx.close();
    }

    ck("no page error anywhere", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
