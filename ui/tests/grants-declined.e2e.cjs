/*
 * Browser e2e for TR-834 and TR-929: the Grants page never lists a person who declined a grant under
 * "agreed by", and the team parts of the project pages (agreed by, Consent, grants waiting in other
 * projects, the repos a grant names, "Questions for the team", "Early hand-overs") come from src/ee,
 * only on a cell whose licence allows enterprise.
 *   - licensed cell: Kim, who agreed, is under "agreed by" and Lee, who declined, is not; a grant
 *     that waits for the caller has Consent (and Consent posts); the grants of other projects that
 *     wait for the caller are listed; the Add form names repos; the overview shows the questions
 *     and the early hand-overs.
 *   - unlicensed cell: the grant list and Revoke work, there is no "agreed by" column, no Consent,
 *     no list of other projects, no repo field, no questions and no early hand-overs; no ee chunk
 *     is requested; adding a grant that the cell answers with 402 shows the licence line, not a
 *     red error.
 *
 * Scratch daemons start through tests/licensed.cjs (auth on), each on a fresh data directory, on
 * TARES_E2E_PORT (default 8860) and the ports after it (daemon + MCP, two each).
 * Run:  npm run build && TARES_E2E_PORT=8860 node tests/grants-declined.e2e.cjs   (from ui/)
 * TARES_E2E_CHROME points Playwright at another Chrome binary. TARES_E2E_SHOTS=<dir> saves screenshots.
 */
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8860);
const ROOT = "root-token-grants-declined-e2e";
const SHOTS = process.env.TARES_E2E_SHOTS || "";
const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;

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
      res.on("end", () => { let j = null; try { j = JSON.parse(d); } catch { /* not json */ } resolve({ status: res.statusCode, body: d, json: j }); });
    });
    r.on("error", reject); r.write(data); r.end();
  });
}

const gone = (url) => new Promise((res) => http.get(url, (r) => { r.resume(); res(false); }).on("error", () => res(true)));
async function stopAndWait(d) {
  d.stop();
  for (let i = 0; i < 100 && !(await gone(`${d.base}/health`)); i++) await new Promise((r) => setTimeout(r, 200));
}

/** One cell with a person who leads a project; returns what the page needs. A cell without a licence
 *  is set up with one and restarted without it (a person cannot be added to a cell that has none). */
async function cell(port, licence) {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "tares-grants-e2e-"));
  let d = await startDaemon({ port, dataDir, edition: "platform", licence: "valid", auth: ROOT });
  const rootH = { authorization: `Bearer ${ROOT}` };
  let r = await call(d.base, "POST", "/api/keys", { name: "alex-laptop", scopes: ["admin"], person: { email: "alex-gd@example.com", name: "alex" } }, rootH);
  const key = r.json && r.json.secret;
  r = await call(d.base, "POST", "/api/projects", { template: "custom", name: "shop", objects: [], kind: "software_factory" }, { authorization: `Bearer ${key}` });
  const uid = r.json && r.json.id;
  ck(`${licence}: a person with a key leads a project`, !!key && !!uid, r.body);
  if (licence !== "valid") {
    await stopAndWait(d);
    await new Promise((res) => setTimeout(res, 500));   // the database file is released
    d = await startDaemon({ port, dataDir, edition: "platform", licence, auth: ROOT });
  }
  return { d, key, uid };
}

const grantBody = (uid, extra = false) => {
  const at = new Date().toISOString();
  const grant = (id, words, consents, extra = {}) => ({ id, kind: "project", project: uid, words, state: "waiting", given_at: at,
    given_by: { id: "p-alex", name: "Alex" }, holder: null, areas: ["example-org/docs"], consents, waiting_for: [], in_effect: false, ...extra });
  const mine = grant("G5", "Builders may tag releases", [], { waiting_for: [{ id: "p-me", name: "alex" }] });
  const other = grant("G4", "Builders may touch the website", [], { project: "p-elsewhere", waiting_for: [{ id: "p-me", name: "alex" }] });
  return { all: "", project: "", all_id: null, project_id: null, waiting_for_me: [mine, other], known_words: [], grants: [
    grant("G1", "Builders may change the docs", [{ id: "p-kim", name: "Kim", at }, { id: "p-lee", name: "Lee", at, declined: true, reason: "not ready" }]),
    grant("G2", "Builders may delete old docs", [{ id: "p-lee", name: "Lee", at, declined: true }]),
    mine,
    // a grant that waits and names nobody (what a cell without a licence sends): it must read "Waiting", never "Waiting for no one"
    ...(extra ? [grant("G6", "Builders may update the readme", [])] : []),
  ] };
};

