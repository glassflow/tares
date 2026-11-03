/*
 * Browser e2e for TR-755: the edition chooser, the first screen of a cell that has no edition yet.
 *
 * Four scratch daemons, one after the other, each on an empty data directory under a temp dir (never
 * ~/.tares), started with every edition, auth and factory variable unset:
 *   A  a fresh cell: / lands on /welcome/edition, Software Factory is preselected, Continue lands on
 *      Home and whoami says factory and edition_needed false.
 *   B  a fresh cell: every state of the page at 1440 and 390 px (loading, default, AI SRE picked,
 *      saving, save failed, not an admin), the address of any page sends to the chooser, no menu;
 *      then AI SRE picked, a failed save keeps the choice, Retry saves, whoami says sre.
 *   C  a cell that already has projects (tests/seed-projects-list.py): the startup rule makes it
 *      platform, the chooser never shows, /welcome/edition lands on /.
 *   D  a cell started with TARES_EDITION=platform and no data: no chooser either.
 * Every state goes through checkWords; every page load through expectOneCall (the one read is
 * /api/whoami).
 *
 * Ports: TARES_E2E_PORT (default 8873) for the console and +1 for MCP, reused by each daemon in turn.
 * Run:  npm run test:choose-edition   (builds first: the daemon serves ui/dist; tares installed in ../.venv)
 * SHOTS_DIR keeps the screenshots (default a temp dir). TARES_E2E_CHROME points Playwright at another Chrome.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8873);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "tares-choose-edition-shots-"));
fs.mkdirSync(SHOTS, { recursive: true });
const WIDTHS = [390, 1440];   // the last one ends by saving a choice, which settles the cell

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 150) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const bin = path.join(REPO, ".venv/bin");
const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
const UNSET = ["TARES_AUTH_TOKEN", "TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
  "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE", "FACTORY_PROJECT", "TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET",
  "TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TARES_EDITION"];
const cleanEnv = (extra = {}) => {
  const env = { ...process.env, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO, ...extra };
  for (const k of UNSET) if (!(k in extra)) delete env[k];
  return env;
};

/** A scratch daemon on its own data directory; stop() ends only this one and waits for the port. */
async function startDaemon(home, extra = {}) {
  const env = cleanEnv({ TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, ...extra });
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  const up = await waitFor(`${BASE}/health`);
  return {
    up,
    async stop() {
      const gone = new Promise((res) => daemon.once("exit", res));
      daemon.kill();
      await Promise.race([gone, new Promise((r) => setTimeout(r, 5000))]);
      fs.rmSync(home, { recursive: true, force: true });
    },
  };
}

const newHome = (tag) => fs.mkdtempSync(path.join(os.tmpdir(), `tares-choose-edition-${tag}-`));
const whoami = () => new Promise((res, rej) => http.get(`${BASE}/api/whoami`, (r) => {
  let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
}).on("error", rej));

