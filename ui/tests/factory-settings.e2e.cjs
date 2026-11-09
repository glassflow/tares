/*
 * Browser e2e for TR-738: the factory Settings page (/settings).
 *
 * The page has a two-level sidebar (Crew, Tools, Models, Team, Workspace, Advanced) in place of the
 * main menu, with a Back link, and shows one item at a time. Each change goes through the route that exists and is read back from the cell:
 * builders at once, the second check, pacing, how far the crew may go (merging on its own asks first),
 * a grant, the edition. A grant shows its words and, for "D79 A", the question it answers. A read key
 * sees the page with every control dimmed and disabled, and no way to switch the edition. Markup in a
 * grant stays text. Each load is one read of the cell. Every state shows only words from words.json
 * (a grant's own words and names are exempt) and none scrolls sideways.
 *
 * Seeds a scratch factory cell (tests/seed-factory-settings.py, invented names), then runs the real
 * daemon on it with a token, serving the built console. The sample states (?sample=<name>, with the
 * sample flag in localStorage) need no data.
 * Run:  npm run build && node tests/factory-settings.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of every state at 1440 and 390 px.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
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

const PORT = Number(process.env.TARES_E2E_PORT || 8855);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const TOKEN = "e2e-admin-token-settings";
const SAMPLES = ["busy", "newc", "ro", "rel", "long", "many", "load", "err"];
const GROUPS = ["Crew", "Tools", "Models", "Team", "Workspace", "Advanced"];

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const api = async (token, method, route, body) => {
  const r = await fetch(BASE + route, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
};

// waits until the cell says what a change should have made it say
const until = async (fn, tries = 40) => {
  for (; tries > 0; tries--) {
    if (await fn()) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
};

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-settings-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-settings.py"), home, String(PORT),
    String(PORT + 1), dist, tares], { cwd: REPO, encoding: "utf8" }).trim().split("\n").pop());
  ck("seeded the cell with grants, keys and tools", !!ids.project && !!ids.read_key);

  const env = { ...process.env, TARES_EDITION: "factory", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", TARES_AUTH_TOKEN: TOKEN, PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_DB", "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TARES_WORKSPACE_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, { sampleFlag = false, token = TOKEN } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      await ctx.addInitScript(([flag, t]) => {
        try { if (flag) localStorage.setItem("taresSamples", "1"); localStorage.setItem("tares_token", t); } catch (e) { /* none */ }
      }, [sampleFlag, token]);
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(150); };
    const crew = async () => (await api(TOKEN, "GET", "/api/crew/settings")).body;
    const text = (page, id) => page.getByTestId(id).innerText().then((t) => t.replace(/\s+/g, " ").trim());

    const go = async (page, tab) => {
      await page.goto(`${BASE}/settings?tab=${tab}`, { waitUntil: "load" });
      await page.getByTestId(`pane-${tab}`).waitFor();
      await settle(page);
    };
    const groupOpen = (page) => page.locator('[data-testid^="group-open-"][aria-expanded="true"]').evaluateAll((b) => b.map((x) => x.textContent.trim()));

    // 1. the page loads in one call, on the first item, with the sidebar in place of the main menu
    const { ctx, page } = await newPage(1440);
    const one = expectOneCall(page, "/api/factory/settings");
    await page.goto(`${BASE}/settings`, { waitUntil: "load" });
    await page.getByTestId("pane-crew").waitFor();
    await settle(page);
    await one(ck, "/settings");
    ck("the page has its heading", await page.getByRole("heading", { name: "Settings", exact: true }).isVisible());
    const heads = await page.locator('[data-testid^="group-open-"]').evaluateAll((b) => b.map((x) => x.textContent.trim()));
    ck("the groups are in order", heads.filter((h) => GROUPS.includes(h)).join() === GROUPS.join(), heads.join());
    ck("only the group of the item shown is open", (await groupOpen(page)).join() === "Crew", (await groupOpen(page)).join());
    ck("the first level opens to the second", (await page.getByTestId("nav-crew").getAttribute("aria-current")) === "page" && (await page.getByTestId("nav-allowed").isVisible()) && (await page.getByTestId("nav-github").count()) === 0);
    ck("the main menu is hidden in Settings", (await page.locator('nav[aria-label="Main"]').count()) === 0);
    ck("What you allowed says how many", (await text(page, "nav-grants")) === "10", await text(page, "nav-grants"));
    await page.getByTestId("group-open-models").click();
    await page.getByTestId("pane-providers").waitFor();
    ck("a group opens on its first item, and the other closes", (await groupOpen(page)).join() === "Models" && new URL(page.url()).searchParams.get("tab") === "providers", page.url());
    await page.getByTestId("nav-budget").click();
    await page.getByTestId("pane-budget").waitFor();
    ck("the second level changes the item, in the same group", (await groupOpen(page)).join() === "Models" && (await page.getByTestId("nav-budget").getAttribute("aria-current")) === "page");

    // Back goes where you came from
    await page.goto(`${BASE}/tickets`, { waitUntil: "load" });
    await page.getByTestId("menu-settings").click();
    await page.locator('[data-testid="settings-nav"] [data-testid="settings-back"]').waitFor();
    await page.locator('[data-testid="settings-nav"] [data-testid="settings-back"]').click();
    await page.waitForURL((u) => u.pathname === "/tickets");
    ck("Back returns to the page before Settings", new URL(page.url()).pathname === "/tickets" && (await page.locator('nav[aria-label="Main"]').count()) === 1, page.url());

    // 2. tools
    await go(page, "github");
    ck("GitHub is connected, as whom", (await text(page, "tool-github")).includes("Connected as example-org"), await text(page, "tool-github"));
    ck("a connected tool has a lit dot in the sidebar and an unconnected one does not", (await page.getByTestId("nav-dot").evaluateAll((d) => d.map((x) => x.getAttribute("data-on")))).join() === "true,false,false,true");
    ck("the GitHub page has one heading of that name and lists the accounts", (await page.getByRole("heading", { name: "GitHub", exact: true }).count()) === 1 && (await page.getByRole("heading", { name: "Accounts", exact: true }).count()) === 1);
    await go(page, "linear");
    ck("Linear is not connected, and its own form is there", /^Not connected/.test(await text(page, "tool-linear")) && (await page.getByTestId("pane-linear").getByRole("button", { name: "Connect" }).count()) === 1, await text(page, "tool-linear"));
    await go(page, "slack");
    ck("Slack is not connected", /^Not connected/.test(await text(page, "tool-slack")), await text(page, "tool-slack"));
    await go(page, "claude_code");
    ck("Claude Code is reporting, with how long ago", /^Reporting: last session .+ See how$/.test(await text(page, "tool-claude_code")), await text(page, "tool-claude_code"));
    ck("Claude Code goes to its source", (await page.getByTestId("tool-claude_code").locator("a").getAttribute("href")) === "/sources/claude-code");

    // 3. the crew starts as a new cell has it
    await go(page, "crew");
    ck("You approve each merge is chosen", (await page.getByTestId("how-L3-review").getAttribute("aria-checked")) === "true" && (await page.getByTestId("how-L4-ship").getAttribute("aria-checked")) === "false");
    ck("builders at once: 3", (await page.getByTestId("builders-count").innerText()) === "3" && /Up to 3 builders at once/.test(await text(page, "row-builders")));
    ck("second check and pacing are off", (await page.getByTestId("toggle-challenger").getAttribute("aria-checked")) === "false" && (await page.getByTestId("toggle-pace").getAttribute("aria-checked")) === "false");
    ck("there is no Releases row without a release profile", (await page.getByTestId("row-releases").count()) === 0);
    ck("what you allowed is not on this page", (await page.getByTestId("grant").count()) === 0);
    await checkWords(page, ck, "live page");
    await shot(page, "live-1440.png");
    await go(page, "keys");
    ck("Access keys lists the two keys, without the introduction", (await page.getByTestId("pane-keys").getByRole("heading", { name: "Access keys", exact: true }).count()) === 1 && /writer/.test(await text(page, "pane-keys")) && /reader/.test(await text(page, "pane-keys")) && !/Give each agent/.test(await text(page, "pane-keys")));
    await checkWords(page, ck, "access keys");
    await go(page, "crew");

    // 4. builders at once
    await page.getByTestId("builders-more").click();
    ck("builders to 4 shows at once", (await page.getByTestId("builders-count").innerText()) === "4");
    ck("builders to 4 persists", await until(async () => (await crew()).builders_max === 4), JSON.stringify(await crew()));
    await settle(page);
    ck("and the page still says 4 after reading again", (await page.getByTestId("builders-count").innerText()) === "4" && /Up to 4 builders at once/.test(await text(page, "row-builders")));

    // 5. second check and pacing
    await page.getByTestId("toggle-challenger").click();
    ck("the second check persists", await until(async () => (await crew()).challenger === true));
    await settle(page);
    ck("and its line says what it does", /A second model checks every commit/.test(await text(page, "row-challenger")), await text(page, "row-challenger"));
    await page.getByTestId("toggle-pace").click();
    ck("pacing on persists", await until(async () => (await crew()).pace === true));
    await settle(page);
    ck("and says it is on", /^Pacing Pacing is on/.test(await text(page, "row-pace")), await text(page, "row-pace"));
    await checkWords(page, ck, "pacing on");
    await page.getByTestId("toggle-pace").click();
    ck("pace off persists", await until(async () => (await crew()).pace === false));
    await settle(page);
    ck("and says it is off", /Pacing is off/.test(await text(page, "row-pace")));
    await page.getByTestId("toggle-challenger").click();
    ck("the second check off persists", await until(async () => (await crew()).challenger === false));

    // 6. merging on its own asks first
    await page.getByTestId("how-L4-ship").click();
    const ship = page.getByTestId("confirm-ship");
    await ship.waitFor();
    ck("L4-ship asks for confirmation, in words", /Let the crew merge on its own\?/.test(await ship.innerText()) && /without waiting for you/.test(await ship.innerText()));
    ck("and has not changed anything yet", (await crew()).autonomy === "L3-review");
    await checkWords(page, ck, "confirm ship");
    await shot(page, "live-confirm-ship-1440.png");
    await ship.getByRole("button", { name: "Keep approving" }).click();
    await ship.waitFor({ state: "hidden" });
    await settle(page);
    ck("Keep approving leaves it as it was", (await crew()).autonomy === "L3-review" && (await page.getByTestId("how-L3-review").getAttribute("aria-checked")) === "true");
    await page.getByTestId("how-L4-ship").click();
    await ship.waitFor();
    await ship.getByRole("button", { name: "Let it merge" }).click();
    ck("Let it merge persists L4-ship", await until(async () => (await crew()).autonomy === "L4-ship"));
    await settle(page);
    ck("and the page chooses it", (await page.getByTestId("how-L4-ship").getAttribute("aria-checked")) === "true");
    await page.getByTestId("how-L3-review").click();
    ck("going back to up to merge needs no confirmation", await until(async () => (await crew()).autonomy === "L3-review") && (await page.getByTestId("confirm-ship").count()) === 0);
    await settle(page);

    // 7. what you allowed
    await go(page, "allowed");
    const grants = page.getByTestId("grant");
    ck("all 10 grants are shown", (await grants.count()) === 10 && (await page.getByTestId("grants-more").count()) === 0, `${await grants.count()}`);
    const first = await grants.first().innerText();
    ck("the newest is first, in quotes, with its scope and when", /^“Builders may delete the merged branch number 6/.test(first) && /For every project, given today/.test(first), first);
    const d79 = grants.filter({ hasText: "D79 A" });
    ck("a grant that only says D79 A shows the question it answers", (await d79.count()) === 1 && /Answer to: Which branch should the next wave of pull requests target\?/.test(await d79.innerText()), await d79.innerText());
    ck("a project grant names the project", /In project-x, given today/.test(await grants.filter({ hasText: "Kim's builders" }).innerText()));
    const hostile = grants.filter({ hasText: "Lee may push" });
    ck("markup in a grant stays text", (await hostile.locator("b, img").count()) === 0 && (await hostile.innerText()).includes("<b>bold</b>") && (await hostile.innerText()).includes("<img src=x onerror=alert(1)>"));
    ck("a grant has no Revoke, there is no route for it", (await page.getByRole("button", { name: /revoke/i }).count()) === 0);
    ck("the long grants do not widen the page", (await overflow(page)) <= 0);
    await checkWords(page, ck, "all grants");
    await shot(page, "live-grants-1440.png");

    await page.getByTestId("grant-add").click();
    const gd = page.getByTestId("grant-dialog");
    await gd.waitFor();
    await gd.getByRole("button", { name: "Add grant" }).click();
    ck("an empty grant asks for words", /Write what the crew may do first\./.test(await gd.innerText()));
    await checkWords(page, ck, "grant dialog");
    await shot(page, "live-grant-dialog-1440.png");
    await gd.locator("textarea").fill("Alex may close stale pull requests in example-org/service-a");
    await gd.getByRole("button", { name: "Add grant" }).click();
    await gd.waitFor({ state: "hidden" });
    ck("an added grant reaches the cell", await until(async () => JSON.stringify((await api(TOKEN, "GET", "/api/factory/settings")).body.crew.grants.map((g) => g.text)).includes("close stale pull requests")));
    await settle(page);
    ck("and shows first, given today", /close stale pull requests/.test(await grants.first().innerText()) && /given today/.test(await grants.first().innerText()), await grants.first().innerText());

    // 8. the edition
    await go(page, "edition");
    ck("the edition is Software Factory, with the note", /Software Factory/.test(await text(page, "row-edition")) && /Switching changes the screens, not your data/.test(await text(page, "row-edition")));
    ck("an admin is offered the switch to AI SRE", (await text(page, "edition-sre")) === "Switch to AI SRE");
    await page.getByTestId("edition-sre").click();
    const ed = page.getByTestId("confirm-edition");
    await ed.waitFor();
    ck("the switch asks first, with the note", /Switch to AI SRE\?/.test(await ed.innerText()) && /not your data/.test(await ed.innerText()));
    await checkWords(page, ck, "confirm edition");
    await shot(page, "live-confirm-edition-1440.png");
    await ed.getByRole("button", { name: "Cancel" }).click();
    ck("Cancel leaves the edition alone", (await api(TOKEN, "GET", "/api/settings/edition")).body.edition === "factory");
    await page.getByTestId("edition-sre").click();
    await ed.waitFor();
    await ed.getByRole("button", { name: "Switch to AI SRE" }).click();
    ck("confirming switches the cell", await until(async () => (await api(TOKEN, "GET", "/api/settings/edition")).body.edition === "sre"));
    await settle(page);
    await api(TOKEN, "PUT", "/api/settings/edition", { edition: "factory" });

    // 9. Advanced has its own place in the sidebar
    await go(page, "agent_tools");
    ck("Advanced is open and lists its items", (await groupOpen(page)).join() === "Advanced" && (await page.getByTestId("nav-observability").isVisible()));
    for (const [id, href] of [["connections", "/sources"], ["explore", "/explore"], ["resources", "/resources"], ["connect", "/connect"], ["reads", "/reads"]]) {
      await go(page, "agent_tools");
      await page.getByTestId(`nav-${id}`).click();
      await page.waitForURL((u) => u.pathname === href);
      ck(`${id} reaches ${href}`, new URL(page.url()).pathname === href, page.url());
    }

    // 10. the old addresses still land on the right item
    for (const [address, id] of [["/settings#crew", "crew"], ["/settings#tools", "github"], ["/settings#models", "providers"], ["/settings?tab=anthropic", "providers"],
                                 ["/settings?tab=agents", "budget"], ["/settings?tab=access", "keys"], ["/settings/advanced?tab=observability", "observability"],
                                 ["/settings/advanced", "agent_tools"]]) {
      await page.goto(`${BASE}${address}`, { waitUntil: "load" });
      await page.getByTestId(`pane-${id}`).waitFor();
      ck(`${address} lands on ${id}`, new URL(page.url()).pathname === "/settings", page.url());
    }

    // 11. the GitHub App return and the Tares Cloud return
    {
      await page.goto(`${BASE}/settings?tab=github&installed=1`, { waitUntil: "load" });
      await page.getByTestId("pane-github").waitFor();
      ck("/settings?tab=github keeps the rest of its address", new URL(page.url()).searchParams.get("installed") === "1", page.url());
      await page.goto(`${BASE}/settings?cloud=github-connected`, { waitUntil: "load" });
      await page.getByTestId("pane-github").waitFor();
      ck("/settings?cloud= lands on GitHub and says what the cloud said", await page.getByText("GitHub is connected.", { exact: false }).first().isVisible());
      await page.waitForURL((u) => !u.searchParams.has("cloud"));
      ck("and the message is said once: the address drops it", new URL(page.url()).searchParams.get("tab") === "github", page.url());
    }
    await ctx.close();

    // 11. a read key: the cell refuses the page, the console says so and shows nothing of it
    {
      const n = await newPage(1440, { token: ids.read_key });
      const p = n.page;
      const asked = [];
      p.on("request", (r) => { if (new URL(r.url()).pathname === "/api/factory/settings") asked.push(r.method()); });
      await p.goto(`${BASE}/settings`, { waitUntil: "load" });
      await p.getByTestId("settings-admin-only").waitFor();
      await settle(p);
      ck("not an admin: the page does not ask for the settings", asked.length === 0, asked.join(", "));
      ck("not an admin: the page says this key can read but not change settings", /This key can read but not change settings\. Use a key that can write to open this page\./.test(await text(p, "settings-admin-only")));
      ck("not an admin: none of the page is there", (await p.getByTestId("factory-settings").count()) === 0 && (await p.getByTestId("section-crew").count()) === 0 && (await p.getByTestId("problem").count()) === 0 && (await p.getByRole("switch").count()) === 0);
      ck("not an admin: it is not told to try again", !/try again|did not load/i.test(await p.getByTestId("factory-settings-refused").innerText()));
      await checkWords(p, ck, "read key");
      await shot(p, "live-readonly-1440.png");
      await n.ctx.close();
    }

    // 12. narrow
    {
      const n = await newPage(390);
      const p = n.page;
      await go(p, "allowed");
      ck("390: the page does not scroll sideways, grants open", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      ck("390: no menu button, and Back is there", (await p.getByTestId("menu-button").count()) === 0 && (await p.getByTestId("settings-back").first().isVisible()));
      await p.getByTestId("settings-select").selectOption("keys");
      await p.getByTestId("pane-keys").waitFor();
      await settle(p);
      ck("390: the select changes the item, and a wide table stays in its card", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      await checkWords(p, ck, "390 live");
      await shot(p, "live-390.png");
      await n.ctx.close();
    }

    // 12b. a mid-size window: a wide table scrolls inside its card, not the page; a focused select
    // keeps one chevron; a checkbox sits apart from its label
    {
      const n = await newPage(1000);
      const p = n.page;
      for (const tab of ["keys", "github", "providers", "observability"]) {
        await go(p, tab);
        await p.locator(`[data-testid="pane-${tab}"] .panel`).first().waitFor();
        ck(`1000: ${tab} does not scroll sideways`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
      }
      await go(p, "observability");
      const sel = p.locator('[data-testid="pane-observability"] select').first();
      await sel.waitFor();
      await sel.focus();
      const rep = await sel.evaluate((e) => getComputedStyle(e).backgroundRepeat);
      ck("a focused select draws its chevron once", rep === "no-repeat", rep);
      await go(p, "agent_tools");
      const box = p.locator('[data-testid="pane-agent_tools"] label.field input[type=checkbox]').first();
      await box.waitFor();
      const gap = await box.evaluate((e) => Math.round(e.nextElementSibling.getBoundingClientRect().left - e.getBoundingClientRect().right));
      ck("the Agent tools checkbox sits apart from its label", gap >= 8, `${gap}px`);
      await n.ctx.close();
    }

    // 13. without the sample flag the sample address changes nothing
    {
      const n = await newPage(1440);
      await n.page.goto(`${BASE}/settings?sample=newc&tab=allowed`, { waitUntil: "load" });
      await n.page.getByTestId("pane-allowed").waitFor();
      await settle(n.page);
      ck("?sample=newc without the flag shows the cell's own settings", (await n.page.getByTestId("grant").count()) >= 10);
      await n.ctx.close();
    }

    // 14. every sample state at 1440 and 390: its words, no sideways scroll, a screenshot
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, { sampleFlag: true });
        const tag = `sample ${name} at ${width}`;
        let reads = 0;
        p.on("request", (r) => { if (r.url().includes("/api/factory/settings")) reads++; });
        const pane = ["busy", "newc", "long", "many"].includes(name) ? "allowed" : "crew";
        await p.goto(`${BASE}/settings?sample=${name}&tab=${pane}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("settings-loading").waitFor();
        else if (name === "err") await p.getByText("Settings did not load.").waitFor();
        else await p.getByTestId(`pane-${pane}`).waitFor();
        await settle(p);
        const body = await p.getByTestId("factory-settings").innerText();
        const panes = await p.locator('[data-testid^="pane-"]').count();
        if (name === "load") ck(`${tag}: it is the loading state, no item`, panes === 0 && (await p.getByTestId("settings-loading").isVisible()));
        else if (name === "err") ck(`${tag}: it says settings did not load and offers Retry`, /Settings did not load\./.test(body) && (await p.getByRole("button", { name: "Retry" }).count()) === 1 && panes === 0, body);
        else ck(`${tag}: one item is shown`, panes === 1, `${panes}`);
        if (name === "busy") ck(`${tag}: all 7 grants`, (await p.getByTestId("grant").count()) === 7);
        if (name === "newc") ck(`${tag}: nothing allowed yet`, /Nothing allowed yet\. The crew asks when it needs something\./.test(body), body);
        if (name === "ro") ck(`${tag}: read only, every control disabled`, (await p.getByTestId("read-only").count()) === 1 && (await p.getByTestId("pane-crew").locator("button:not([disabled])").count()) === 0);
        if (name === "rel") ck(`${tag}: the Releases row, second check and pacing on`, (await p.getByTestId("row-releases").count()) === 1 && (await p.getByTestId("toggle-challenger").getAttribute("aria-checked")) === "true" && /91%/.test(await text(p, "row-pace")), await text(p, "row-pace"));
        if (name === "long") ck(`${tag}: very long names wrap and markup stays text`, (await overflow(p)) <= 0 && (await p.getByTestId("grant").first().locator("b, img").count()) === 0);
        if (name === "many") ck(`${tag}: all 12 grants`, (await p.getByTestId("grant").count()) === 12);
        ck(`${tag}: no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
        await checkWords(p, ck, tag);
        ck(`${tag}: a sample makes no read of the cell`, reads === 0, `${reads} calls`);
        await shot(p, `settings-${name}-${width}.png`);
        await c.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
