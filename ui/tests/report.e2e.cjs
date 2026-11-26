/*
 * Browser e2e for TR-1667 (the factory Report page's frame) and TR-1668 (its Delivered section).
 *
 * A scratch factory cell seeded by tests/seed-factory-report.py (8 weeks hold 9 merged tickets, 4 weeks
 * 5). Checked: the menu entry; the page from one read of GET /api/factory/report; the six section
 * headings, the "cannot see" line and the "API-equivalent" label; each range and each kind of scope
 * changing the address and the figures; a copied address opening the same view; two dates; a range with
 * no merged ticket; Delivered's four tiles, their change on the weeks before, the lines under them and
 * the two weekly charts, also with reviews and no merge and with nothing delivered; a scope or a date
 * the cell refuses; a read that fails; at 1440 and 390 wide, and embedded in the desktop
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
        || !!e.parentElement.closest('[data-week-chart] [role="region"]'));
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
                 `9 tickets merged. ${said(first)} to ${said(plus(ids.monday, 6))}, in weeks that start on Monday (UTC). This week is not over yet.`);
      ck("frame: what the report cannot see, in full", (await text(page, "report-cannot-see")) === CANNOT_SEE,
         await text(page, "report-cannot-see"));
      ck("frame: six section headings in order", (await sections(page)).join("|") === SECTIONS.join("|"), (await sections(page)).join("|"));
      ck("frame: no empty line while tickets were merged", (await count(page, "report-empty")) === 0);
      ck("frame: each section but Delivered says it is not built yet",
         (await page.locator('[data-testid^="report-section-"]').filter({ hasText: "This section is not built yet." }).count()) === 5
         && !(await text(page, "report-section-delivered")).includes("not built"));
      ck("frame: the Cost section is labelled API-equivalent",
         await page.getByTestId("report-section-cost").getByText("API-equivalent", { exact: true }).isVisible());
      ck("frame: no date fields until two dates are asked for", (await count(page, "report-from")) === 0);
      await checkWords(page, ck, "frame @1440");
      ck("frame @1440: no horizontal scroll", (await overflow(page)) <= 0 && await inside(page), `${await overflow(page)}px over`);
      await shot(page, "frame-1440");

      // ---- range and scope live in the address
      await pick(page, "report-range", "Last 4 weeks");
      await says(page, "range: 4 weeks count 5 merged tickets", `5 tickets merged. ${said(plus(ids.monday, -21))}`);
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
      await says(page, "dates: an earlier first date counts the old ticket too", `10 tickets merged. ${said(ids.old_monday)}`);
      ck("dates: the address holds the new date", query(page).from === ids.old_monday, page.url());
      await page.getByTestId("report-to").fill(plus(ids.old_monday, 2));
      await says(page, "dates: one week long, one ticket, said in the singular",
                 `1 ticket merged. ${said(ids.old_monday)} to ${said(plus(ids.old_monday, 6))}, in weeks that start on Monday (UTC).`);
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
        await says(page, "dates: a last date cleared by hand means today again", `10 tickets merged. ${said(ids.old_monday)}`);
        ck("dates: and it leaves the address", !("to" in query(page)) && query(page).from === ids.old_monday, page.url());
        await page.getByTestId("report-from").fill("");
        await says(page, "dates: an emptied first date means the usual weeks", `9 tickets merged. ${said(plus(ids.monday, -49))} to `);
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
         && !(await text(page, "report-summary")).includes("merged."), await text(page, "report-empty"));
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
      const days = (back) => `/report?range=dates&from=${plus(ids.monday, -7 * back)}&to=${plus(ids.monday, -7 * back + 6)}`;

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
         (await page.locator(`[data-week-chart] th[data-week="${ids.monday}"]`).filter({ hasText: "so far" }).count()) === 2);
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
         await page.locator('[data-week-chart] [role="region"]').evaluateAll((els) => els.length === 2 && els.every((e) => e.scrollWidth > e.clientWidth))
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
