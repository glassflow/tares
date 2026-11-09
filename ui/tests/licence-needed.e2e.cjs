/*
 * Browser e2e for the licence line (TR-927). When the cell answers a team action with 402
 * `licence_required`, the console shows <LicenceNeeded> (a short line and a link to the enterprise
 * licence page) and never the raw answer.
 *  - Settings, People on a platform cell: Add person.
 *  - People on a factory cell: Add.
 *  - A licensed cell whose licence read fails: People and Settings Access say the cell did not answer, never that a
 *    licence is needed (#507 F1), at 1440 and 390, early and late.
 *  - Settings, Workspace on Tares Cloud: Invite. A cell with no licence shows the line in place of the invite
 *    form; a licensed one shows the form, and an invite the workspace refuses with 402 shows the line.
 * The control plane is made up too: the cell's workspace address is on its own origin and page.route answers it.
 * The answers are made up with page.route, so the suite does not depend on which routes the daemon
 * has moved out of core.
 *
 * Runs the real daemon, licensed (tests/licensed.cjs), on a scratch data directory and port, serving the built console.
 * Run:  npm run build && node tests/licence-needed.e2e.cjs
 * Needs: tares importable by the python at TARES_E2E_PY (default: the repo .venv, else python3). TARES_E2E_CHROME points Playwright at another Chrome binary when
 * its own browser download is missing.
 */
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8816);
const URL_ENTERPRISE = "https://docs.glassflow.ai/tares/enterprise";
const BODY = { code: "licence_required", message: "Adding people needs an enterprise licence.", url: URL_ENTERPRISE };

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

// Licensed daemons (tests/licensed.cjs): the add forms are the licence's, so they show only on a cell that has
// one; the 402 here is the licence running out while the page is open.
async function startCell(port, edition, licence = "valid", cloud = false) {
  const env = cloud ? { TARES_WORKSPACE_API_URL: `http://127.0.0.1:${port}/api/ws` } : undefined;
  const d = await startDaemon({ port, edition, licence, env });
  process.on("exit", () => d.stop());
  return d;
}

const refuse = (page, method, glob) => page.route(glob, (route) => route.request().method() === method
  ? route.fulfill({ status: 402, contentType: "application/json", body: JSON.stringify(BODY) })
  : route.continue());

