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
 *  - TR-1088: a Tares Cloud cell (the workspace address is set) never says licence, key or enterprise and links
 *    nowhere: every place above says "... is not available on this workspace right now." on a cell with no
 *    licence and on one whose licence ended, and a self-hosted cell keeps its words and link exactly.
 *  - TR-1088 (#509 review): with /health slow no licence line shows until it answers, then a Tares Cloud cell
 *    shows the plain line and a self-hosted cell its words and link; with /health failing the words stay plain;
 *    a refusal answered before /health lands never says licence on a Tares Cloud cell. What request() carries out
 *    of a 402 before /health is checked without a browser in licence-words.e2e.cjs.
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
  // the words say "not available" until /health has said the cell is self-hosted: wait for them
  await page.waitForFunction((w) => (document.querySelector('[data-testid="licence-needed"]')?.textContent || "").includes(w),
                             `${what} needs an enterprise licence.`, { timeout: 5000 }).catch(() => undefined);
  const text = (await line.first().innerText()).trim();
  ck(`${label}: the licence line shows`, text.includes(`${what} needs an enterprise licence.`), text);
  const a = line.first().locator("a");
  ck(`${label}: it links to the enterprise licence page`, (await a.getAttribute("href")) === URL_ENTERPRISE
     && (await a.getAttribute("target")) === "_blank" && /noreferrer/.test((await a.getAttribute("rel")) || ""));
  const body = await page.locator("body").innerText();
  ck(`${label}: no raw answer on the page`, !body.includes('{"code"') && !body.includes("licence_required"), body.slice(0, 300));
}