(async () => {
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "tares-choose-edition-"));
  let browser, current;
  try {
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width) => (await browser.newContext({ viewport: { width, height: 900 } })).newPage();

    // Loads an address with the one-read check, waits until the chooser (or wherever it lands) is quiet.
    const load = async (page, address, label) => {
      const one = expectOneCall(page, "/api/whoami");
      await page.goto(`${BASE}${address}`, { waitUntil: "load" });
      await page.locator("#root > *").first().waitFor();
      await one(ck, label);
    };
    // A platform console loads many reads of its own, so these pages are opened without the one-read check.
    const visit = async (page, address) => { await page.goto(`${BASE}${address}`, { waitUntil: "load" }); await settle(page); };
    const notChooser = (page) => path_(page) !== "/welcome/edition";
    const settle = async (page) => { await page.waitForLoadState("networkidle"); };
    const shot = (page, state, width) => page.screenshot({ path: path.join(SHOTS, `choose-edition-${state}-${width}.png`), fullPage: true });
    const lead = (page) => page.getByTestId("choose-lead");
    const card = (page, id) => page.getByTestId(`choice-${id}`);
    const checked = async (page, id) => (await card(page, id).getAttribute("aria-checked")) === "true";
    const path_ = (page) => new URL(page.url()).pathname;

    // ── A: a fresh cell, Software Factory preselected, Continue ──────────────────────────────
    current = await startDaemon(newHome("a"));
    ck("A: daemon up", current.up);
    {
      const w = await whoami();
      ck("A: a fresh cell has no edition yet", w.edition_needed === true, JSON.stringify(w));
      const page = await newPage(1440);
      await load(page, "/", "A: opening /");
      ck("A: / lands on /welcome/edition", path_(page) === "/welcome/edition", page.url());
      await lead(page).waitFor();
      ck("A: the heading asks which Tares this is", (await page.getByRole("heading", { level: 1 }).innerText()) === "Which Tares is this?");
      ck("A: Software Factory is selected", await checked(page, "factory"));
      ck("A: AI SRE is not", !(await checked(page, "sre")));
      await checkWords(page, ck, "A: default");
      await page.getByTestId("choose-continue").click();
      await page.waitForURL((u) => u.pathname === "/");
      await settle(page);
      ck("A: Continue lands on Home", path_(page) === "/", page.url());
      const after = await whoami();
      ck("A: whoami says factory", after.edition === "factory", after.edition);
      ck("A: and the edition is no longer needed", after.edition_needed === false);
      ck("A: the page after Continue is the Software Factory home, with its menu", (await page.getByTestId("edition-menu").count()) > 0);
      await page.close();
    }
    await current.stop();

    // ── B: a fresh cell, every state, the failed save and Retry ──────────────────────────────
    current = await startDaemon(newHome("b"));
    ck("B: daemon up", current.up);
    for (const width of WIDTHS) {
      const page = await newPage(width);
      const at = `${width}px`;

      // loading: whoami is slow, the page keeps its shape
      await page.route("**/api/whoami", async (route) => { await new Promise((r) => setTimeout(r, 1200)); await route.continue(); });
      {
        const one = expectOneCall(page, "/api/whoami");
        await page.goto(`${BASE}/welcome/edition`, { waitUntil: "commit" });
        await lead(page).filter({ hasText: "Checking" }).waitFor();
        ck(`B: loading says Checking at ${at}`, (await lead(page).innerText()) === "Checking");
        ck(`B: loading shows two placeholder cards at ${at}`, (await page.getByTestId("choose-loading").locator("> *").count()) === 2);
        ck(`B: loading has no cards or buttons at ${at}`, (await page.getByRole("radio").count()) === 0 && (await page.getByRole("button").count()) === 0);
        await checkWords(page, ck, `B: loading ${at}`);
        if (width === 1440) await shot(page, "loading", width);
        await one(ck, `B: loading ${at}`);
        ck(`B: the page leaves loading for the chooser at ${at}`, (await page.getByRole("radio").count()) === 2);
      }
      await page.unroute("**/api/whoami");

      // default
      await load(page, "/welcome/edition", `B: default ${at}`);
      await lead(page).filter({ hasText: "Pick what you want" }).waitFor();
      ck(`B: lead says it can be changed later at ${at}`, (await lead(page).innerText()) === "Pick what you want to use it for. You can change this later in Settings.");
      ck(`B: the sentence about Settings appears once at ${at}`, ((await page.locator("body").innerText()).match(/You can change this later in Settings\./g) || []).length === 1);
      ck(`B: the cards are a radio group at ${at}`, (await page.getByRole("radiogroup", { name: "Which Tares is this?" }).count()) === 1);
      ck(`B: Software Factory is selected at ${at}`, await checked(page, "factory"));
      ck(`B: Software Factory has its sentence at ${at}`, /A crew of Claude Code sessions builds your projects; you review and decide\./.test(await card(page, "factory").innerText()));
      ck(`B: AI SRE has its sentence at ${at}`, /Agents watch your services and tell you what broke and why\./.test(await card(page, "sre").innerText()));
      ck(`B: no menu at ${at}`, (await page.getByTestId("edition-menu").count()) === 0 && (await page.locator("nav, aside, header").count()) === 0);
      const cards = await Promise.all([card(page, "factory"), card(page, "sre")].map((c) => c.boundingBox()));
      const cont = await page.getByTestId("choose-continue").boundingBox();
      if (width === 390) {
        ck("B: the cards stack at 390px", cards[1].y > cards[0].y + cards[0].height - 1 && Math.abs(cards[0].x - cards[1].x) < 1, JSON.stringify(cards));
        ck("B: Continue is full width at 390px", Math.abs(cont.width - cards[0].width) < 2, `${cont.width} vs ${cards[0].width}`);
        ck("B: nothing scrolls sideways at 390px", await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
      } else {
        ck("B: the cards sit side by side at 1440px", Math.abs(cards[0].y - cards[1].y) < 1 && cards[1].x > cards[0].x, JSON.stringify(cards));
      }
      const border = await card(page, "factory").evaluate((e) => getComputedStyle(e).borderTopWidth);
      ck(`B: the selected card has a 2px border at ${at}`, border === "2px", border);
      await checkWords(page, ck, `B: default ${at}`);
      await shot(page, "default", width);

      // AI SRE picked
      await card(page, "sre").click();
      ck(`B: picking AI SRE selects it at ${at}`, (await checked(page, "sre")) && !(await checked(page, "factory")));
      await checkWords(page, ck, `B: AI SRE picked ${at}`);
      await shot(page, "sre", width);
      await card(page, "sre").focus();
      await page.keyboard.press("ArrowLeft");
      ck(`B: the arrow keys move the choice at ${at}`, await checked(page, "factory"));

      // saving, then the failed save: the PUT is slow and then fails
      let puts = [];
      await page.route("**/api/settings/edition", async (route) => {
        if (route.request().method() !== "PUT") return route.continue();
        puts.push(route.request().postDataJSON());
        await new Promise((r) => setTimeout(r, 700));
        await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "boom" }) });
      });
      await load(page, "/welcome/edition", `B: saving ${at}`);
      await card(page, "sre").click();
      await page.getByTestId("choose-continue").click();
      ck(`B: while saving the button says Saving at ${at}`, (await page.getByTestId("choose-continue").innerText()) === "Saving");
      ck(`B: while saving the cards are locked at ${at}`, (await card(page, "sre").isDisabled()) && (await card(page, "factory").isDisabled()));
      ck(`B: while saving the button is locked at ${at}`, await page.getByTestId("choose-continue").isDisabled());
      await checkWords(page, ck, `B: saving ${at}`);
      await page.getByTestId("choose-error").waitFor();
      ck(`B: the PUT carried the choice at ${at}`, puts.length === 1 && puts[0].edition === "sre", JSON.stringify(puts));
      ck(`B: a failed save says so at ${at}`, /Your choice was not saved\. The cell did not answer\./.test(await page.getByTestId("choose-error").innerText()));
      ck(`B: the choice stays at ${at}`, await checked(page, "sre"));
      ck(`B: the cards unlock at ${at}`, !(await card(page, "sre").isDisabled()));
      ck(`B: the page stays on the chooser at ${at}`, path_(page) === "/welcome/edition");
      ck(`B: Retry is offered at ${at}`, (await page.getByRole("button", { name: "Retry" }).count()) === 1);
      await checkWords(page, ck, `B: save failed ${at}`);
      await shot(page, "error", width);

      await page.unroute("**/api/settings/edition");

      // not an admin: whoami reports a caller with only the read scope
      await page.route("**/api/whoami", async (route) => {
        const res = await route.fetch();
        const body = await res.json();
        await route.fulfill({ response: res, json: { ...body, id: "k-reader", name: "reader", scopes: ["read"] } });
      });
      await load(page, "/", `B: not an admin ${at}`);
      ck(`B: an address other than the chooser goes to it for a non-admin at ${at}`, path_(page) === "/welcome/edition", page.url());
      await lead(page).filter({ hasText: "An admin of this Tares" }).waitFor();
      ck(`B: a non-admin reads that an admin chooses first at ${at}`, (await lead(page).innerText()) === "An admin of this Tares chooses what it is for first.");
      ck(`B: and is told to ask at ${at}`, /Ask an admin to open this Tares and choose\. You will see it here once they have\./.test(await page.getByTestId("choose-wait").innerText()));
      ck(`B: a non-admin has no cards and no buttons at ${at}`, (await page.getByRole("radio").count()) === 0 && (await page.getByRole("button").count()) === 0);
      await checkWords(page, ck, `B: not an admin ${at}`);
      await shot(page, "nonadmin", width);
      await page.unroute("**/api/whoami");

      // any address sends a fresh cell to the chooser
      await load(page, "/projects?x=1", `B: deep link ${at}`);
      ck(`B: /projects goes to the chooser at ${at}`, path_(page) === "/welcome/edition", page.url());

      if (width === WIDTHS[WIDTHS.length - 1]) {
        // AI SRE picked, the save fails, the choice stays, Retry saves it and lands on Home
        await page.route("**/api/settings/edition", (route) => (route.request().method() === "PUT"
          ? route.fulfill({ status: 500, contentType: "application/json", body: "{}" }) : route.continue()));
        await load(page, "/welcome/edition", "B: Retry flow");
        await card(page, "sre").click();
        await page.getByTestId("choose-continue").click();
        await page.getByTestId("choose-error").waitFor();
        ck("B: after a failed save AI SRE is still picked", await checked(page, "sre"));
        await page.unroute("**/api/settings/edition");
        await page.getByRole("button", { name: "Retry" }).click();
        await page.waitForURL((u) => u.pathname === "/");
        await settle(page);
        const w = await whoami();
        ck("B: Retry saved AI SRE", w.edition === "sre" && w.edition_needed === false, JSON.stringify(w));
        ck("B: and Home is the AI SRE home, with its menu", (await page.getByTestId("edition-menu").count()) > 0 && /AI SRE/.test(await page.locator("body").innerText()));
      }
      await page.close();
    }
    await current.stop();

    // ── C: a cell that already has data is platform and never asks ────────────────────────────
    {
      const home = newHome("c");
      const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-projects-list.py"), home, String(PORT), String(PORT + 1), dist, tares],
        { cwd: REPO, encoding: "utf8", env: cleanEnv() }).trim().split("\n").pop());
      ck("C: seeded a cell with projects", Object.keys(ids).length > 1, String(Object.keys(ids).length));
      current = await startDaemon(home);
      ck("C: daemon up", current.up);
      const w = await whoami();
      ck("C: a cell with data is platform", w.edition === "platform" && w.edition_needed === false, JSON.stringify(w));
      const page = await newPage(1440);
      await visit(page, "/");
      ck("C: / opens the platform console, not the chooser", notChooser(page), page.url());
      ck("C: no chooser", (await page.getByTestId("choose-edition").count()) === 0);
      await visit(page, "/welcome/edition");
      ck("C: /welcome/edition leaves the chooser for the console", notChooser(page) && (await page.getByTestId("choose-edition").count()) === 0, page.url());
      await page.close();
      await current.stop();
    }

    // ── D: TARES_EDITION=platform on an empty cell never asks ────────────────────────────────
    current = await startDaemon(newHome("d"), { TARES_EDITION: "platform" });
    ck("D: daemon up", current.up);
    {
      const w = await whoami();
      ck("D: the cell is platform and not needed", w.edition === "platform" && w.edition_needed === false, JSON.stringify(w));
      const page = await newPage(1440);
      await visit(page, "/");
      ck("D: / opens the platform console, not the chooser", notChooser(page), page.url());
      ck("D: no chooser", (await page.getByTestId("choose-edition").count()) === 0);
      await visit(page, "/welcome/edition");
      ck("D: /welcome/edition leaves the chooser too", notChooser(page) && (await page.getByTestId("choose-edition").count()) === 0, page.url());
      await page.close();
    }
    await current.stop();
    current = null;
  } finally {
    if (browser) await browser.close();
    if (current) await current.stop();
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
  console.log(`screenshots in ${SHOTS}`);
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