const questions = { questions: [{ id: "Q1", to_person_id: "p-kim", to_person: { id: "p-kim", name: "Kim" }, repo: "example-org/docs",
  text: "May the docs build change?", asked_by: "spec", asked_at: new Date().toISOString(), answer: null, answered_by: null,
  answered_at: null, state: "open" }] };

async function newPage(browser, c, width = 1400, extra = false) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } });
  await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, c.key);
  const page = await ctx.newPage();
  page.eeRequests = []; page.errors = [];
  page.on("request", (r) => { if (EE_CHUNK.test(r.url())) page.eeRequests.push(r.url()); });
  page.on("pageerror", (e) => page.errors.push(String(e)));
  await page.route("**/api/grants?*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(grantBody(c.uid, extra)) }));
  await page.route("**/api/projects/*/questions", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(questions) }));
  // the crew answer of the real daemon, with one early hand-over added
  await page.route("**/api/projects/*/crew", async (route) => {
    try {
      const res = await route.fetch();
      let j = {}; try { j = await res.json(); } catch { /* none */ }
      j.handover = { ...(j.handover || {}), repo: null, repos: [], autonomy: null, by: null, at: new Date().toISOString(),
        overrides: [{ reason: "the docs owner is away", at: new Date().toISOString(), by_label: "alex" }] };
      await route.fulfill({ response: res, json: j });
    } catch { /* the page closed while the poll was in flight */ }
  });
  return { ctx, page };
}

