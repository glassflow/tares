/*
 * Browser e2e for TR-834: the Grants page never lists a person who declined a grant under "agreed by".
 * The daemon leaves a declined (revoked) grant out of /api/grants today, so the page is given a
 * row with an agreement and a decline in its consents and must name only the one who agreed; a
 * row with a decline alone has nobody under "agreed by".
 *
 * Runs the real daemon on a scratch data directory (auth on), serving the built console.
 * Run:  npm run build && TARES_E2E_PORT=8860 node tests/grants-declined.e2e.cjs   (from ui/)
 * Needs tares importable by the python at TARES_E2E_PY (default: the repo .venv, else python3, with
 * the repo on PYTHONPATH). TARES_E2E_CHROME points Playwright at another Chrome binary.
 * TARES_E2E_SHOTS=<dir> saves a screenshot.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8860);
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = "root-token-grants-declined-e2e";
const SHOTS = process.env.TARES_E2E_SHOTS || "";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function call(method, url, body, headers = {}) {
  const data = body === undefined ? "" : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const r = http.request(BASE + url, {
      method,
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data), ...headers },
    }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, body: d, json: j }); });
    });
    r.on("error", reject); r.write(data); r.end();
  });
}

function waitFor(url, tries = 150) {
  return new Promise((resolve) => {
    const tick = () => http.get(url, (r) => { r.resume(); resolve(true); })
      .on("error", () => (--tries > 0 ? setTimeout(tick, 200) : resolve(false)));
    tick();
  });
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-grants-declined-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  // a person on the cell before it starts: adding a second person over HTTP needs a licence
  const seeded = spawnSync(py, [path.join(REPO, "tests", "_people.py"), path.join(tmp, "g.duckdb"), "alex=alex-gd@example.com"],
    { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, encoding: "utf8" });
  if (seeded.status !== 0) { console.log("FAIL could not seed people: " + (seeded.stderr || seeded.stdout)); process.exit(1); }
  const daemon = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], {
    cwd: REPO,
    env: { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "g.duckdb"),
           TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(PORT), TARES_HOST: "127.0.0.1",
           TARES_EDITION: "platform", TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off", TARES_AUTH_TOKEN: ROOT,
           TARES_UI_DIST: path.join(UI_DIR, "dist") },
    stdio: "ignore",
  });
  const stop = () => { try { daemon.kill(); } catch { /* gone */ } };
  process.on("exit", stop);

  ck("daemon up", await waitFor(`${BASE}/health`));
  const rootH = { authorization: `Bearer ${ROOT}` };
  let r = await call("POST", "/api/keys", { name: "alex-laptop", scopes: ["admin"], person: { email: "alex-gd@example.com", name: "alex" } }, rootH);
  const key = r.json && r.json.secret;
  r = await call("POST", "/api/projects", { template: "custom", name: "shop", objects: [], kind: "software_factory" }, { authorization: `Bearer ${key}` });
  const uid = r.json && r.json.id;
  ck("a person with a key leads a project", !!key && !!uid, r.body);

  const grant = (id, words, consents) => ({ id, kind: "project", project: uid, words, state: "waiting", given_at: new Date().toISOString(),
    given_by: { id: "p-alex", name: "Alex" }, holder: null, areas: ["example-org/docs"], consents, waiting_for: [], in_effect: false });
  const at = new Date().toISOString();
  const body = { all: "", project: "", all_id: null, project_id: null, waiting_for_me: [], known_words: [], grants: [
    grant("G1", "Builders may change the docs", [{ id: "p-kim", name: "Kim", at }, { id: "p-lee", name: "Lee", at, declined: true, reason: "not ready" }]),
    grant("G2", "Builders may delete old docs", [{ id: "p-lee", name: "Lee", at, declined: true }]),
  ] };

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  try {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, key);
    const page = await ctx.newPage();
    await page.route("**/api/grants?*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }));
    await page.goto(`${BASE}/projects/${uid}?view=grants`);
    const row = (words) => page.locator("tr", { hasText: words });
    await row("change the docs").waitFor({ timeout: 15000 });
    // columns: grant, kind, given by, agreed by, state, actions
    const cell = async (words) => (await row(words).locator("td").nth(3).innerText()).trim();
    const agreed1 = await cell("change the docs");
    ck("Kim, who agreed, is under agreed by", /Kim/.test(agreed1), agreed1);
    ck("Lee, who declined, is not", !/Lee/.test(agreed1), agreed1);
    const agreed2 = await cell("delete old docs");
    ck("a grant only declined has nobody under agreed by", agreed2 === "", JSON.stringify(agreed2));
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, "grants-declined.png"), fullPage: true }); }
    await ctx.close();
  } catch (e) {
    ck("the run finished", false, String(e && e.stack || e));
  } finally {
    await browser.close();
    stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