// A made-up control plane for the Workspace tab: the overview, the members, and what an invite answers.
async function fakeCloud(page, inviteAnswer) {
  const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/ws", (route) => route.request().method() !== "GET" ? route.fallback() : json(route, 200, {
    slug: "acme", state: "running", plan: "team", image_tag: "1.55.0", storage_gb: 5, storage_max_gb: 50,
    storage_used_bytes: null, storage_pct_used: null, host: "acme.example.test", role: "owner",
    you: { email: "owner@example.com", name: "Owner" }, is_default: false, default_url: "http://127.0.0.1:9/api/ws/default",
  }));
  await page.route("**/api/ws/members", (route) => {
    if (route.request().method() === "GET") return json(route, 200, { members: [
      { user_id: "u1", email: "owner@example.com", name: "Owner", role: "owner", status: "active" }] });
    return json(route, inviteAnswer.status, inviteAnswer.body);
  });
}

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
  let platform, factory, cloudBare, cloudLicensed;
  try {
    platform = await startCell(PORT, "platform");
    factory = await startCell(PORT + 2, "factory");   // each takes its port and the next (MCP)
    cloudBare = await startCell(PORT + 4, "factory", "none", true);
    cloudLicensed = await startCell(PORT + 6, "factory", "valid", true);
  } catch (e) { console.log("FAIL daemons did not start: " + e.message); process.exit(1); }
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
    // Tares Cloud, no licence: the line replaces the invite form
    {
      const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
      await fakeCloud(page, { status: 200, body: {} });
      await page.goto(`${cloudBare.base}/settings?tab=workspace`);
      await page.getByRole("heading", { name: "Team" }).waitFor({ timeout: 15000 });
      await checkLine(page, "workspace, no licence", "Inviting people");
      ck("workspace, no licence: there is no invite form", (await page.locator("#ws-invite").count()) === 0);
      await page.context().close();
    }
    // Tares Cloud, licensed: the form shows, and a refused invite shows the line
    {
      const page = await (await browser.newContext({ viewport: { width: 390, height: 900 } })).newPage();
      await fakeCloud(page, { status: 402, body: BODY });
      await page.goto(`${cloudLicensed.base}/settings?tab=workspace`);
      await page.locator("#ws-invite").waitFor({ timeout: 15000 });
      ck("workspace, licensed: no licence line before an invite", (await page.getByTestId("licence-needed").count()) === 0);
      await page.locator("#ws-invite").fill("sam@example.com");
      await page.locator("#ws-invite").press("Enter");
      await checkLine(page, "workspace, licensed, refused", "Inviting people");
      await page.context().close();
    }
    // the licensed part cannot be loaded: say so, never "needs an enterprise licence" on a licensed cell
    {
      const EE_CHUNK = /\/assets\/ee-[^/]*\.js(\?|$)/;
      for (const [label, base, route, what] of [
        ["settings people", platform.base, "/settings?tab=access", "Adding people"],
        ["factory people", factory.base, "/people", "Adding people"],
      ]) {
        const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await page.route(EE_CHUNK, (r) => r.abort());
        await page.goto(base + route);
        const said = await page.getByTestId("ee-failed").first().waitFor({ timeout: 15000 }).then(() => true, () => false);
        ck(`chunk fails, ${label}: the page says this part did not load`, said
           && /did not load\. Reload the page/.test(await page.getByTestId("ee-failed").first().innerText()));
        ck(`chunk fails, ${label}: no licence line on a licensed cell`, (await page.getByTestId("licence-needed").count()) === 0
           && !(await page.locator("body").innerText()).includes(`${what} needs an enterprise licence`));
        await page.context().close();
      }
    }
    // the licence read fails on a licensed cell: the add part says the cell did not answer, never "needs an enterprise licence"
    {
      for (const [label, base, route, what] of [
        ["settings people", platform.base, "/settings?tab=access", "Adding people"],
        ["factory people", factory.base, "/people", "Adding people"],
      ]) {
        for (const w of [1440, 390]) {
          const page = await (await browser.newContext({ viewport: { width: w, height: 900 } })).newPage();
          await page.route(/\/api\/licence(\?|$)/, (r) => r.abort());
          await page.goto(base + route);
          const said = await page.getByTestId("licence-no-answer").first().waitFor({ timeout: 15000 }).then(() => true, () => false);
          ck(`licence read fails, ${label} @${w}: the page says the cell did not answer`, said
             && /The cell did not answer\./.test(await page.getByTestId("licence-no-answer").first().innerText()));
          for (const [when, ms] of [["early", 0], ["late", 6000]]) {
            if (ms) await page.waitForTimeout(ms);
            ck(`licence read fails, ${label} @${w}, ${when}: no licence line`, (await page.getByTestId("licence-needed").count()) === 0
               && !(await page.locator("body").innerText()).includes(`${what} needs an enterprise licence`));
          }
          await page.context().close();
        }
      }
    }
    // the licence read is slow or fails: a licensed workspace keeps its invite form, no licence line
    {
      for (const [label, slow] of [["slow", true], ["failing", false]]) {
        const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await fakeCloud(page, { status: 200, body: {} });
        await page.route(/\/api\/licence(\?|$)/, async (route) => {
          if (!slow) return route.abort();
          await new Promise((r) => setTimeout(r, 3500));
          return route.continue();
        });
        await page.goto(`${cloudLicensed.base}/settings?tab=workspace`);
        await page.getByRole("heading", { name: "Team" }).waitFor({ timeout: 15000 });
        await page.waitForTimeout(2000);
        ck(`workspace, licence read ${label}, at 2 s: the invite form shows`, (await page.locator("#ws-invite").count()) === 1);
        ck(`workspace, licence read ${label}, at 2 s: no licence line`, (await page.getByTestId("licence-needed").count()) === 0);
        await page.waitForTimeout(slow ? 3500 : 3000);
        ck(`workspace, licence read ${label}, later: still the form, no line`, (await page.locator("#ws-invite").count()) === 1
           && (await page.getByTestId("licence-needed").count()) === 0);
        await page.context().close();
      }
    }
  } finally {
    await browser.close();
    for (const s of [platform, factory, cloudBare, cloudLicensed]) s.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
