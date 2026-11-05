/*
 * Browser e2e for the factory People page (TR-1011) on a self-hosted cell.
 *  - The menu has People, and it opens /people.
 *  - Signed in as an admin: every person is listed, you first and marked, admins marked, a person
 *    who never signed in shows as invited; the add form adds a teammate, who shows as invited; the
 *    same email twice says so.
 *  - Signed in with a read key: the list shows, the form does not, and the page says why.
 *  - At phone width the page fits without a sideways scroll.
 *  - The page's words keep the words list.
 *
 * Runs the real daemon on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/factory-people.e2e.cjs
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

const ROOT = "root-token-factory-people-e2e";
const PORT = Number(process.env.TARES_E2E_PORT || 8871);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;

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

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-people-e2e-"));
  const venvPy = path.join(REPO, ".venv/bin/python");
  const py = process.env.TARES_E2E_PY || (fs.existsSync(venvPy) ? venvPy : "python3");
  const env = { ...process.env, HOME: tmp, PYTHONPATH: REPO, TARES_DB: path.join(tmp, "p.duckdb"),
                TARES_CATALOG: path.join(tmp, "none.yaml"), TARES_PORT: String(PORT), TARES_HOST: "127.0.0.1",
                TARES_EDITION: "factory", TARES_OTLP_GRPC_PORT: "off", TARES_MCP_PORT: "off",
                TARES_PR_SYNC_SECONDS: "off", TARES_UI_DIST: path.join(UI_DIR, "dist"), TARES_AUTH_TOKEN: ROOT };
  delete env.TARES_WORKSPACE_API_URL;
  const d = spawn(py, ["-c", "from tares.cli import run_daemon; run_daemon()"], { cwd: REPO, env, stdio: "ignore" });
  process.on("exit", () => { try { d.kill(); } catch { /* gone */ } });
  if (!(await waitFor(`${BASE}/health`))) { console.log("FAIL daemon did not start"); process.exit(1); }

  const root = { authorization: `Bearer ${ROOT}` };
  const bob = await call("POST", "/api/keys", { name: "bob-laptop", scopes: ["admin"], person: { email: "bob@example.com", name: "Bob B" } }, root);
  const alice = await call("POST", "/api/keys", { name: "alice-laptop", scopes: ["read"], person: { email: "alice@example.com", name: "Alice A" } }, root);
  const kim = await call("POST", "/api/factory/people", { email: "kim@example.com", name: "Kim K" }, root);
  await call("PATCH", `/api/people/${kim.json.id}`, { is_admin: true }, root);
  ck("keys and a person made", bob.status === 201 && alice.status === 201, `${bob.status} ${alice.status}`);
  // Alice has used her key once, so she has signed in; Kim never has
  await call("GET", "/api/factory/people", undefined, { authorization: `Bearer ${alice.json.secret}` });

  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const open = async (token, width = 1440) => {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, token);
    const page = await ctx.newPage();
    await page.goto(`${BASE}/people`);
    await page.getByTestId("people-list").waitFor({ timeout: 15000 });
    await page.getByTestId("person-row").first().waitFor({ timeout: 15000 });
    return { ctx, page };
  };
  const rowOf = (page, email) => page.getByTestId("person-row").filter({ hasText: email });
  try {
    // ---- an admin
    const { ctx, page } = await open(bob.json.secret);
    ck("the menu has People, current here", (await page.locator('nav[aria-label="Main"] a', { hasText: "People" }).getAttribute("href")) === "/people"
       && (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "People");
    ck("the heading", await page.getByRole("heading", { name: "People", exact: true }).isVisible());
    const names = (await page.getByTestId("person-name").allInnerTexts()).map((t) => t.trim());
    ck("you come first, and are marked", names[0] === "Bob B" && /\(you\)/.test(await page.getByTestId("person-row").first().innerText()), names.join("|"));
    ck("every person is listed", ["Bob B", "Alice A", "Kim K"].every((n) => names.includes(n)), names.join("|"));
    ck("an admin is marked, the others are not", (await rowOf(page, "kim@example.com").getByTestId("person-admin").count()) === 1
       && (await rowOf(page, "alice@example.com").getByTestId("person-admin").count()) === 0);
    ck("signed in: last active", /^Last active/.test((await rowOf(page, "alice@example.com").getByTestId("person-state").innerText()).trim()));
    ck("never signed in: invited", (await rowOf(page, "kim@example.com").getByTestId("person-state").innerText()).trim() === "Invited, not signed in yet");
    ck("the add form is there, with Add", await page.getByTestId("people-add").isVisible()
       && (await page.getByTestId("people-submit").innerText()).trim() === "Add");
    await checkWords(page, ck, "admin");
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "factory-people-admin-1440.png"), fullPage: true });

    await page.getByTestId("people-email").fill("sam@example.com");
    await page.getByTestId("people-name").fill("Sam S");
    await page.getByTestId("people-submit").click();
    await page.getByTestId("people-note").waitFor({ timeout: 10000 });
    ck("adding says so", (await page.getByTestId("people-note").innerText()).trim() === "Added Sam S.");
    await rowOf(page, "sam@example.com").waitFor({ timeout: 10000 });
    ck("the new person is listed as invited", (await rowOf(page, "sam@example.com").getByTestId("person-state").innerText()).trim() === "Invited, not signed in yet");
    ck("the form is empty again", (await page.getByTestId("people-email").inputValue()) === "");
    await page.getByTestId("people-email").fill("sam@example.com");
    await page.getByTestId("people-submit").click();
    await page.getByText("sam@example.com is already on this cell.").waitFor({ timeout: 10000 });
    ck("the same email twice says so", true);
    await page.getByTestId("people-email").fill("");
    await page.getByTestId("people-submit").click();
    ck("no email: asks for one", await page.getByText("Enter an email address.").isVisible());
    await ctx.close();

    // ---- a read key
    const r = await open(alice.json.secret);
    ck("a read key: no form, and the page says why", (await r.page.getByTestId("people-add").count()) === 0
       && (await r.page.getByTestId("people-add-text").innerText()).trim() === "Only an admin can add people to this cell.");
    ck("a read key: the list still shows", (await r.page.getByTestId("person-row").count()) >= 4);
    await r.ctx.close();

    // ---- phone width
    const n = await open(bob.json.secret, 390);
    const over = await n.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ck("390: no sideways scroll", over <= 0, String(over));
    if (SHOTS) await n.page.screenshot({ path: path.join(SHOTS, "factory-people-admin-390.png"), fullPage: true });
    await n.ctx.close();
  } finally {
    await browser.close();
    try { d.kill(); } catch { /* gone */ }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
