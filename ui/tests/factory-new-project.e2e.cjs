/*
 * Browser e2e for New project on a factory cell (TR-1014).
 *  - Projects' New project opens the factory page, not the generic setup or the template gallery.
 *  - The command is `tares factory spec "what you want to build"`, and it follows what you type,
 *    quotes and dollar signs escaped.
 *  - Copy the command puts it on the clipboard and says so.
 *  - The old addresses (templates, custom, shared code context, one template) land on the factory page.
 *  - A platform cell still shows the generic setup and the gallery.
 *  - At phone width the page fits; the page's words keep the words list.
 *
 * Runs the real daemon (a factory cell and a platform cell) on scratch data, serving the built console.
 * Run:  npm run build && node tests/factory-new-project.e2e.cjs
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

const PORT = Number(process.env.TARES_E2E_PORT || 8881);
const SHOTS = process.env.SHOTS_DIR;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function waitFor(url, tries = 150) {
  return new Promise((resolve) => {
    const tick = () => http.get(url, (r) => { r.resume(); resolve(true); })
      .on("error", () => (--tries > 0 ? setTimeout(tick, 200) : resolve(false)));
    tick();
  });
}

function startDaemon(port, edition) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-new-project-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  const env = { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "n.duckdb"),
                TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(port), TARES_HOST: "127.0.0.1",
                TARES_EDITION: edition, TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off",
                TARES_PR_SYNC_SECONDS: "off", TARES_UI_DIST: path.join(UI_DIR, "dist") };
  delete env.TARES_AUTH_TOKEN;
  const d = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], { cwd: REPO, env, stdio: "ignore" });
  process.on("exit", () => { try { d.kill(); } catch { /* gone */ } });
  return { d, tmp, base: `http://127.0.0.1:${port}` };
}

(async () => {
  const fac = startDaemon(PORT, "factory");
  const plat = startDaemon(PORT + 1, "platform");
  if (!(await waitFor(`${fac.base}/health`)) || !(await waitFor(`${plat.base}/health`))) {
    console.log("FAIL daemons did not start"); process.exit(1);
  }
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const page_ = async (base, width = 1440) => {
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, permissions: ["clipboard-read", "clipboard-write"] });
    return { ctx, page: await ctx.newPage() };
  };
  try {
    // ---- factory: from Projects
    const { ctx, page } = await page_(fac.base);
    await page.goto(`${fac.base}/projects`);
    await page.getByTestId("new-project").first().click();
    await page.getByTestId("factory-new-project").waitFor({ timeout: 15000 });
    ck("New project on Projects opens the factory page", new URL(page.url()).pathname === "/projects/new"
       && await page.getByRole("heading", { name: "New project", exact: true }).isVisible());
    ck("no template gallery and no generic setup", (await page.getByText("AI SRE demo").count()) === 0
       && (await page.getByText("Challenger workflow").count()) === 0 && (await page.getByText("From existing objects").count()) === 0);
    const cmd = page.getByTestId("new-project-command");
    ck("the command before an idea", (await cmd.innerText()).trim() === 'tares factory spec "what you want to build"', await cmd.innerText());
    await page.getByTestId("new-project-idea").fill('A "billing"   page for $TEAM');
    ck("the command follows the idea, quotes and dollar signs escaped",
       (await cmd.innerText()).trim() === 'tares factory spec "A \\"billing\\" page for \\$TEAM"', await cmd.innerText());
    ck("what happens next, in four steps", (await page.getByTestId("new-project-next").locator("li").count()) === 4);
    ck("first time: points to Home's Get started list", (await page.getByTestId("new-project-first-time").getByRole("link", { name: "Home" }).getAttribute("href")) === "/");
    await page.getByTestId("new-project-copy").click();
    await page.getByText("Copied. Paste it in a terminal in the repo the project builds on.").first().waitFor({ timeout: 5000 });
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    ck("Copy the command copies it", clip === 'tares factory spec "A \\"billing\\" page for \\$TEAM"', clip);
    await checkWords(page, ck, "factory new project");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "factory-new-project-1440.png"), fullPage: true });

    // ---- factory: the old addresses
    for (const old of ["/projects/new/templates", "/projects/new/custom", "/projects/new/shared_code_context", "/projects/new/ai_sre_demo"]) {
      await page.goto(`${fac.base}${old}`);
      await page.getByTestId("factory-new-project").waitFor({ timeout: 15000 });
      ck(`${old} lands on the factory page`, new URL(page.url()).pathname === "/projects/new", page.url());
    }
    await ctx.close();

    // ---- factory: phone width
    const n = await page_(fac.base, 390);
    await n.page.goto(`${fac.base}/projects/new`);
    await n.page.getByTestId("factory-new-project").waitFor({ timeout: 15000 });
    const over = await n.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck("390: no sideways scroll", over <= 0, String(over));
    if (SHOTS) await n.page.screenshot({ path: path.join(SHOTS, "factory-new-project-390.png"), fullPage: true });
    await n.ctx.close();

    // ---- platform: unchanged
    const p = await page_(plat.base);
    await p.page.goto(`${plat.base}/projects/new/templates`);
    await p.page.getByRole("heading", { name: "Templates" }).waitFor({ timeout: 15000 });
    ck("platform: the template gallery is still there", (await p.page.getByTestId("factory-new-project").count()) === 0
       && new URL(p.page.url()).pathname === "/projects/new/templates");
    await p.page.goto(`${plat.base}/projects/new`);
    await p.page.waitForLoadState("networkidle");
    ck("platform: New project is still the generic setup", (await p.page.getByTestId("factory-new-project").count()) === 0
       && new URL(p.page.url()).pathname === "/projects/new");
    await p.ctx.close();
  } finally {
    await browser.close();
    for (const s of [fac, plat]) { try { s.d.kill(); } catch { /* gone */ } fs.rmSync(s.tmp, { recursive: true, force: true }); }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
