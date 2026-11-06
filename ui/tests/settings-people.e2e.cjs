/*
 * Browser e2e for Settings, Access with several people on one cell.
 *  - Signed in with a person's key: the Access box names that key and its person, not the root
 *    token; the People list marks that person as you and does not call the boot person "You".
 *  - Signed in with the root token: the Access box says so.
 *  - A key with no person: the Access box says it belongs to no one.
 *  - An open cell (no auth): the boot person is still "You", as before.
 *
 * Runs the real daemon on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/settings-people.e2e.cjs
 * Needs: tares importable by the python at TARES_E2E_PY (default: the repo .venv, else python3,
 * with the repo on PYTHONPATH). TARES_E2E_CHROME points Playwright at another Chrome binary when
 * its own browser download is missing.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const ROOT = "root-token-settings-people-e2e";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

function call(base, method, url, body, headers = {}) {
  const data = body === undefined ? "" : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const r = http.request(base + url, {
      method,
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data), ...headers },
    }, (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, json: j }); });
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

function startDaemon(port, auth, people = []) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-settings-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  const env = { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "s.duckdb"),
                TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(port), TARES_HOST: "127.0.0.1",
                TARES_EDITION: "platform", TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off", TARES_UI_DIST: path.join(UI_DIR, "dist") };
  delete env.TARES_AUTH_TOKEN;
  if (auth) env.TARES_AUTH_TOKEN = ROOT;
  if (people.length) {   // people on the cell before it starts: adding a second person over HTTP needs a licence
    const seeded = spawnSync(py, [path.join(REPO, "tests", "_people.py"), env.TARES_DB, ...people], { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, encoding: "utf8" });
    if (seeded.status !== 0) { console.log("FAIL could not seed people: " + (seeded.stderr || seeded.stdout)); process.exit(1); }
  }
  const d = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], { cwd: REPO, env, stdio: "ignore" });
  process.on("exit", () => { try { d.kill(); } catch { /* gone */ } });
  return { d, tmp, base: `http://127.0.0.1:${port}` };
}

async function accessText(browser, base, token) {
  const ctx = await browser.newContext();
  if (token) await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
  const page = await ctx.newPage();
  await page.goto(`${base}/settings`);
  await page.getByRole("heading", { name: "People" }).waitFor({ timeout: 15000 });
  await page.locator("table tbody tr").first().waitFor({ timeout: 15000 });
  await page.waitForFunction(() => !document.body.innerText.includes("loading…"), null, { timeout: 15000 });
  const access = await page.locator(".panel", { has: page.getByRole("heading", { name: "Access" }) }).innerText();
  const people = await page.locator(".panel", { has: page.getByRole("heading", { name: "People" }) }).locator("tbody tr").allInnerTexts();
  await ctx.close();
  return { access, people };
}

(async () => {
  const on = startDaemon(8813, true, ["Bob B=bob@example.com", "Alice A=alice@example.com"]);
  const off = startDaemon(8814, false);
  if (!(await waitFor(`${on.base}/health`)) || !(await waitFor(`${off.base}/health`))) {
    console.log("FAIL daemons did not start"); process.exit(1);
  }
  const root = { authorization: `Bearer ${ROOT}` };
  const bob = await call(on.base, "POST", "/api/keys",
    { name: "bob-laptop", scopes: ["admin"], person: { email: "bob@example.com", name: "Bob B" } }, root);
  await call(on.base, "POST", "/api/keys",
    { name: "alice-laptop", scopes: ["read"], person: { email: "alice@example.com", name: "Alice A" } }, root);
  const nobody = await call(on.base, "POST", "/api/keys", { name: "ci-script", scopes: ["read"] }, root);
  ck("keys made", bob.status === 201 && nobody.status === 201, `${bob.status} ${nobody.status}`);

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  try {
    const b = await accessText(browser, on.base, bob.json.secret);
    ck("Bob: Access names his key and him", b.access.includes("You signed in with the API key bob-laptop, which belongs to Bob B."), b.access);
    ck("Bob: Access does not say root token sign-in", !b.access.includes("You signed in with the root token"), b.access);
    const bobRow = b.people.find((r) => r.includes("bob@example.com")) || "";
    const aliceRow = b.people.find((r) => r.includes("alice@example.com")) || "";
    ck("Bob: his row is marked you", /you/i.test(bobRow.replace("bob@example.com", "")), bobRow);
    ck("Bob: Alice's row is not marked you", !/you/i.test(aliceRow), aliceRow);
    ck("Bob: no person is called You", !b.people.some((r) => r.split("\t")[0].trim() === "You"), JSON.stringify(b.people));
    ck("Bob: the boot person shows as Local (no login)", b.people.some((r) => r.startsWith("Local (no login)")), JSON.stringify(b.people));

    const r = await accessText(browser, on.base, ROOT);
    ck("root: Access says the root token", r.access.includes("You signed in with the root token printed by tares up --auth."), r.access);

    const n = await accessText(browser, on.base, nobody.json.secret);
    ck("key with no person: Access says it belongs to no one", n.access.includes("API key ci-script, which belongs to no one."), n.access);

    const o = await accessText(browser, off.base, null);
    ck("open cell: the boot person is still You", o.people.some((x) => x.split("\t")[0].trim() === "You"), JSON.stringify(o.people));
    ck("open cell: no Local (no login) row", !o.people.some((x) => x.includes("Local (no login)")), JSON.stringify(o.people));
  } finally {
    await browser.close();
    for (const s of [on, off]) { try { s.d.kill(); } catch { /* gone */ } fs.rmSync(s.tmp, { recursive: true, force: true }); }
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
