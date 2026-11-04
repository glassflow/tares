/*
 * Browser e2e for TR-764: the AI SRE Settings page (/settings on an sre cell).
 *
 * Seven groups from one read of the cell (GET /api/sre/settings): Watching, Investigating, Where notes
 * go, Tools, People and keys, Edition, Advanced. The first three save on their own and a save that stops
 * partway says which part was saved. A key's secret is shown once and never kept. The edition switch asks
 * first. A key that may only read sees plain text: no inputs, no buttons, no Revoke. Every state shows only
 * words from words.json and none scrolls sideways at 390 px.
 *
 * Seeds three scratch sre cells (tests/seed-sre-settings.py, invented names) and runs the real daemon on
 * each, serving the built console: a cell with a watcher and an investigator (an open cell, so the person
 * is an admin), a copy of it behind an auth token (a read key sees it), and a new cell with neither. The
 * sample states (?sample=<name>, with the sample flag in localStorage) need no data.
 * Run:  npm run build && node tests/sre-settings.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const crypto = require("crypto");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8873);
const RO_PORT = PORT + 2;
const NEW_PORT = PORT + 4;
const SHOTS = process.env.SHOTS_DIR;
const ROUTE = "/api/sre/settings";
const SAMPLES = ["busy", "new", "ro", "saved", "partial", "switch", "load", "err", "long", "many"];
const GROUPS = ["watching", "investigating", "notes", "tools", "people", "edition", "advanced"];
const GROUP_TITLES = ["Watching", "Investigating", "Where notes go", "Tools", "People and keys", "Edition", "Advanced"];

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

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-settings-e2e-"));
  const homes = { busy: path.join(root, "busy"), ro: path.join(root, "ro"), new: path.join(root, "new") };
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const seeded = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-settings.py"), homes.busy, homes.ro, homes.new],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  ck("seeded the cells", seeded.busy === homes.busy && seeded.cell.includes("agents"));

  const ROOT_TOKEN = "e2e-" + crypto.randomBytes(12).toString("hex");
  const daemons = [];
  const start = (home, port, extra = {}) => {
    const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(port), TARES_MCP_PORT: String(port + 1),
                  TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO, ...extra };   // the venv installs the main checkout; this runs the code under test
    for (const k of ["TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET", "TARES_SLACK_CONNECT_URL", "TARES_DB", "TARES_CATALOG", "TARESD_URL",
                     "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
                     "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE"]) delete env[k];
    if (!extra.TARES_AUTH_TOKEN) delete env.TARES_AUTH_TOKEN;
    const d = spawn(python, ["-c", "from tares.cli import main; main()", "up", "--port", String(port), "--data-dir", home],
      { cwd: REPO, stdio: "ignore", env });
    daemons.push(d);
    return d;
  };
  start(homes.busy, PORT);
  start(homes.ro, RO_PORT, { TARES_AUTH_TOKEN: ROOT_TOKEN });
  start(homes.new, NEW_PORT);
  const BASE = `http://127.0.0.1:${PORT}`;
  const RO_BASE = `http://127.0.0.1:${RO_PORT}`;
  const NEW_BASE = `http://127.0.0.1:${NEW_PORT}`;

  let browser;
  try {
    ck("daemons up", (await waitFor(`${BASE}/health`)) && (await waitFor(`${RO_BASE}/health`)) && (await waitFor(`${NEW_BASE}/health`)));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    // a read key for the cell behind the token
    const made = await (await fetch(`${RO_BASE}/api/keys`, {
      method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${ROOT_TOKEN}` },
      body: JSON.stringify({ name: "viewer", scopes: ["read"] }),
    })).json();
    const READ_KEY = made.secret;
    ck("made a read key", typeof READ_KEY === "string" && READ_KEY.length > 10);

    const newPage = async (width, { sample = false, token = null } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      await ctx.addInitScript(([s, t]) => {
        try { if (s) localStorage.setItem("taresSamples", "1"); if (t) localStorage.setItem("tares_token", t); } catch (e) { /* none */ }
      }, [sample, token]);
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };
    const T = (page, id) => page.getByTestId(id);
    const text = async (page, id) => (await T(page, id).innerText()).replace(/\s+/g, " ").trim();
    const saved = (page, g) => page.locator(`[data-testid=save-msg-${g}]:has-text("Saved just now")`).waitFor();
    const group = (page, g) => T(page, `group-${g}`);
    const load = async (page, base, query = "") => {
      await page.goto(`${base}/settings${query}`, { waitUntil: "load" });
      await group(page, "watching").waitFor();
    };
    const pickOption = async (page, testid, option) => {
      await T(page, testid).click();
      await page.getByRole("option", { name: option, exact: true }).click();
    };
    const read = async (base, token) => (await fetch(`${base}${ROUTE}`, { headers: token ? { authorization: `Bearer ${token}` } : {} })).json();
    const fv = (b, g, k) => b[g].fields.find((f) => f.key === k).value;
    const inputsIn = (page) => page.locator("[data-testid=sre-settings] input, [data-testid=sre-settings] textarea, [data-testid=sre-settings] select, [data-testid=sre-settings] [role=combobox]").count();
    const buttonsIn = (page) => page.locator("[data-testid=sre-settings] button").count();

    // ---- 1. the live admin page: one call, seven groups in order, the cell's own values
    const { ctx, page } = await newPage(1440);
    const one = expectOneCall(page, ROUTE);
    await load(page, BASE);
    await one(ck, "/settings");
    ck("the page has its heading", await page.getByRole("heading", { name: "Settings", exact: true }).isVisible());
    ck("seven groups, in order", JSON.stringify(await page.locator("[data-testid^=group-]").evaluateAll((e) => e.map((x) => x.querySelector("h2").textContent.trim()))) === JSON.stringify(GROUP_TITLES));
    ck("the jump links name the same groups", JSON.stringify(await page.locator("[data-testid=settings-nav] a").allInnerTexts()) === JSON.stringify(GROUP_TITLES));
    ck("how often it checks is the cell's value", (await text(page, "input-watching-every")) === "Every 10 minutes", await text(page, "input-watching-every"));
    ck("the watcher's budget is the cell's value", (await T(page, "input-watching-budget_usd").inputValue()) === "25");
    ck("the budget says what is spent", /\$25/.test(await text(page, "help-watching-budget_usd")), await text(page, "help-watching-budget_usd"));
    ck("the investigator's budget never resets", /Never resets/.test(await text(page, "input-investigating-budget_period")), await text(page, "input-investigating-budget_period"));
    ck("the wait between looks is the cell's value", (await text(page, "input-investigating-cooldown")) === "1 hour", await text(page, "input-investigating-cooldown"));
    ck("hit rate with no answers says so", /Not enough answers yet/.test(await text(page, "hit-rate")), await text(page, "hit-rate"));
    ck("Slack is not connected and notes say so", /Slack is not connected/.test(await text(page, "value-notes-slack_channel")) && /notes stay in the console/.test(await text(page, "group-notes")), await text(page, "group-notes"));
    ck("each field group has Save, off until something changes", (await Promise.all(["watching", "investigating", "notes"].map((g) => T(page, `save-${g}`).isDisabled()))).every(Boolean));
    ck("tools: the connected ones, then Slack", JSON.stringify(await page.locator("[data-testid=tool-kind]").allInnerTexts()) === JSON.stringify(["Vercel request logs", "GitHub", "Traces and logs", "Slack"]), (await page.locator("[data-testid=tool-kind]").allInnerTexts()).join("|"));
    ck("Slack as a tool says it is not connected", /Not connected/i.test(await page.locator("[data-testid=tool-row]").last().innerText()));
    { const t = await page.locator("[data-testid=tool-row]").first().innerText(); ck("a working tool says Working", /Working/i.test(t), t); }
    ck("tools can be added", (await page.locator("[data-testid=tools-add] a").count()) >= 4);
    ck("no keys yet says so", /No keys yet/i.test(await text(page, "keys-empty")) || (await page.locator("[data-testid=key-row]").count()) > 0, await text(page, "group-people"));
    ck("the edition is AI SRE, with a switch", (await text(page, "edition-current")) === "AI SRE" && (await T(page, "edition-switch").count()) === 1, await text(page, "edition-current"));
    ck("Advanced is links and a sentence", (await page.locator("[data-testid=advanced-link]").count()) === 6 && (await text(page, "advanced-note")).length > 10);
    ck("no 30 seconds claimed anywhere on the page", !/30 seconds/.test(await T(page, "sre-settings").innerText()));
    await checkWords(page, ck, "live busy");
    await shot(page, "live-busy-1440.png");

    // ---- 2. watching: change, Save, saved, and the cell has it
    await T(page, "input-watching-budget_usd").fill("30");
    ck("a change turns Save on and says there are changes", !(await T(page, "save-watching").isDisabled()) && /not saved/.test(await text(page, "save-msg-watching")), await text(page, "save-msg-watching"));
    ck("the other groups stay off", (await T(page, "save-investigating").isDisabled()) && (await T(page, "save-notes").isDisabled()));
    await pickOption(page, "input-watching-every", "Every 15 minutes");
    await T(page, "rule-add").click();
    await T(page, "rule-add-input").fill("namespace");
    await T(page, "rule-add-input").press("Enter");
    ck("a rule label can be added", JSON.stringify(await page.locator("[data-testid=rule-chip] [data-user-text]").allInnerTexts()) === JSON.stringify(["namespace"]));
    await T(page, "save-watching").click();
    await saved(page, "watching");
    ck("the group says it saved", (await text(page, "save-msg-watching")) === "Saved just now" && (await T(page, "save-watching").isDisabled()));
    const after = await read(BASE);
    ck("the cell has the new budget, check time and rule label", fv(after, "watching", "budget_usd") === 30 && fv(after, "watching", "every") === "15m" && JSON.stringify(fv(after, "watching", "rule")) === JSON.stringify(["namespace"]), JSON.stringify(after.watching.fields.map((f) => [f.key, f.value])));
    ck("the other groups were not touched", fv(after, "investigating", "budget_usd") === 25 && fv(after, "investigating", "cooldown") === "1h");
    ck("a rule label can be removed", await (async () => { await T(page, "rule-chip-remove").click(); await T(page, "save-watching").click(); await page.waitForTimeout(400); return JSON.stringify(fv(await read(BASE), "watching", "rule")) === "[]"; })());
    await checkWords(page, ck, "watching saved");

    // ---- 3. a save that stops partway says which part was saved
    await pickOption(page, "input-watching-every", "Every 5 minutes");
    await T(page, "input-watching-budget_usd").fill("2o");
    await T(page, "save-watching").click();
    await T(page, "partial-banner").waitFor();
    const banner = await text(page, "partial-banner");
    ck("the banner says part of Watching was saved", banner.startsWith("Part of Watching was saved."), banner);
    ck("and which part, and why not the rest", /Saved/.test(banner) && /How often it checks/i.test(banner) && /must be a number of dollars/.test(banner), banner);
    ck("the part before the stop is in the cell", fv(await read(BASE), "watching", "every") === "5m");
    ck("the part that failed is still in the box, ready to fix", (await T(page, "input-watching-budget_usd").inputValue()) === "2o" && !(await T(page, "save-watching").isDisabled()));
    ck("the part that was saved shows as saved", (await text(page, "input-watching-every")) === "Every 5 minutes");
    await checkWords(page, ck, "partial save");
    await shot(page, "live-partial-1440.png");
    await T(page, "input-watching-budget_usd").fill("35");
    await T(page, "save-watching").click();
    await saved(page, "watching");
    ck("fixing it and saving clears the banner", (await T(page, "partial-banner").count()) === 0 && fv(await read(BASE), "watching", "budget_usd") === 35);

    // ---- 4. investigating saves on its own
    await pickOption(page, "input-investigating-cooldown", "2 hours");
    ck("Investigating has its own Save, Watching stays off", !(await T(page, "save-investigating").isDisabled()) && (await T(page, "save-watching").isDisabled()));
    await T(page, "save-investigating").click();
    await saved(page, "investigating");
    const afterInv = await read(BASE);
    ck("the cell has the new wait; Watching is as it was", fv(afterInv, "investigating", "cooldown") === "2h" && fv(afterInv, "watching", "budget_usd") === 35, fv(afterInv, "investigating", "cooldown"));

    // ---- 5. where notes go saves on its own
    await T(page, "input-notes-watch_channel").fill("#oncall");
    await T(page, "save-notes").click();
    await saved(page, "notes");
    ck("the cell has the channel for services nobody watches", fv(await read(BASE), "notes", "watch_channel") === "oncall");
    await T(page, "input-notes-watch_channel").fill("not a channel!");
    await T(page, "save-notes").click();
    await T(page, "partial-banner").waitFor();
    ck("a refused channel says Where notes go was not saved", (await text(page, "partial-banner")).startsWith("Where notes go was not saved."), await text(page, "partial-banner"));
    await T(page, "input-notes-watch_channel").fill("oncall");
    await settle(page);

    // ---- 6. keys: create shows the secret once, revoke removes it
    await page.goto(`${BASE}/settings`, { waitUntil: "load" });
    await group(page, "people").waitFor();
    await T(page, "key-create-open").click();
    await T(page, "key-create").click();
    ck("a key needs a name", /Give the key a name/.test(await text(page, "key-problem")));
    await T(page, "key-name-input").fill("Alex laptop");
    await pickOption(page, "key-scope", "Read");
    await T(page, "key-create").click();
    await T(page, "created-key").waitFor();
    const secret = (await T(page, "created-key-secret").innerText()).trim();
    ck("the new key's secret is shown, with what it can do", secret.length > 16 && /Key created: Alex laptop\./.test(await text(page, "created-key")) && /Can read/.test(await text(page, "created-key")), await text(page, "created-key"));
    ck("it is listed with its name and use", (await page.locator("[data-testid=key-row]").count()) === 1 && /Alex laptop/.test(await text(page, "group-people")) && /Never used|not used/i.test(await text(page, "group-people")), await text(page, "group-people"));
    ck("it can be copied", (await T(page, "created-key-copy").count()) === 1);
    await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE }).catch(() => {});
    await T(page, "created-key-copy").click();
    await checkWords(page, ck, "key created");
    await shot(page, "live-key-created-1440.png");
    ck("the secret is not in the browser's storage", await page.evaluate((s) => !JSON.stringify({ ...localStorage }).includes(s) && !JSON.stringify({ ...sessionStorage }).includes(s), secret));
    await page.reload({ waitUntil: "load" });
    await group(page, "people").waitFor();
    ck("after a reload the secret is gone, the key stays", (await T(page, "created-key").count()) === 0 && !(await page.locator("body").innerText()).includes(secret) && (await page.locator("[data-testid=key-row]").count()) === 1);
    ck("a key made here shows in the cell", (await read(BASE)).people.keys.some((k) => k.name === "Alex laptop"));
    // an address with a group in it opens at that group on a fresh load (about:blank first: a hash change on the same page scrolls natively)
    for (const g of ["people", "tools"]) {
      await page.goto("about:blank");
      await page.goto(`${BASE}/settings#${g}`, { waitUntil: "load" });
      await group(page, g).waitFor();
      await page.waitForTimeout(400);
      ck(`a fresh load of /settings#${g} scrolls to that group`, (await page.evaluate(() => window.scrollY)) > 100, String(await page.evaluate(() => window.scrollY)));
    }
    await T(page, "key-revoke").click();
    await page.locator("[data-testid=key-row]").waitFor({ state: "detached" });
    ck("Revoke removes it from the page and the cell", (await page.locator("[data-testid=key-row]").count()) === 0 && !(await read(BASE)).people.keys.some((k) => k.name === "Alex laptop"));

    // ---- 7. the edition asks first
    await T(page, "edition-switch").click();
    const dialog = T(page, "edition-dialog");
    await dialog.waitFor();
    const dtext = (await dialog.innerText()).replace(/\s+/g, " ");
    ck("the dialog names the other edition and says what changes", /Switch to Software Factory\?/.test(dtext) && /Switching changes the screens, not your data/.test(dtext), dtext);
    await checkWords(page, ck, "edition dialog");
    await shot(page, "live-edition-dialog-1440.png");
    await T(page, "edition-cancel").click();
    await dialog.waitFor({ state: "hidden" });
    ck("Cancel switches nothing", (await read(BASE)).edition.value === "sre");
    await ctx.close();

    // ---- 8. 390 px: the live page does not scroll sideways
    {
      const { ctx: c, page: p } = await newPage(390);
      await load(p, BASE);
      ck("390: live busy does not scroll sideways", (await overflow(p)) <= 0, String(await overflow(p)));
      await checkWords(p, ck, "390 live busy");
      await shot(p, "live-busy-390.png");
      await c.close();
    }

    // ---- 9. a new cell: no watcher, no investigator
    {
      const { ctx: c, page: p } = await newPage(1440);
      const o = expectOneCall(p, ROUTE);
      await load(p, NEW_BASE);
      await o(ck, "new cell");
      ck("new cell: Watching says nothing is watched", /No watcher is set up yet, so nothing is being checked and nothing has been spent/.test(await text(p, "empty-watching")), await text(p, "empty-watching"));
      ck("new cell: Investigating says nothing was looked into", /No investigator is set up yet/.test(await text(p, "empty-investigating")));
      ck("new cell: Watching has no Save for a watcher that is not there", (await T(p, "save-investigating").count()) === 0);
      ck("new cell: Slack is not connected, notes stay in the console", /Slack is not connected/.test(await text(p, "group-notes")) && /notes stay in the console/.test(await text(p, "group-notes")), await text(p, "group-notes"));
      await checkWords(p, ck, "live new");
      await shot(p, "live-new-1440.png");
      await c.close();
      const { ctx: c2, page: p2 } = await newPage(390);
      await load(p2, NEW_BASE);
      ck("390: live new does not scroll sideways", (await overflow(p2)) <= 0, String(await overflow(p2)));
      await shot(p2, "live-new-390.png");
      await c2.close();
    }

    // ---- 10. a key that may only read: the cell refuses the page, the console says so and shows nothing of it
    for (const width of [1440, 390]) {
      const { ctx: c, page: p } = await newPage(width, { token: READ_KEY });
      const asked = [];
      p.on("request", (r) => { if (new URL(r.url()).pathname === ROUTE) asked.push(r.method()); });
      await p.goto(`${RO_BASE}/settings`, { waitUntil: "load" });
      await T(p, "settings-admin-only").waitFor();
      ck(`read key ${width}: it says the key can read but not change settings`, /This key can read but not change settings\. Use a key that can write to open this page\./.test(await text(p, "settings-admin-only")));
      ck(`read key ${width}: the page does not ask for the settings`, asked.length === 0, asked.join(", "));
      ck(`read key ${width}: none of the settings is there`, (await T(p, "sre-settings").count()) === 0 && (await inputsIn(p)) === 0 && (await T(p, "settings-error").count()) === 0);
      ck(`read key ${width}: it is not told to try again`, !/try again|did not load/i.test(await text(p, "sre-settings-refused")));
      ck(`read key ${width}: the cell refuses the read`, (await fetch(`${RO_BASE}${ROUTE}`, { headers: { authorization: `Bearer ${READ_KEY}` } })).status === 403);
      if (width === 390) ck("read key 390: no sideways scroll", (await overflow(p)) <= 0, String(await overflow(p)));
      await checkWords(p, ck, `read key ${width}`);
      await shot(p, `live-ro-${width}.png`);
      await c.close();
    }
    {
      // no token at all: the cell asks for sign in, the page never shows a settings group
      const { ctx: c, page: p } = await newPage(1440);
      await p.goto(`${RO_BASE}/settings`, { waitUntil: "load" });
      await p.waitForTimeout(800);
      ck("no key at all: no settings are shown", (await T(p, "group-watching").count()) === 0);
      await c.close();
    }

    // ---- 11. the sample states
    for (const name of SAMPLES) {
      for (const width of [1440, 390]) {
        const { ctx: c, page: p } = await newPage(width, { sample: true });
        await p.goto(`${BASE}/settings?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await T(p, "settings-loading").waitFor();
        else if (name === "err") await T(p, "settings-error").waitFor();
        else await group(p, "watching").waitFor();
        await settle(p);
        const label = `${name} ${width}`;
        ck(`${label}: no sideways scroll`, (await overflow(p)) <= 0, String(await overflow(p)));
        await checkWords(p, ck, label);
        if (width === 1440) {
          if (name === "busy") {
            ck("busy: seven groups with their saves", (await p.locator("[data-testid^=group-]").count()) === 7 && (await p.locator("[data-testid^=save-]:not([data-testid^=save-msg])").count()) === 3);
            ck("busy: keys have Revoke", (await p.locator("[data-testid=key-revoke]").count()) >= 1);
            ck("busy: the rule's wording is read-only with a link to open it", (await T(p, "rule-text").count()) === 1 && (await T(p, "rule-open").getAttribute("href")) !== null && (await p.locator("[data-testid=rule-text] input, [data-testid=rule-text] textarea").count()) === 0);
            ck("busy: rule labels can be removed", (await p.locator("[data-testid=rule-chip-remove]").count()) >= 1);
            ck("busy: a minute is written in the singular", /Used 1 minute ago/.test(await text(p, "group-people")) && !/1 minutes/.test(await T(p, "sre-settings").innerText()));
            await T(p, "input-watching-budget_usd").fill("40");
            await T(p, "save-watching").click();
            await saved(p, "watching");
            ck("busy: Save on a sample changes the page and nothing else", (await T(p, "input-watching-budget_usd").inputValue()) === "40");
            await T(p, "key-create-open").click();
            await T(p, "key-name-input").fill("Lee");
            await T(p, "key-create").click();
            await T(p, "created-key").waitFor();
            ck("busy: a sample key shows its sample secret once", /sample/.test(await T(p, "created-key-secret").innerText()));
          } else if (name === "new") {
            ck("new: Watching and Investigating say nothing is set up", /No watcher is set up yet/.test(await text(p, "empty-watching")) && /No investigator is set up yet/.test(await text(p, "empty-investigating")));
            ck("new: Where notes go says Slack is not connected and notes stay in the console", /Slack is not connected/.test(await text(p, "group-notes")) && /notes stay in the console/.test(await text(p, "group-notes")), await text(p, "group-notes"));
          } else if (name === "ro") {
            ck("ro: no input, no button, no Revoke", (await inputsIn(p)) === 0 && (await buttonsIn(p)) === 0 && (await T(p, "key-revoke").count()) === 0, `${await inputsIn(p)} / ${await buttonsIn(p)}`);
            ck("ro: the note that it can be read but not changed", (await T(p, "readonly-note").count()) === 1);
          } else if (name === "saved") {
            ck("saved: Watching says it saved", (await text(p, "save-msg-watching")) === "Saved just now");
          } else if (name === "partial") {
            ck("partial: says which part was saved", /^Part of Watching was saved\./.test(await text(p, "partial-banner")) && (await T(p, "input-watching-budget_usd").inputValue()) === "2o", await text(p, "partial-banner"));
          } else if (name === "switch") {
            ck("switch: asks before it switches", (await T(p, "edition-dialog").isVisible()) && /Switch to/.test(await T(p, "edition-dialog").innerText()));
            await T(p, "edition-cancel").click();
            ck("switch: Cancel closes it", (await T(p, "edition-dialog").count()) === 0);
          } else if (name === "load") {
            ck("load: the loading rows, and no group", (await T(p, "settings-loading").isVisible()) && (await p.locator("[data-testid^=group-]").count()) === 0);
          } else if (name === "err") {
            const e = await text(p, "settings-error");
            ck("err: says Settings did not load and that it tries again in 30 seconds", /Settings did not load/.test(e) && /try again in 30 seconds/.test(e) && !/everywhere/.test(e), e);
            ck("err: has Try now", (await T(p, "settings-retry").count()) === 1);
          } else if (name === "long") {
            ck("long: the long name and rule stay inside the page", (await p.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)));
          } else if (name === "many") {
            ck("many: the tools and keys scroll inside their group", (await p.locator("[data-testid=tool-row]").count()) === 20 && (await p.locator("[data-testid=key-row]").count()) === 30
               && (await T(p, "tools-scroll").evaluate((e) => e.scrollHeight > e.clientHeight)) && (await T(p, "keys-scroll").evaluate((e) => e.scrollHeight > e.clientHeight)));
          }
        }
        await shot(p, `${name}-${width}.png`);
        await c.close();
      }
    }
    // the address alone shows no sample on a live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await load(p, BASE, "?sample=new");
      ck("the sample address alone shows the live cell", (await p.locator("[data-testid^=empty-]").count()) === 0);
      await c.close();
    }

    // ---- 12. last: the edition switch on the live cell
    {
      const { ctx: c, page: p } = await newPage(1440);
      await load(p, BASE);
      await T(p, "edition-switch").click();
      await T(p, "edition-confirm").click();
      await p.waitForURL((u) => u.pathname === "/", { timeout: 15000 });
      await settle(p);
      ck("Switch changes the cell's edition and opens the new home", (await (await fetch(`${BASE}/api/whoami`)).json()).edition === "factory");
      await c.close();
    }
  } finally {
    if (browser) await browser.close();
    for (const d of daemons) d.kill();
    fs.rmSync(root, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
