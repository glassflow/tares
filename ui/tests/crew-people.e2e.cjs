/*
 * Browser e2e for the Crew page with two people on one cell.
 *  - Another person's station: the By category fold never asks for a /context reading of it
 *    (the reading is taken on its owner's machine), and says who can. Your own station still asks.
 *  - A who-am-I answer that fails: the page says it could not load that and waits; it does not
 *    treat the viewer as `local` and show someone else's crew as theirs.
 *
 * Runs the real daemon on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/crew-people.e2e.cjs
 * Needs: tares importable by the python at TARES_E2E_PY (default: the repo .venv, else python3,
 * with the repo on PYTHONPATH). TARES_E2E_CHROME points Playwright at another Chrome binary when
 * its own browser download is missing. TARES_E2E_SHOTS=<dir> saves a screenshot of each step.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = 8812;
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = "root-token-crew-people-e2e";
const SHOTS = process.env.TARES_E2E_SHOTS || "";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function call(method, url, body, headers = {}) {
  const data = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
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

const stamp = (s) => new Date(Date.now() + s * 1000).toISOString();
const stationLine = (sid, crew, machine) => JSON.stringify({
  type: "session_station", sessionId: sid, cwd: "/w/crew", timestamp: stamp(-30),
  station: "crew-orchestrator", role: "orchestrator", parent: "", factory_station: "crew-orchestrator",
  factory_crew: crew, crew, factory_machine: machine, machine,
});
const userLine = (sid, text) => JSON.stringify({
  type: "user", sessionId: sid, cwd: "/w/crew", timestamp: stamp(-5), message: { role: "user", content: text },
});

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-crew-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  const daemon = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], {
    cwd: REPO,
    env: { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "crew.duckdb"),
           TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(PORT), TARES_HOST: "127.0.0.1",
           TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off", TARES_AUTH_TOKEN: ROOT,
           TARES_UI_DIST: path.join(UI_DIR, "dist") },
    stdio: "ignore",
  });
  const stop = () => { try { daemon.kill(); } catch { /* gone */ } };
  process.on("exit", stop);

  ck("daemon up", await waitFor(`${BASE}/health`));
  const rootH = { authorization: `Bearer ${ROOT}` };
  let r = await call("POST", "/api/sources", { name: "claude_code", connector: "claude_code", poll: "10s", config: { push: true } }, rootH);
  ck("the claude_code source is made", r.status < 300, r.body);

  const keys = {}, people = {};
  for (const [who, mail] of [["ashish", "ashish@example.com"], ["kiran", "kiran-test@example.com"]]) {
    r = await call("POST", "/api/keys", { name: `${who}-laptop`, scopes: ["admin"], person: { email: mail, name: who } }, rootH);
    keys[who] = r.json && r.json.secret;
    people[who] = r.json && r.json.person && r.json.person.id;
  }
  ck("two people with a key each", keys.ashish && keys.kiran && people.ashish !== people.kiran, "keys");
  for (const [who, crew, machine] of [["ashish", "a", "ashish-mac"], ["kiran", "b", "kiran-pc"]]) {
    r = await call("POST", "/ingest/claude_code",
      [stationLine(`e2e-${crew}`, crew, machine), userLine(`e2e-${crew}`, "working")].join("\n") + "\n",
      { authorization: `Bearer ${keys[who]}`, "content-type": "application/x-ndjson" });
    ck(`${who}'s crew ${crew} is recorded`, r.status < 300, r.body);
  }
  r = await call("GET", "/api/crew", undefined, rootH);
  const crews = (r.json && r.json.crews) || [];
  const crewOf = (who) => (crews.find((c) => c.person && c.person.id === people[who]) || {}).id;
  ck("the cell lists both crews", crewOf("ashish") && crewOf("kiran"), r.body.slice(0, 200));

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const open = async (who) => {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, keys[who]);
    const page = await ctx.newPage();
    const asks = [];
    page.on("request", (q) => { if (q.method() === "POST" && /\/api\/crew\/[^/]+\/context/.test(q.url())) asks.push(q.url()); });
    return { ctx, page, asks };
  };
  const shot = async (page, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); } };
  const member = (who) => `${BASE}/crew?member=crew-orchestrator&crew=${encodeURIComponent(crewOf(who))}`;
  const fold = (page) => page.locator("summary", { hasText: "By category" }).first();

  try {
    console.log("== another person's station: no context ask ==");
    {
      const { ctx, page, asks } = await open("kiran");
      await page.goto(member("ashish"));
      await fold(page).waitFor({ timeout: 15000 });
      await fold(page).click();
      await page.waitForTimeout(1500);
      ck("kiran opened ashish's By category fold", await page.locator("details[open] summary", { hasText: "By category" }).count() === 1);
      ck("no /context ask was sent", asks.length === 0, asks.join(","));
      ck("it says who can ask", await page.getByText("Only ashish can ask for one").count() === 1);
      await shot(page, "kiran-sees-ashish-station.png");
      await ctx.close();
    }
    console.log("== your own station still asks ==");
    {
      const { ctx, page, asks } = await open("ashish");
      await page.goto(member("ashish"));
      await fold(page).waitFor({ timeout: 15000 });
      await fold(page).click();
      await page.waitForTimeout(1500);
      ck("one /context ask was sent", asks.length === 1, asks.join(","));
      await shot(page, "ashish-sees-own-station.png");
      await ctx.close();
    }
    console.log("== a failing who-am-I answer is not treated as local ==");
    {
      const { ctx, page } = await open("kiran");
      let failing = true;
      await page.route("**/api/whoami", (route) => failing
        ? route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"whoami is down"}' })
        : route.continue());
      await page.goto(`${BASE}/crew`);
      await page.getByText("load who you are").first().waitFor({ timeout: 15000 });
      ck("the page says it could not load who you are", true);
      ck("and shows no crew as yours", await page.getByText(/Your crew/).count() === 0);
      ck("nor the members list", await page.getByText("Pick a member").count() === 0);
      await shot(page, "whoami-down.png");
      failing = false;
      await page.getByRole("button", { name: "Retry" }).click();
      await page.getByRole("heading", { name: "Crew", exact: true }).waitFor({ timeout: 15000 });
      await page.waitForTimeout(500);
      ck("after Retry the page loads", await page.getByText("load who you are").count() === 0);
      await shot(page, "whoami-back.png");
      await ctx.close();
    }
  } catch (e) {
    ck("the run finished", false, String(e && e.stack || e));
  } finally {
    await browser.close();
    stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
