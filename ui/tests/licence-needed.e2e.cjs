/*
 * Browser e2e for the licence line (TR-927). When the cell answers a team action with 402
 * `licence_required`, the console shows <LicenceNeeded> (a short line and a link to the enterprise
 * licence page) and never the raw answer.
 *  - Settings, People on a platform cell: Add person.
 *  - People on a factory cell: Add.
 * The answers are made up with page.route, so the suite does not depend on which routes the daemon
 * has moved out of core.
 *
 * Runs the real daemon on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/licence-needed.e2e.cjs
 * Needs: tares importable by the python at TARES_E2E_PY (default: the repo .venv, else python3,
 * with the repo on PYTHONPATH). TARES_E2E_CHROME points Playwright at another Chrome binary when
 * its own browser download is missing.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8816);
const URL_ENTERPRISE = "https://docs.glassflow.ai/tares/enterprise";
const BODY = { code: "licence_required", message: "Adding people needs an enterprise licence.", url: URL_ENTERPRISE };

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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-licence-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  const env = { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "l.duckdb"),
                TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(port), TARES_HOST: "127.0.0.1",
                TARES_EDITION: edition, TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off",
                TARES_PR_SYNC_SECONDS: "off", TARES_UI_DIST: path.join(UI_DIR, "dist") };
  delete env.TARES_AUTH_TOKEN; delete env.TARES_WORKSPACE_API_URL; delete env.TARES_LICENSE;
  const d = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], { cwd: REPO, env, stdio: "ignore" });
  process.on("exit", () => { try { d.kill(); } catch { /* gone */ } });
  return { d, tmp, base: `http://127.0.0.1:${port}` };
}

const refuse = (page, method, glob) => page.route(glob, (route) => route.request().method() === method
  ? route.fulfill({ status: 402, contentType: "application/json", body: JSON.stringify(BODY) })
  : route.continue());

// the licence line is there, links to the enterprise page in a new tab, and no raw answer is on the page
async function checkLine(page, label, what) {
  const line = page.getByTestId("licence-needed");
  await line.first().waitFor({ timeout: 10000 });
  const text = (await line.first().innerText()).trim();
  ck(`${label}: the licence line shows`, text.includes(`${what} needs an enterprise licence.`), text);
  const a = line.first().locator("a");
  ck(`${label}: it links to the enterprise licence page`, (await a.getAttribute("href")) === URL_ENTERPRISE
     && (await a.getAttribute("target")) === "_blank" && /noreferrer/.test((await a.getAttribute("rel")) || ""));
  const body = await page.locator("body").innerText();
  ck(`${label}: no raw answer on the page`, !body.includes('{"code"') && !body.includes("licence_required"), body.slice(0, 300));
}

(async () => {
  const platform = startDaemon(PORT, "platform");
  const factory = startDaemon(PORT + 1, "factory");
  if (!(await waitFor(`${platform.base}/health`)) || !(await waitFor(`${factory.base}/health`))) {
    console.log("FAIL daemons did not start"); process.exit(1);
  }
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  try {
    // platform: Settings, People, Add person
    {
      const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
      await refuse(page, "POST", "**/api/people");
      await page.goto(`${platform.base}/settings?tab=access`);
      await page.getByRole("heading", { name: "People" }).waitFor({ timeout: 15000 });
      await page.getByRole("button", { name: "Add person" }).first().click();
      await page.getByRole("textbox", { name: "name", exact: true }).fill("Sam S");
      await page.getByRole("textbox", { name: "email", exact: true }).fill("sam@example.com");
      await page.getByRole("button", { name: "Add person" }).last().click();
      await checkLine(page, "settings people", "Adding people");
      await page.context().close();
    }
    // factory: People, Add
    {
      const page = await (await browser.newContext({ viewport: { width: 390, height: 900 } })).newPage();
      await refuse(page, "POST", "**/api/factory/people");
      // a cell with no licence hides the form; this is the licence running out while the page is open
      await page.route("**/api/factory/people", async (route) => {
        if (route.request().method() !== "GET") return route.fallback();
        const res = await route.fetch();
        await route.fulfill({ response: res, json: { ...(await res.json()), can_add: true } });
      });
      await page.goto(`${factory.base}/people`);
      await page.getByTestId("people-add").waitFor({ timeout: 15000 });
      await page.getByTestId("people-email").fill("sam@example.com");
      await page.getByTestId("people-submit").click();
      await checkLine(page, "factory people", "Adding people");
      ck("factory people: the email box shows no error text", (await page.getByTestId("people-email").getAttribute("aria-invalid")) !== "true");
      await page.context().close();
    }
  } finally {
    await browser.close();
    for (const s of [platform, factory]) { try { s.d.kill(); } catch { /* gone */ } fs.rmSync(s.tmp, { recursive: true, force: true }); }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