// TR-1088: on a Tares Cloud cell the line is plain words, with no link, and nothing on the page names a licence
const BAD = /licen[cs]e|enterprise|\bkey\b/i;
async function checkPlain(page, label, what) {
  const line = page.getByTestId("licence-needed");
  await line.first().waitFor({ timeout: 10000 });
  await page.waitForTimeout(1500);   // /health has long answered: nothing may switch to licence words later
  const text = (await line.first().innerText()).trim();
  ck(`${label}: the plain line shows`, text === `${what} is not available on this workspace right now.`, text);
  ck(`${label}: the line names no licence, key or enterprise`, !BAD.test(text), text);
  ck(`${label}: the line has no link`, (await line.first().locator("a").count()) === 0);
  const all = await line.evaluateAll((els) => els.map((e) => e.textContent).join(" | "));
  ck(`${label}: no licence line anywhere says licence`, !BAD.test(all), all);
  ck(`${label}: no link to the enterprise page on the page`, (await page.locator('a[href*="docs.glassflow.ai/tares/enterprise"]').count()) === 0);
  const body = await page.locator("body").innerText();
  ck(`${label}: no licence words or raw answer in the page`, !/enterprise licence|needs a licence|licence_required|\{"code"/i.test(body), body.slice(0, 300));
}
async function checkPlainPage(page, label) {
  const note = page.getByTestId("licence-needed");
  await note.first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(1500);
  const text = (await note.first().innerText()).trim();
  ck(`${label}: the page note is plain`, text === "Teams are not available on this workspace right now.", text);
  ck(`${label}: it names no licence, key or enterprise`, !BAD.test(text), text);
  ck(`${label}: it has no link`, (await note.first().locator("a").count()) === 0);
  ck(`${label}: no link to the enterprise page on the page`, (await page.locator('a[href*="docs.glassflow.ai/tares/enterprise"]').count()) === 0);
}

// a home with one grant to agree to, so Agree on a cell without the team's part shows what is missing
const GRANT = { id: "grant-1", kind: "grant", label: "Grant", title: "Agree to merge to acme/web", detail: "Kim asks to merge.",
                meta: "", project: null, repo: null, from: { name: "Kim" }, since: null, waited: "1 minute",
                actions: [{ label: "Agree", primary: true, do: "agree", grant: "g1" }, { label: "Decline", primary: false, do: "decline", grant: "g1" }] };
async function fakeHome(page) {
  await page.route(/\/api\/home(\?|$)/, async (route) => {
    const res = await route.fetch();
    const data = await res.json();
    return route.fulfill({ response: res, json: { ...data, summary: "", counts: { mine: 0, team: 0 },
      waiting_for_you: [GRANT], waiting_total: 1, projects: [], you_are_waiting_on: [], crews: [], crews_quiet: [], areas: [] } });
  });
}

(async () => {
  let platform, factory, cloudBare, cloudLicensed, selfFactoryNone, selfPlatNone, cloudExpired, cloudPlatNone, cloudPlatExpired;
  try {
    platform = await startCell(PORT, "platform");
    factory = await startCell(PORT + 2, "factory");   // each takes its port and the next (MCP)
    cloudBare = await startCell(PORT + 4, "factory", "none", true);
    cloudLicensed = await startCell(PORT + 6, "factory", "valid", true);
    selfFactoryNone = await startCell(PORT + 8, "factory", "none");
    selfPlatNone = await startCell(PORT + 10, "platform", "none");
    cloudExpired = await startCell(PORT + 12, "factory", "expired", true);
    cloudPlatNone = await startCell(PORT + 14, "platform", "none", true);
    cloudPlatExpired = await startCell(PORT + 16, "platform", "expired", true);
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
      await checkPlain(page, "workspace, no licence", "Inviting people");
      ck("workspace, no licence: there is no invite form", (await page.locator("#ws-invite").count()) === 0);
      await page.context().close();
    }
    // Tares Cloud, licensed: the form shows, and a refused invite shows the line (plain words, TR-1088)
    {
      const page = await (await browser.newContext({ viewport: { width: 390, height: 900 } })).newPage();
      await fakeCloud(page, { status: 402, body: BODY });
      await page.goto(`${cloudLicensed.base}/settings?tab=workspace`);
      await page.locator("#ws-invite").waitFor({ timeout: 15000 });
      ck("workspace, licensed: no licence line before an invite", (await page.getByTestId("licence-needed").count()) === 0);
      await page.locator("#ws-invite").fill("sam@example.com");
      await page.locator("#ws-invite").press("Enter");
      await checkPlain(page, "workspace, licensed, refused", "Inviting people");
      await page.context().close();
    }
    // TR-1088: every other place a Tares Cloud cell could say licence says plain words, with no link, with no
    // licence and with a licence that ended; a self-hosted cell keeps its words and its link
    {
      const page = async (w = 1440) => (await browser.newContext({ viewport: { width: w, height: 900 } })).newPage();
      // People on a factory cell: the add slot of a cell with no licence
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudBare.base}/people`); await p.getByTestId("people-list").waitFor({ timeout: 15000 });
        await checkPlain(p, "cloud, no licence, People add slot", "Adding people"); await p.context().close(); }
      // the same at phone width
      { const p = await page(390); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudBare.base}/people`); await p.getByTestId("people-list").waitFor({ timeout: 15000 });
        await checkPlain(p, "cloud, no licence, People add slot @390", "Adding people"); await p.context().close(); }
      // Settings, Access on a factory cell with no licence (the one-person panel)
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudBare.base}/settings?tab=access`); await p.getByTestId("you-row").waitFor({ timeout: 15000 });
        await checkPlain(p, "cloud, no licence, Settings Access", "Adding people"); await p.context().close(); }
      // a page for teams only (Areas), on a platform cell with no licence
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudPlatNone.base}/settings?tab=areas`);
        await checkPlainPage(p, "cloud, no licence, Areas page"); await p.context().close(); }
      { const p = await page(390); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudPlatNone.base}/settings?tab=areas`);
        await checkPlainPage(p, "cloud, no licence, Areas page @390"); await p.context().close(); }
      // Settings, Access on a platform cell with no licence
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} });
        await p.goto(`${cloudPlatNone.base}/settings?tab=access`); await p.getByTestId("you-row").waitFor({ timeout: 15000 });
        await checkPlain(p, "cloud platform, no licence, Settings Access", "Adding people"); await p.context().close(); }
      // an agree the cell cannot take (the team's part is not there): the row says so in plain words
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} }); await fakeHome(p);
        await p.goto(`${cloudBare.base}/`, { waitUntil: "load" });
        await p.getByTestId("waiting-row").first().waitFor({ timeout: 15000 }); await p.waitForLoadState("networkidle");
        await p.getByRole("button", { name: "Agree" }).click();
        const err = p.getByTestId("row-error"); await err.waitFor({ timeout: 10000 });
        const words = (await err.innerText()).trim();
        ck("cloud, no licence, Agree: the row says it is not available", words === "That is not available on this workspace right now.", words);
        ck("cloud, no licence, Agree: no licence, key or enterprise", !BAD.test(words), words);
        ck("cloud, no licence, Agree: no link to the enterprise page", (await p.locator('a[href*="docs.glassflow.ai/tares/enterprise"]').count()) === 0);
        await p.context().close(); }
      // a licence that ended: the invite the workspace refuses (People, Settings Workspace)
      { const p = await page(); await fakeCloud(p, { status: 402, body: BODY });
        await p.goto(`${cloudExpired.base}/people`); await p.getByTestId("people-email").waitFor({ timeout: 15000 });
        await p.getByTestId("people-email").fill("sam@example.com"); await p.getByTestId("people-submit").click();
        await checkPlain(p, "cloud, ended licence, People invite refused", "Inviting people"); await p.context().close(); }
      { const p = await page(390); await fakeCloud(p, { status: 402, body: BODY });
        await p.goto(`${cloudExpired.base}/settings?tab=workspace`); await p.locator("#ws-invite").waitFor({ timeout: 15000 });
        await p.locator("#ws-invite").fill("sam@example.com"); await p.locator("#ws-invite").press("Enter");
        await checkPlain(p, "cloud, ended licence, Workspace invite refused", "Inviting people"); await p.context().close(); }
      // a licence that ended: an add the cell itself refuses (402 on POST /api/people), as the cell words it
      { const p = await page(); await fakeCloud(p, { status: 200, body: {} }); await refuse(p, "POST", "**/api/people");
        await p.goto(`${cloudPlatExpired.base}/settings?tab=access`);
        await p.getByRole("button", { name: "Add person" }).first().click();
        await p.getByRole("textbox", { name: "name", exact: true }).fill("Sam S");
        await p.getByRole("textbox", { name: "email", exact: true }).fill("sam@example.com");
        await p.getByRole("button", { name: "Add person" }).last().click();
        await checkPlain(p, "cloud, ended licence, Settings Access add refused", "Adding people"); await p.context().close(); }
      // self-hosted, no licence: the words and the link stay
      { const p = await page(); await p.goto(`${selfFactoryNone.base}/people`); await p.getByTestId("people-list").waitFor({ timeout: 15000 });
        await checkLine(p, "self-hosted, no licence, People add slot", "Adding people"); await p.context().close(); }
      { const p = await page(); await p.goto(`${selfPlatNone.base}/settings?tab=access`); await p.getByTestId("you-row").waitFor({ timeout: 15000 });
        await checkLine(p, "self-hosted, no licence, Settings Access", "Adding people"); await p.context().close(); }
      { const p = await page(); await p.goto(`${selfPlatNone.base}/settings?tab=areas`);
        const note = p.getByTestId("licence-needed"); await note.first().waitFor({ timeout: 15000 });
        await p.waitForFunction(() => /Teams need an enterprise licence/.test(document.querySelector('[data-testid="licence-needed"]')?.textContent || ""), null, { timeout: 5000 }).catch(() => undefined);
        const text = (await note.first().innerText()).trim();
        ck("self-hosted, no licence, Areas page: the words stay", /Teams need an enterprise licence/.test(text) && /This page is for cells with more than one person\./.test(text), text);
        const a = note.first().locator("a");
        ck("self-hosted, no licence, Areas page: the link stays", (await a.getAttribute("href")) === URL_ENTERPRISE && (await a.innerText()) === "About the enterprise licence");
        await p.context().close(); }
      { const p = await page(); await fakeHome(p);
        await p.goto(`${selfFactoryNone.base}/`, { waitUntil: "load" });
        await p.getByTestId("waiting-row").first().waitFor({ timeout: 15000 }); await p.waitForLoadState("networkidle");
        await p.getByRole("button", { name: "Agree" }).click();
        const err = p.getByTestId("row-error"); await err.waitFor({ timeout: 10000 });
        const words = (await err.innerText()).trim();
        ck("self-hosted, no licence, Agree: the row keeps its words", words === "That needs an enterprise licence.", words);
        await p.context().close(); }
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
    // /health slow or failing (#509 F3, F4): until /health has answered the console does not know which kind of cell
    // it is, so no licence line shows (nothing flashes the wrong words); a Tares Cloud cell never says licence at
    // any moment; a self-hosted cell gets its words and link once /health lands; a failed /health stays plain
    {
      const SAY = /enterprise licence|needs a licence|licence_required|\{"code"/i;
      const HEALTH = /\/health(\?|$)/;
      const slowHealth = (page, ms) => page.route(HEALTH, async (route) => { await new Promise((r) => setTimeout(r, ms)); return route.continue(); });
      // sample the page at the given times after load; every sample must pass `ok`
      async function samples(page, label, times, ok) {
        let last = 0;
        for (const t of times) {
          await page.waitForTimeout(t - last); last = t;
          const lines = await page.getByTestId("licence-needed").evaluateAll((els) => els.map((e) => e.textContent));
          const links = await page.locator('a[href*="docs.glassflow.ai/tares/enterprise"]').count();
          const body = await page.locator("body").innerText();
          const bad = ok({ lines, links, body });
          ck(`${label}, at ${t} ms: ${bad ? "FAILED: " + bad : "as expected"}`, !bad, JSON.stringify({ lines, links }));
        }
      }
      const never = ({ lines, links, body }) => lines.some((l) => BAD.test(l)) ? "a line names a licence" : links ? "a link to the enterprise page"
        : SAY.test(body) ? "the page says licence" : "";
      const pendingQuiet = (s) => never(s) || (s.lines.length ? "a line shows before /health answered" : "");

      // Tares Cloud, /health slow: never the licence words, no line while pending, then the plain line
      for (const [label, base, route, ready] of [
        ["cloud, slow /health, People add slot", cloudBare.base, "/people", (p) => p.getByTestId("people-list")],
        ["cloud, slow /health, Settings Access", cloudBare.base, "/settings?tab=access", (p) => p.getByTestId("you-row")],
        ["cloud, slow /health, Areas page", cloudPlatNone.base, "/settings?tab=areas", null],
      ]) {
        const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await fakeCloud(p, { status: 200, body: {} }); await slowHealth(p, 3500);
        await p.goto(base + route, { waitUntil: "commit" });
        if (ready) await ready(p).waitFor({ timeout: 15000 });
        await samples(p, label, [300, 1500, 3000], pendingQuiet);
        await samples(p, label + ", after it landed", [6500], (s) => never(s)
          || (s.lines.length ? (s.lines.every((l) => / (is|are) not available on this workspace right now\./.test(l)) ? "" : "a line is not the plain one") : "no line"));
        await p.context().close();
      }
      // a self-hosted cell, /health slow: no line while pending, then the words and the link
      for (const [label, base, route, ready, want] of [
        ["self-hosted, slow /health, People add slot", selfFactoryNone.base, "/people", (p) => p.getByTestId("people-list"), "Adding people needs an enterprise licence."],
        ["self-hosted, slow /health, Settings Access", selfPlatNone.base, "/settings?tab=access", (p) => p.getByTestId("you-row"), "Adding people needs an enterprise licence."],
      ]) {
        const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await slowHealth(p, 3500);
        await p.goto(base + route, { waitUntil: "commit" });
        await ready(p).waitFor({ timeout: 15000 });
        await samples(p, label, [300, 1500, 3000], (s) => s.lines.length ? "a line shows before /health answered" : s.links ? "a link before /health answered" : "");
        await p.getByTestId("licence-needed").first().waitFor({ timeout: 10000 });
        const text = (await p.getByTestId("licence-needed").first().innerText()).trim();
        ck(`${label}, after it landed: the words`, text.includes(want), text);
        ck(`${label}, after it landed: the link`, (await p.locator(`a[href="${URL_ENTERPRISE}"]`).count()) >= 1);
        await p.context().close();
      }
      // /health fails: the cell is not known to be self-hosted, so the words stay plain on either kind of cell
      for (const [label, base, route, ready, what] of [
        ["cloud, failing /health, People add slot", cloudBare.base, "/people", (p) => p.getByTestId("people-list"), "Adding people"],
        ["self-hosted, failing /health, People add slot", selfFactoryNone.base, "/people", (p) => p.getByTestId("people-list"), "Adding people"],
      ]) {
        const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await fakeCloud(p, { status: 200, body: {} });
        await p.route(HEALTH, (route) => route.abort());
        await p.goto(base + route, { waitUntil: "commit" });
        await ready(p).waitFor({ timeout: 15000 });
        await samples(p, label, [500, 3000], never);
        const lines = await p.getByTestId("licence-needed").evaluateAll((els) => els.map((e) => e.textContent));
        ck(`${label}: when a line shows it is the plain one`, lines.every((l) => l === `${what} is not available on this workspace right now.`), JSON.stringify(lines));
        await p.context().close();
      }
      // a refusal answered BEFORE /health lands (a licence that ended, Add person at once): nothing says licence at
      // any moment on a Tares Cloud cell (the same case on a self-hosted cell is in licence-words.e2e.cjs)
      {
        const addRefused = async (p, base) => {
          await refuse(p, "POST", "**/api/people"); await slowHealth(p, 4000);
          await p.goto(`${base}/settings?tab=access`, { waitUntil: "commit" });
          await p.getByRole("button", { name: "Add person" }).first().click({ timeout: 15000 });
          await p.getByRole("textbox", { name: "name", exact: true }).fill("Sam S");
          await p.getByRole("textbox", { name: "email", exact: true }).fill("sam@example.com");
          await p.getByRole("button", { name: "Add person" }).last().click();
        };
        const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
        await fakeCloud(p, { status: 200, body: {} });
        await addRefused(p, cloudPlatExpired.base);
        await samples(p, "cloud, refusal before /health", [300, 1500, 3000], pendingQuiet);
        await samples(p, "cloud, refusal before /health, after it landed", [6000], (s) => never(s)
          || (s.lines.length === 1 && /^Adding people is not available on this workspace right now\.$/.test(s.lines[0].trim()) ? "" : "not the plain line: " + JSON.stringify(s.lines)));
        await p.context().close();
      }
    }
  } finally {
    await browser.close();
    for (const s of [platform, factory, cloudBare, cloudLicensed, selfFactoryNone, selfPlatNone, cloudExpired, cloudPlatNone, cloudPlatExpired]) if (s) s.stop();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