(async () => {
  const daemons = [];
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
  const shot = async (page, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); } };
  try {
    // ── licensed ──
    {
      const c = await cell(PORT, "valid"); daemons.push(c.d);
      const { ctx, page } = await newPage(browser, c);
      await page.goto(`${c.d.base}/projects/${c.uid}?view=grants`);
      const row = (words) => page.locator("tr", { hasText: words });
      await row("change the docs").waitFor({ timeout: 15000 });
      await page.locator("th", { hasText: "agreed by" }).first().waitFor({ timeout: 15000 });   // the ee chunk is in
      // columns: grant, kind, given by, agreed by, state, actions
      const agreedBy = async (words) => (await row(words).locator("td").nth(3).innerText()).trim();
      const agreed1 = await agreedBy("change the docs");
      ck("licensed: Kim, who agreed, is under agreed by", /Kim/.test(agreed1), agreed1);
      ck("licensed: Lee, who declined, is not", !/Lee/.test(agreed1), agreed1);
      const agreed2 = await agreedBy("delete old docs");
      ck("licensed: a grant only declined has nobody under agreed by", agreed2 === "", JSON.stringify(agreed2));
      ck("licensed: the agreed by header is there", (await page.locator("th", { hasText: "agreed by" }).count()) === 2);   // the list and the other projects
      ck("licensed: Consent is on the grant that waits for the caller", (await row("tag releases").getByRole("button", { name: "Consent" }).count()) === 1);
      ck("licensed: a waiting grant names who it waits for", /Waiting for alex/i.test(await row("tag releases").innerText()), await row("tag releases").innerText());
      ck("licensed: no Consent on a grant that waits for no one here", (await row("change the docs").getByRole("button", { name: "Consent" }).count()) === 0);
      ck("licensed: grants of other projects that wait for the caller are listed",
        (await page.getByText("Waiting for your yes in other projects").count()) === 1 && (await row("touch the website").count()) === 1);
      let consented = "";
      await page.route("**/api/grants/*/consent", (route) => { consented = route.request().url(); route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ grant: {} }) }); });
      await row("tag releases").getByRole("button", { name: "Consent" }).click();
      for (let i = 0; i < 30 && !consented; i++) await new Promise((r) => setTimeout(r, 100));
      ck("licensed: Consent posts to the grant", /\/api\/grants\/G5\/consent$/.test(consented), consented);
      await page.getByRole("button", { name: "Add a grant" }).click();
      ck("licensed: the Add form names repos", (await page.getByText("repos it names").count()) === 1);
      await shot(page, "grants-licensed.png");
      await page.goto(`${c.d.base}/projects/${c.uid}`);
      await page.getByText("Questions for the team").waitFor({ timeout: 15000 });
      ck("licensed: the overview lists the questions", (await page.getByText("May the docs build change?").count()) === 1);
      await page.getByText("Early hand-overs").waitFor({ timeout: 15000 });
      ck("licensed: the overview lists the early hand-overs", (await page.getByText("the docs owner is away").count()) === 1);
      ck("licensed: no page error", page.errors.length === 0, page.errors.join("; "));
      await shot(page, "overview-licensed.png");
      await ctx.close();
      await stopAndWait(c.d);
    }
    // ── unlicensed ──
    {
      const c = await cell(PORT + 2, "none"); daemons.push(c.d);
      const { ctx, page } = await newPage(browser, c, 1400, true);
      await page.goto(`${c.d.base}/projects/${c.uid}?view=grants`);
      const row = (words) => page.locator("tr", { hasText: words });
      await row("change the docs").waitFor({ timeout: 15000 });
      // Revoke needs the caller's person id (usePeople), which arrives after the rows: wait for it
      await row("change the docs").getByRole("button", { name: "Revoke" }).waitFor({ state: "visible", timeout: 15000 });
      ck("unlicensed: the grants are listed", (await row("delete old docs").count()) === 1);
      ck("unlicensed: Revoke is there", (await row("change the docs").getByRole("button", { name: "Revoke" }).count()) === 1);
      ck("unlicensed: a waiting grant reads just Waiting, not Waiting for no one",
        (await page.getByText("Waiting for no one").count()) === 0 && (await page.getByText(/^Waiting$/).count()) >= 1,
        `${await page.getByText("Waiting for no one").count()} / ${await page.getByText(/^Waiting$/).count()}`);
      ck("unlicensed: no agreed by column", (await page.locator("th", { hasText: "agreed by" }).count()) === 0
        && (await row("change the docs").locator("td").count()) === 5);
      ck("unlicensed: no Consent", (await page.getByRole("button", { name: "Consent" }).count()) === 0);
      ck("unlicensed: no list of other projects", (await page.getByText("Waiting for your yes in other projects").count()) === 0
        && (await row("touch the website").count()) === 0);
      let addBody = "";
      await page.route("**/api/grants", (route) => route.request().method() !== "POST" ? route.fallback()
        : (addBody = route.request().postData() || "", route.fulfill({ status: 402, contentType: "application/json",
            body: JSON.stringify({ code: "licence_required", message: "Adding a grant needs an enterprise licence.", url: "https://docs.glassflow.ai/tares/enterprise" }) })));
      await page.getByRole("button", { name: "Add a grant" }).click();
      ck("unlicensed: no repo field", (await page.getByText("repos it names").count()) === 0);
      await page.getByPlaceholder(/what the crew may do/).fill("builders may merge");
      await page.getByRole("button", { name: "Add grant", exact: true }).click();
      await page.getByTestId("licence-needed").waitFor({ timeout: 10000 });
      ck("unlicensed: adding a grant sends no repos (areas: [])", (() => { try { const b = JSON.parse(addBody); return Array.isArray(b.areas) && b.areas.length === 0; } catch { return false; } })(), addBody);
      ck("unlicensed: a 402 shows the licence line with its link, not a red error",
        (await page.getByTestId("licence-needed").getByRole("link").count()) === 1 && (await page.locator(".alert.error").count()) === 0);
      await shot(page, "grants-unlicensed.png");
      await page.goto(`${c.d.base}/projects/${c.uid}`);
      await page.getByText("Docs", { exact: true }).first().waitFor({ timeout: 15000 });
      ck("unlicensed: no questions", (await page.getByText("Questions for the team").count()) === 0);
      ck("unlicensed: no early hand-overs", (await page.getByText("Early hand-overs").count()) === 0);
      ck("unlicensed: no ee chunk requested", page.eeRequests.length === 0, page.eeRequests.join(", "));
      ck("unlicensed: no page error", page.errors.length === 0, page.errors.join("; "));
      await shot(page, "overview-unlicensed.png");
      await ctx.close();
    }
  } catch (e) {
    ck("the run finished", false, String(e && e.stack || e));
  } finally {
    await browser.close();
    for (const d of daemons) d.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
