/*
 * Browser e2e for TR-730: the factory home page, drawn from its samples.
 *
 * Starts one scratch daemon with an empty factory cell (TARES_EDITION=factory) on TARES_E2E_PORT
 * (default 8833), MCP port +1, in a clean environment and a fresh data directory, and stops only it.
 * With the browser flag taresSamples=1 each sample (busy, empty, crewQuiet, nothingWaiting, manyWaiting,
 * longGoals, loading, error) is opened at 1440 and 390 wide and shot as factory-home-<sample>-<width>.png.
 * Checks: the rows and sections of each state, "15 more" expanding to all 20, the copy action and its
 * Toast, goals clamped to two lines with the full goal in the title, no horizontal scroll at 390, the
 * words check in every state, and that without the flag ?sample=busy shows no sample.
 * Run:  npm run test:factory-home     (builds first: the daemon serves ui/dist; tares installed in ../.venv or on PATH)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8833);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SAMPLES = ["busy", "empty", "crewQuiet", "nothingWaiting", "manyWaiting", "longGoals", "loading", "error"];
const READY = { loading: "factory-home-loading", error: "factory-home-error" };

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

// A clean environment: the daemon gets only what it needs plus the variables this script sets.
function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-home-e2e-"));
  const env = cleanEnv({ TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                         TARES_UI_DIST: dist, PYTHONPATH: REPO });   // the venv installs the main checkout; this runs the code under test
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env, detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    // A context with the sample flag set and a stand-in clipboard that records what is written to it.
    const flagged = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript(() => {
        try { window.localStorage.setItem("taresSamples", "1"); } catch { /* blocked */ }
        window.__copied = [];
        Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } } });
      });
      return { ctx, page: await ctx.newPage() };
    };
    const open = async (page, sample) => {
      await page.goto(`${BASE}/?sample=${sample}`, { waitUntil: "load" });
      await page.getByTestId(READY[sample] || "factory-home").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const shot = async (page, sample, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-home-${sample}-${width}.png`), fullPage: true });
    };

    for (const width of [1440, 390]) {
      const { ctx, page } = await flagged(width);
      const rows = page.getByTestId("waiting-row");
      const projects = page.getByTestId("project-row");
      const tag = (s) => `${s} @${width}`;
      for (const sample of SAMPLES) {
        await open(page, sample);
        await shot(page, sample, width);
        await checkWords(page, ck, tag(sample));
        ck(`${tag(sample)}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
        ck(`${tag(sample)}: nothing on the page is wider than the window`, await page.evaluate(() => {
          const w = document.documentElement.clientWidth;
          return [...document.querySelectorAll("main *")].every((e) => e.getBoundingClientRect().right <= w + 1);
        }));
        const hello = (await page.locator("h1").first().innerText()).trim();
        ck(`${tag(sample)}: a greeting by the time of day`, /^Good (morning|afternoon|evening)$/.test(hello), hello);
        ck(`${tag(sample)}: Talk to the crew and New project in the top bar`,
           (await page.getByRole("link", { name: "Talk to the crew" }).getAttribute("href")) === "/crew"
           && (await page.getByRole("link", { name: "New project" }).first().getAttribute("href")) === "/projects/new");
      }

      // busy
      await open(page, "busy");
      ck(`busy @${width}: 4 waiting rows`, (await rows.count()) === 4, String(await rows.count()));
      ck(`busy @${width}: 5 project rows`, (await projects.count()) === 5, String(await projects.count()));
      ck(`busy @${width}: no "more" button with 4 waiting`, (await page.getByRole("button", { name: /\d+ more/ }).count()) === 0);
      ck(`busy @${width}: oldest first caption`, await page.getByText("oldest first").isVisible());
      ck(`busy @${width}: the headline is the data's`, (await page.getByTestId("home-headline").innerText()).startsWith("4 things wait for you."));
      const second = rows.nth(1).getByRole("button");
      ck(`busy @${width}: a row's outline button comes before its primary one`,
         (await rows.nth(1).locator("a, button").allInnerTexts()).join("|") === "Open project|Close project");
      ck(`busy @${width}: a link action is a link`, (await rows.nth(0).getByRole("link", { name: "Open Reviews" }).getAttribute("href")) === "/reviews");
      ck(`busy @${width}: a do action is a button`, (await second.count()) === 1 && (await second.innerText()) === "Close project");
      const names = await projects.locator("[data-user-text]").first().innerText();
      ck(`busy @${width}: a project row links to the project`, (await projects.first().getAttribute("href")) === `/projects/${names}`, names);
      ck(`busy @${width}: the first project says 15 of 21 tickets merged and 4 blocked`,
         /15 of 21 tickets merged/.test(await projects.first().innerText()) && /4 blocked/.test(await projects.first().innerText()));
      ck(`busy @${width}: a project with no tickets merged says so`, /0 of 9 tickets merged/.test(await projects.nth(3).innerText()));
      ck(`busy @${width}: a project with no tickets at all says so`, /No tickets yet/.test(await projects.nth(4).innerText()));
      const chip = async (i) => projects.nth(i).locator("span.uppercase").evaluate((e) => getComputedStyle(e).backgroundColor);
      const chips = [0, 1, 2, 3, 4].map(chip);
      const cols = await Promise.all(chips);
      ck(`busy @${width}: the state chips have four different tones`, new Set(cols).size === 4, cols.join(" | "));
      ck(`busy @${width}: the blocked count is red`, await projects.first().getByText("4 blocked").evaluate((e) => {
        const probe = document.createElement("i"); probe.style.color = "var(--err)"; document.body.appendChild(probe);
        const c = getComputedStyle(probe).color; probe.remove(); return getComputedStyle(e).color === c;
      }));
      ck(`busy @${width}: "All projects, 4 finished" links to /projects`,
         (await page.getByRole("link", { name: "All projects, 4 finished" }).getAttribute("href")) === "/projects");
      ck(`busy @${width}: the crew card lists 3 members with their memory`, (await page.getByTestId("crew-member").count()) === 3
         && (await page.getByText("memory 82% full").count()) === 1);
      ck(`busy @${width}: only the member at 80% or more has the amber memory bar`, (await page.getByTestId("memory-warn").count()) === 1
         && (await page.getByTestId("memory").count()) === 2);
      ck(`busy @${width}: How far it may go links to Settings`, (await page.getByRole("link", { name: "How far it may go" }).getAttribute("href")) === "/settings#crew");
      ck(`busy @${width}: the plan card shows both limits`, (await page.getByText("1% used · resets 21:50").count()) === 1
         && (await page.getByText("11% used · resets Fri").count()) === 1);
      ck(`busy @${width}: connected tools and the dashed Add a tool link`, (await page.getByText("GitHub").count()) === 1
         && (await page.getByRole("link", { name: "Add a tool" }).getAttribute("href")) === "/settings#tools");
      const copy = page.getByRole("button", { name: "Copy the hand-over command" });
      ck(`busy @${width}: the copy action is a button`, (await copy.count()) === 1);
      await copy.click();
      await page.getByText("Copied. Paste it into your Claude Code session.", { exact: true }).waitFor();
      ck(`busy @${width}: copying shows the Toast`, true);
      ck(`busy @${width}: and the text it copies is the action's`, JSON.stringify(await page.evaluate(() => window.__copied)) === JSON.stringify(["/tares:ship project-z"]));
      await checkWords(page, ck, `busy @${width} with the Toast`);

      // empty
      await open(page, "empty");
      ck(`empty @${width}: Nothing waits for you.`, await page.getByText("Nothing waits for you.", { exact: true }).isVisible());
      ck(`empty @${width}: no waiting rows and no project rows`, (await rows.count()) === 0 && (await projects.count()) === 0);
      ck(`empty @${width}: a New project button in the projects box`, (await page.getByRole("region", { name: "Projects" }).getByRole("link", { name: "New project" }).count()) === 1);
      ck(`empty @${width}: no "All projects" link on a cell with none`, (await page.getByRole("link", { name: /^All projects/ }).count()) === 0);
      ck(`empty @${width}: plan hidden for one line`, await page.getByText("Plan use shows once the crew reports it.", { exact: true }).isVisible()
         && (await page.getByText(/% used/).count()) === 0);

      // crewQuiet
      await open(page, "crewQuiet");
      ck(`crewQuiet @${width}: the crew says it was not heard from`, (await page.getByText("Not heard from in 1 hour").count()) === 4, String(await page.getByText("Not heard from in 1 hour").count()));
      ck(`crewQuiet @${width}: members are in the not heard state`, (await page.getByRole("img", { name: "not heard" }).count()) === 3);

      // nothingWaiting
      await open(page, "nothingWaiting");
      ck(`nothingWaiting @${width}: Nothing waits for you. and 2 projects`, await page.getByText("Nothing waits for you.", { exact: true }).isVisible() && (await projects.count()) === 2);

      // manyWaiting
      await open(page, "manyWaiting");
      ck(`manyWaiting @${width}: 5 rows`, (await rows.count()) === 5, String(await rows.count()));
      const more = page.getByRole("button", { name: "15 more" });
      ck(`manyWaiting @${width}: a "15 more" button`, (await more.count()) === 1);
      await more.click();
      ck(`manyWaiting @${width}: 20 rows after it`, (await rows.count()) === 20, String(await rows.count()));
      ck(`manyWaiting @${width}: and the button is gone`, (await page.getByRole("button", { name: /\d+ more/ }).count()) === 0);
      ck(`manyWaiting @${width}: no horizontal scroll with all 20`, (await overflow(page)) <= 0);

      // longGoals
      await open(page, "longGoals");
      const goal = projects.first().locator("[title]").first();
      const full = await goal.getAttribute("title");
      const box = await goal.evaluate((e) => ({ h: e.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(e).lineHeight), sh: e.scrollHeight }));
      ck(`longGoals @${width}: the goal holds 300+ characters in its title`, (full || "").length > 300, String((full || "").length));
      ck(`longGoals @${width}: and shows 2 lines at most`, box.h <= box.lh * 2 + 1 && box.sh > box.h, JSON.stringify(box));
      const detail = rows.first().locator("[title]").first();
      const dbox = await detail.evaluate((e) => ({ h: e.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(e).lineHeight), sh: e.scrollHeight }));
      ck(`longGoals @${width}: a waiting detail shows 3 lines at most`, dbox.h <= dbox.lh * 3 + 1 && dbox.sh > dbox.h, JSON.stringify(dbox));
      ck(`longGoals @${width}: the 60 character name stays inside its row`, await projects.first().evaluate((e) => e.scrollWidth <= e.clientWidth + 1));

      // loading, error
      await open(page, "loading");
      ck(`loading @${width}: placeholder rows, no real rows`, (await page.getByRole("status", { name: "Loading" }).count()) >= 3 && (await rows.count()) === 0);
      await open(page, "error");
      ck(`error @${width}: the banner says exactly what failed`,
         (await page.getByRole("alert").innerText()).replace(/\s+/g, " ").startsWith("Home did not load. The cell did not answer, so nothing here is current. Try again in a moment."));
      ck(`error @${width}: with a Retry button`, (await page.getByRole("button", { name: "Retry" }).count()) === 1);
      await ctx.close();
    }

    // without the flag the address alone shows no sample
    {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await ctx.newPage();
      await page.goto(`${BASE}/?sample=busy`, { waitUntil: "load" });
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500);
      ck("no flag: ?sample=busy does not show the busy sample", (await page.getByTestId("factory-home").count()) === 0
         && (await page.getByText("4 PRs to review in project-x").count()) === 0 && (await page.getByTestId("waiting-row").count()) === 0);
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  for (const l of []) console.log(l);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
