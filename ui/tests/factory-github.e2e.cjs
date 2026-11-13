/*
 * Browser e2e for a factory project's pull requests: the Pull requests card on Overview (how many are
 * open, the newest one, a link to all of them) and the project's Pull requests page (/projects/:id/github):
 * by repo, open first, then merged, then closed without merging, each with its ticket, CI, review,
 * size and who merged it. A project with no pull request has a card that says none yet and an empty page.
 * Every state shows only words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds a scratch factory cell (tests/seed-factory-github.py, invented names), then runs the real
 * daemon on it, serving the built console. Every pull request is seeded as read just now, so the
 * page shows it as seeded (the cell has no GitHub credential to read it again).
 * Run:  npm run build && node tests/factory-github.e2e.cjs     (from ui/, tares installed in ../.venv)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8370);
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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-github-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-github.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  const g = ids["project-g"], n = ids["project-n"];
  ck("seeded the projects", !!g && !!n);

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
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(400); };
    const texts = async (loc) => (await loc.allInnerTexts()).map((s) => s.trim());

    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      console.log(`== the project page at ${width} ==`);
      await page.goto(`${BASE}/projects/${g}`, { waitUntil: "load" });
      await page.getByTestId("summary-prs").waitFor();
      await settle(page);
      // TR-1245: Overview has a card for them, the project sidebar a page
      const link = page.getByTestId("summary-prs");
      const big = (await link.getByTestId("summary-big").innerText()).trim(), line = (await link.getByTestId("summary-line").innerText()).trim();
      ck("the card says how many are open and names the newest", big === "3 open" && line.startsWith("#115 "), `${big} | ${line}`);
      ck("the card names all of them", (await link.innerText()).includes("All 7 pull requests"), await link.innerText());
      ck("the project sidebar counts them on Pull requests", (await page.getByTestId("project-page-prs").innerText()).replace(/\s+/g, " ").trim() === "Pull requests 7",
         await page.getByTestId("project-page-prs").innerText());
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, `project page ${width}`);
      await shot(page, `project-${width}.png`);

      console.log(`== the GitHub page at ${width} ==`);
      await link.click();
      await page.getByTestId("github-page").waitFor();
      await settle(page);
      ck("the address is the project's Pull requests page", page.url().endsWith(`/projects/${g}/github`)
         && (await page.getByTestId("project-page-prs").getAttribute("aria-current")) === "page", page.url());
      ck("the summary counts them", (await page.getByTestId("github-summary").innerText()).startsWith(
         "3 open, 3 merged, 1 closed without merging on project-g."), await page.getByTestId("github-summary").innerText());
      const repos = page.getByTestId("pr-repo");
      ck("two repos, in the hand-over's order", (await repos.count()) === 2
         && (await repos.nth(0).innerText()).startsWith("example-org/api"));
      const api = repos.nth(0).getByTestId("pr-row");
      const order = [];
      for (let i = 0; i < await api.count(); i++) order.push(await api.nth(i).getAttribute("data-number"));
      ck("open first, then merged newest first, then closed", order.join(",") === "48,47,44,41,43", order.join(","));
      ck("the repo's counts and the lines its merged pull requests changed",
         (await repos.nth(0).getByTestId("pr-repo-counts").innerText()).trim() === "2 open, 2 merged, 1 closed without merging, merged +408 −59",
         await repos.nth(0).getByTestId("pr-repo-counts").innerText());
      const failing = api.nth(0).getByTestId("pr-facts");
      const f = await failing.innerText();
      ck("an open one: ticket, branch, who opened it, CI, review, size",
         f.includes("T4") && f.includes("factory/T4 → main") && f.includes("by tares-crew") && f.includes("CI failing")
         && f.includes("review round 1: changes asked") && f.includes("2 files +40 −12"), f);
      const merged = await api.nth(2).getByTestId("pr-facts").innerText();
      ck("a merged one says who merged it, with no CI", merged.includes("merged 5 hours ago by alex") && !merged.includes("CI"), merged);
      ck("the closed one says so", (await api.nth(4).getByTestId("pr-state").innerText()).trim().toLowerCase() === "closed without merging");
      ck("the title opens GitHub in a new tab", (await api.nth(0).getByTestId("pr-title").getAttribute("href")) === "https://github.com/example-org/api/pull/48"
         && (await api.nth(0).getByTestId("pr-title").getAttribute("target")) === "_blank");
      ck("no read failed, so no banner", (await page.getByTestId("github-error").count()) === 0);
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, `GitHub page ${width}`);
      await shot(page, `github-${width}.png`);
      await api.nth(0).getByTestId("pr-ticket").click();
      await page.waitForURL(`**/projects/${g}/tickets/T4`);
      ck("the ticket opens its page", page.url().endsWith(`/projects/${g}/tickets/T4`), page.url());
      await ctx.close();
    }

    console.log("== a project with no pull request ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/projects/${n}`, { waitUntil: "load" });
      await page.getByTestId("factory-project").waitFor();
      await settle(page);
      ck("the Pull requests card says none yet", (await page.getByTestId("summary-prs").getByTestId("summary-big").innerText()).trim() === "None yet");
      await page.goto(`${BASE}/projects/${n}/github`, { waitUntil: "load" });
      await page.getByTestId("github-empty").waitFor();
      ck("the GitHub page says none yet", (await page.getByTestId("github-summary").innerText()).startsWith("no pull requests on project-n"),
         await page.getByTestId("github-summary").innerText());
      await checkWords(page, ck, "empty GitHub page");
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
