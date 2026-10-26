/*
 * Browser e2e for TR-689: the Projects list hides finished projects, sorts by work pending, opens a
 * project from its title (no Open button), has no Objects column, keeps the factory chip small at a
 * narrow width, and shows when a project last moved from its tickets.
 *
 * Seeds a scratch cell (tests/seed-projects-list.py: 16 projects, done and active), then runs the
 * real daemon on it, serving the built console.
 * Run:  npm run build && node tests/projects-list.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep screenshots at desktop and narrow width.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8823);
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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-projects-list-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-projects-list.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded 16 projects", Object.keys(ids).length === 16, String(Object.keys(ids).length));

  const env = { ...process.env, TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, ANTHROPIC_API_KEY: "sk-ant-scratch-not-real",
                PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const rows = page.getByTestId("project-row");
    const names = async () => (await rows.locator(".pj-name strong").allInnerTexts());
    const shot = async (n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const open = async (q = "") => {
      await page.goto(`${BASE}/projects${q}`, { waitUntil: "networkidle" });
      await rows.first().waitFor();
    };

    // 1. finished projects are hidden by default and one click away
    await open();
    ck("the default view lists the 12 projects not done", (await rows.count()) === 12, `${await rows.count()} rows`);
    const statuses = await rows.evaluateAll((r) => r.map((x) => x.getAttribute("data-status")));
    ck("and none of them is done", !statuses.includes("done"), statuses.join(","));
    const doneLink = page.getByTestId("show-done-link");
    ck("it says how many are hidden", /4 done projects are hidden/.test(await page.locator(".pj-foot").innerText()));
    await shot("1-desktop.png");
    await page.getByTestId("show-done").click();
    ck("the Done tab lists only the 4 done projects",
       (await rows.count()) === 4 && (await rows.evaluateAll((r) => r.every((x) => x.dataset.status === "done"))));
    await page.getByTestId("show-all").click();
    ck("the All tab lists all 16", (await rows.count()) === 16, `${await rows.count()} rows`);
    await page.getByTestId("show-active").click();
    ck("the Active tab brings the default back", (await rows.count()) === 12);
    await open();
    await doneLink.click();
    ck("the hidden-projects link shows everything", (await rows.count()) === 16);
    ck("the choice lives in the address, so a reload keeps it", /show=all/.test(page.url()), page.url());
    await open("?show=done");
    ck("a link to the Done view opens it", (await rows.count()) === 4);
    const q = page.getByRole("textbox", { name: /Filter projects/ });
    await open("?show=all");
    await q.fill("silent");
    ck("the filter box matches names", (await names()).sort().join("|") === "Rius: Silent Err 0|silent-errors-context-rot", (await names()).join("|"));
    await q.fill("stripe");
    ck("and goals", (await names()).join("|") === "Billing webhooks", (await names()).join("|"));
    await q.fill("zzzz");
    ck("a filter with no match says so", (await page.getByTestId("no-match").count()) === 1);

    // 2. sort by work pending
    await open("?sort=most");
    const most = await names();
    ck("most work pending first", most.slice(0, 4).join("|") === "vetme-desktop|fb-90a1|Rius: Cursor Codex Gaps|silent-errors-context-rot", most.join("|"));
    ck("projects without tickets come last, a factory one with none included",
       most.slice(-4).every((n) => ["Challenger workflow", "Checkout latency watch", "Billing webhooks", "New idea"].includes(n)), most.join("|"));
    await open("?sort=least");
    const least = await names();
    ck("least work pending first (nothing pending leads)", ["Rius Bugs Auto", "Tests auto fix"].includes(least[0]) && ["Rius Bugs Auto", "Tests auto fix"].includes(least[1]), least.join("|"));
    ck("and the one with the most is the last with tickets", least[least.length - 5] === "vetme-desktop", least.join("|"));
    await open();
    await page.getByTestId("sort").selectOption("most");
    ck("the sort control changes the order", (await names())[0] === "vetme-desktop");
    ck("and the address", /sort=most/.test(page.url()), page.url());

    // 3. updated reflects activity: vetme-desktop's own row is 13 days old, its newest ticket 9 minutes
    await open();
    const recent = await names();
    ck("recently active ranks by ticket activity, not the project row's age",
       recent.indexOf("vetme-desktop") < recent.indexOf("Rius: Cursor Codex Gaps")
       && recent.indexOf("🔄 Tares Bugs Auto Triage & Fix") < recent.indexOf("Claude Code auto-update (TR-669)"), recent.join("|"));
    const vet = rows.filter({ hasText: "vetme-desktop" });
    const updated = (await vet.locator(".pj-updated").innerText()).trim();
    ck("Updated shows the ticket's activity, not the project row's age", /^(\d+ ?m|\d+ ?min|just now)/i.test(updated) || /min/.test(updated), updated);
    const stale = (await rows.filter({ hasText: "Checkout latency watch" }).locator(".pj-updated").innerText().catch(() => "")).trim();
    ck("a project nobody touched shows its own age", stale === "" || !/min/.test(stale), stale);

    // 4. no Open button, no Objects column, the title is the link
    const heads = (await page.locator(".pj-table thead th").allInnerTexts()).map((h) => h.trim().toLowerCase());
    ck("no Objects column", !heads.includes("objects"), heads.join(","));
    ck("columns are name, status, pending, updated", heads.slice(0, 4).join(",") === "name,status,pending,updated", heads.join(","));
    ck("no Open button", (await page.locator(".pj-table").getByRole("link", { name: /^open$/i }).count()) === 0
       && (await page.locator(".pj-table").getByRole("button", { name: /^open$/i }).count()) === 0);
    const link = rows.filter({ hasText: "vetme-desktop" }).getByRole("link", { name: "vetme-desktop" });
    ck("the title is a link to the project", (await link.getAttribute("href")) === `/projects/${ids["vetme-desktop"]}`);
    await link.click();
    await page.waitForURL(`**/projects/${ids["vetme-desktop"]}`);
    ck("and it opens the project", true);

    // 5. status: a ring and a count, no Active/Done words, colour by state
    await open("?show=all");
    const texts = await rows.evaluateAll((r) => r.map((x) => x.querySelectorAll("td")[1].innerText.trim()));
    ck("the status cell has no Active or Done text", !texts.some((t) => /active|done|paused|draft/i.test(t)), texts.join(" | "));
    ck("it names the status for screen readers", (await rows.filter({ hasText: "Rius Bugs Auto" }).locator(".pj-status").getAttribute("aria-label")).startsWith("Active"));
    const dot = (name) => rows.filter({ hasText: name }).locator(".pj-dot").evaluate((e) => getComputedStyle(e).backgroundColor);
    const [active, done] = [await dot("Rius Bugs Auto"), await dot("link-click-counter")];
    ck("active and done have different colours", active !== done, `${active} ${done}`);
    const token = (v) => page.evaluate((x) => { const e = document.createElement("i"); e.style.background = `var(${x})`;
      document.body.appendChild(e); const c = getComputedStyle(e).backgroundColor; e.remove(); return c; }, v);
    ck("done is the success green", done === (await token("--ok")), `${done} vs ${await token("--ok")}`);
    ck("active is the warm yellow", active === (await token("--sand-300")), `${active} vs ${await token("--sand-300")}`);
    const blocked = await rows.filter({ hasText: "Rius: Cursor Codex Gaps" }).locator(".pj-pending").innerText();
    ck("pending shows what is in progress and blocked", /7/.test(blocked) && /1 in progress/.test(blocked) && /2 blocked/.test(blocked), blocked);

    const ring = (name) => rows.filter({ hasText: name }).locator(".pj-dot").evaluate((e) => getComputedStyle(e).borderTopWidth);
    ck("a draft's dot is hollow, a paused one's is filled", (await ring("New idea")) !== (await ring("Checkout latency watch")),
       `${await ring("New idea")} vs ${await ring("Checkout latency watch")}`);
    const draft = rows.filter({ hasText: "New idea" });
    ck("a draft's title opens its setup", (await draft.getByRole("link").first().getAttribute("href")) === `/projects/${ids["New idea"]}/setup`);
    ck("a draft can be deleted from its row", (await draft.getByRole("button", { name: "Delete draft" }).count()) === 1);
    ck("no other row offers Delete draft", (await page.getByRole("button", { name: "Delete draft" }).count()) === 1);

    // 6. goals: a tooltip, or all of them on request; rows stay one line tall by default
    await open();
    const h0 = await rows.first().evaluate((e) => e.getBoundingClientRect().height);
    ck("a row is a single line tall", h0 < 60, String(h0));
    ck("the narrow sort cue stays hidden at desktop width", !(await page.locator(".pj-cue").first().isVisible()));
    ck("goals are not printed in the rows", (await page.getByTestId("goal-line").count()) === 0);
    const title = await rows.filter({ hasText: "Checkout latency watch" }).getByRole("link").first().getAttribute("title");
    ck("hovering a name gives the goal", /Close the loop on every alert/.test(title ?? ""), title ?? "");
    await page.getByTestId("goals-toggle").click();
    ck("the Goals toggle prints them", (await page.getByTestId("goal-line").count()) === 12);
    ck("and keeps it in the address", /goals=1/.test(page.url()), page.url());
    await shot("2-desktop-goals.png");

    // 7. the factory chip is compact and legended
    await open("?show=all");
    ck("factory projects carry a chip", (await page.getByTestId("factory-chip").count()) === 13, `${await page.getByTestId("factory-chip").count()}`);
    const chipH = await page.getByTestId("factory-chip").first().evaluate((e) => e.getBoundingClientRect().height);
    ck("the chip is one line", chipH < 30, String(chipH));
    ck("a legend explains it", /Factory/.test(await page.locator(".pj-foot").innerText()));

    // 8. narrow: the factory chip shrinks to an icon, rows stay one line, the title stays readable, nothing scrolls the page
    for (const width of [640, 390]) {
      await page.setViewportSize({ width, height: 844 });
      await open("?show=all");
      const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      ck(`no horizontal scroll at ${width}px`, over <= 0, `${over}px over`);
      const chipW = await page.getByTestId("factory-chip").first().evaluate((e) => e.getBoundingClientRect().width);
      ck(`the chip is an icon at ${width}px`, chipW < 40, String(chipW));
      const rowH = await rows.first().evaluate((e) => e.getBoundingClientRect().height);
      ck(`rows stay compact at ${width}px`, rowH < 60, String(rowH));
      if (width === 640) {
        const linkBox = await rows.filter({ hasText: "vetme-desktop" }).getByRole("link").first().boundingBox();
        ck("the title is whole at 640px", linkBox && linkBox.width > 90, JSON.stringify(linkBox));
        const sw = await page.locator(".pj-scroll").evaluate((e) => e.scrollWidth - e.clientWidth);
        ck("the table fits at 640px without its own scroll", sw <= 0, `${sw}px`);
      }
      const cues = page.locator(".pj-cue");
      await open("?show=all&sort=most");
      const cue = await rows.filter({ hasText: "Rius: Cursor Codex Gaps" }).locator(".pj-cue").innerText();
      ck(`a pending sort says its order by under the name at ${width}px`,
         /^7 pending$/.test(cue.trim()) && await rows.filter({ hasText: "Rius: Cursor Codex Gaps" }).locator(".pj-cue").isVisible(), cue);
      await open("?show=all");
      ck(`the recent sort shows when at ${width}px`, await cues.first().isVisible() && /ago|now/.test(await cues.first().innerText()), await cues.first().innerText());
      await open("?show=all&sort=name");
      ck(`the name sort needs no cue at ${width}px`, (await cues.first().isVisible().catch(() => false)) === false);
      await open("?show=all");
      await shot(`3-narrow-${width}.png`);
      await open();
      await shot(`4-narrow-${width}-active.png`);
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
