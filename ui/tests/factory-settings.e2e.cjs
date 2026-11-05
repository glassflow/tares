/*
 * Browser e2e for TR-738: the factory Settings page (/settings).
 *
 * The page has six sections in a fixed order: Tools, Crew, Models and budget, People and keys,
 * Edition, Advanced. Each change goes through the route that exists and is read back from the cell:
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
const SECTIONS = ["tools", "crew", "models", "people", "edition", "advanced"];

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

    // 1. the page loads in one call, in six sections in order
    const { ctx, page } = await newPage(1440);
    const one = expectOneCall(page, "/api/factory/settings");
    await page.goto(`${BASE}/settings`, { waitUntil: "load" });
    await page.getByTestId("section-advanced").waitFor();
    await settle(page);
    await one(ck, "/settings");
    ck("the page has its heading", await page.getByRole("heading", { name: "Settings", exact: true }).isVisible());
    const order = await page.locator("section[id]").evaluateAll((s) => s.map((x) => x.id));
    ck("the sections are in order: tools, crew, models, people, edition, advanced", order.join() === SECTIONS.join(), order.join());
    const heads = await page.locator("section[id] > h2").allInnerTexts();
    ck("each has its name", heads.join("|") === "Tools|Crew|Models and budget|People and keys|Edition|Advanced", heads.join("|"));
    ck("the menu has Settings as the current page", (await page.locator('nav[aria-label="Main"] a[aria-current="page"]').allInnerTexts()).map((t) => t.trim()).join() === "Settings");

    // 2. tools
    ck("GitHub is connected, as whom", (await text(page, "tool-github")).includes("Connected as example-org") && (await text(page, "tool-github")).endsWith("Change"), await text(page, "tool-github"));
    ck("Linear and Slack are not connected, and offer Connect", /Not connected Connect$/.test(await text(page, "tool-linear")) && /Not connected Connect$/.test(await text(page, "tool-slack")),
       `${await text(page, "tool-linear")} | ${await text(page, "tool-slack")}`);
    ck("Claude Code is reporting, with how long ago", /^Claude Code Reporting: last session .+ See how$/.test(await text(page, "tool-claude_code")), await text(page, "tool-claude_code"));
    ck("a connected tool has a lit dot and an unconnected one does not", (await page.getByTestId("tool-dot").evaluateAll((d) => d.map((x) => x.getAttribute("data-on")))).join() === "true,false,false,true");
    ck("Connect for Linear goes to its settings tab", (await page.getByTestId("tool-linear").locator("a").getAttribute("href")) === "/settings/advanced?tab=linear");
    ck("Claude Code goes to its source", (await page.getByTestId("tool-claude_code").locator("a").getAttribute("href")) === "/sources/claude-code");

    // 3. the crew starts as a new cell has it
    ck("up to merge is chosen", (await page.getByTestId("how-L3-review").getAttribute("aria-checked")) === "true" && (await page.getByTestId("how-L4-ship").getAttribute("aria-checked")) === "false");
    ck("builders at once: 3", (await page.getByTestId("builders-count").innerText()) === "3" && /Up to 3 builders at once/.test(await text(page, "row-builders")));
    ck("second check and pacing are off", (await page.getByTestId("toggle-challenger").getAttribute("aria-checked")) === "false" && (await page.getByTestId("toggle-pace").getAttribute("aria-checked")) === "false");
    ck("there is no Releases row without a release profile", (await page.getByTestId("row-releases").count()) === 0);
    ck("people: two access keys", /2 access keys/.test(await text(page, "row-keys")));
    ck("the People row opens the People page", (await page.getByTestId("row-team").getByRole("link", { name: "Open People" }).getAttribute("href")) === "/people");
    await checkWords(page, ck, "live page");
    await shot(page, "live-1440.png");

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
    const grants = page.getByTestId("grant");
    ck("10 grants, 5 shown and the rest on request", (await grants.count()) === 5 && (await text(page, "grants-more")) === "Show 5 more", `${await grants.count()}`);
    const first = await grants.first().innerText();
    ck("the newest is first, in quotes, with its scope and when", /^“Builders may delete the merged branch number 6/.test(first) && /For every project, given today/.test(first), first);
    await page.getByTestId("grants-more").click();
    ck("Show 5 more shows all 10", (await grants.count()) === 10 && (await page.getByTestId("grants-more").count()) === 0);
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

    // 9. the way to everything else
    await page.goto(`${BASE}/settings`, { waitUntil: "load" });
    await page.getByTestId("section-advanced").waitFor();
    const adv = await page.getByTestId("advanced-link").evaluateAll((a) => a.map((x) => [x.querySelector("b").textContent.trim(), x.getAttribute("href")]));
    ck("Advanced lists six places", JSON.stringify(adv.map((a) => a[0])) === JSON.stringify(["All settings", "Connections", "Explore your data", "All resources", "Connect an agent", "Reads"]), JSON.stringify(adv));
    for (const [label, href] of [["All settings", "/settings/advanced"], ["Connections", "/sources"], ["Explore your data", "/explore"]]) {
      await page.goto(`${BASE}/settings`, { waitUntil: "load" });
      await page.getByTestId("advanced-link").filter({ hasText: label }).click();
      await page.waitForURL((u) => u.pathname === href);
      ck(`${label} reaches ${href}`, new URL(page.url()).pathname === href, page.url());
    }

    // 10. an address with a section lands on it
    for (const id of ["crew", "tools"]) {
      await page.goto(`${BASE}/settings#${id}`, { waitUntil: "load" });
      await page.getByTestId(`section-${id}`).waitFor();
      await settle(page);
      const top = await page.getByTestId(`section-${id}`).evaluate((s) => Math.round(s.getBoundingClientRect().top));
      ck(`/settings#${id} scrolls to ${id}`, id === "tools" ? top < 200 : top < 200 && (await page.evaluate(() => window.scrollY)) > 0, `top ${top}`);
    }

    // 11. the old addresses that name a tab still reach it (the GitHub App return, the Tares Cloud return)
    {
      const tabOpen = (name) => page.locator('nav[aria-label="Settings"]').getByRole("button", { name, exact: true });
      await page.goto(`${BASE}/settings?tab=github&installed=1`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname === "/settings/advanced");
      const u = new URL(page.url());
      ck("/settings?tab=github lands on /settings/advanced with the same query", u.searchParams.get("tab") === "github" && u.searchParams.get("installed") === "1", page.url());
      ck("and the GitHub tab is the one open", (await tabOpen("GitHub").getAttribute("aria-current")) === "page");
      ck("and this page's sections are not drawn", (await page.getByTestId("section-crew").count()) === 0);
      await page.goto(`${BASE}/settings?cloud=github-connected`, { waitUntil: "load" });
      await page.waitForURL((x) => x.pathname === "/settings/advanced");
      ck("/settings?cloud= lands on the GitHub tab", (await tabOpen("GitHub").getAttribute("aria-current")) === "page");
      ck("and shows what the cloud said", await page.getByText("GitHub is connected.", { exact: false }).first().isVisible());
      await page.goto(`${BASE}/settings`, { waitUntil: "load" });
      await page.getByTestId("section-crew").waitFor();
      ck("/settings without a tab stays on this page", new URL(page.url()).pathname === "/settings");
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
      await p.goto(`${BASE}/settings`, { waitUntil: "load" });
      await p.getByTestId("section-advanced").waitFor();
      await p.getByTestId("grants-more").click();
      await settle(p);
      ck("390: the page does not scroll sideways, grants open", (await overflow(p)) <= 0, `${await overflow(p)}px`);
      await checkWords(p, ck, "390 live");
      await shot(p, "live-390.png");
      await n.ctx.close();
    }

    // 13. without the sample flag the sample address changes nothing
    {
      const n = await newPage(1440);
      await n.page.goto(`${BASE}/settings?sample=newc`, { waitUntil: "load" });
      await n.page.getByTestId("section-advanced").waitFor();
      await settle(n.page);
      ck("?sample=newc without the flag shows the cell's own settings", (await n.page.getByTestId("grant").count()) === 5);
      await n.ctx.close();
    }

    // 14. every sample state at 1440 and 390: its words, no sideways scroll, a screenshot
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, { sampleFlag: true });
        const tag = `sample ${name} at ${width}`;
        let reads = 0;
        p.on("request", (r) => { if (r.url().includes("/api/factory/settings")) reads++; });
        await p.goto(`${BASE}/settings?sample=${name}`, { waitUntil: "load" });
        if (name === "load") await p.getByTestId("settings-loading").waitFor();
        else if (name === "err") await p.getByText("Settings did not load.").waitFor();
        else await p.getByTestId("section-advanced").waitFor();
        await settle(p);
        const body = await p.getByTestId("factory-settings").innerText();
        const sections = await p.locator("section[id]").evaluateAll((s) => s.map((x) => x.id));
        if (name === "load") ck(`${tag}: it is the loading state, no sections`, sections.length === 0 && (await p.getByTestId("settings-loading").isVisible()));
        else if (name === "err") ck(`${tag}: it says settings did not load and offers Retry`, /Settings did not load\./.test(body) && (await p.getByRole("button", { name: "Retry" }).count()) === 1 && sections.length === 0, body);
        else ck(`${tag}: six sections in order`, sections.join() === SECTIONS.join(), sections.join());
        if (name === "busy") ck(`${tag}: 7 grants, 5 shown, Show 2 more`, (await p.getByTestId("grant").count()) === 5 && (await text(p, "grants-more")) === "Show 2 more");
        if (name === "newc") ck(`${tag}: nothing allowed yet, nothing connected`, /Nothing allowed yet\. The crew asks when it needs something\./.test(body) && (await p.getByTestId("tool-dot").evaluateAll((d) => d.every((x) => x.getAttribute("data-on") === "false"))) && /Not chosen yet/.test(body) && /No access keys yet/.test(body) && (await p.getByTestId("row-team").count()) === 1);
        if (name === "ro") ck(`${tag}: read only, every control disabled, People still opens`, (await p.getByTestId("read-only").count()) === 1 && (await p.locator("#tools, #crew, #models, #people").locator("button:not([disabled]), a:not([aria-disabled=true]):not([href='/people'])").count()) === 0
           && (await p.getByTestId("row-team").getByRole("link", { name: "Open People" }).count()) === 1 && (await p.getByTestId("edition-sre").count()) === 0);
        if (name === "rel") ck(`${tag}: the Releases row, second check and pacing on`, (await p.getByTestId("row-releases").count()) === 1 && (await p.getByTestId("toggle-challenger").getAttribute("aria-checked")) === "true" && /91%/.test(await text(p, "row-pace")));
        if (name === "long") ck(`${tag}: very long names wrap and markup stays text`, (await overflow(p)) <= 0 && (await p.getByTestId("grant").first().locator("b, img").count()) === 0 && (await p.getByTestId("grant").first().innerText()).includes("<b>bold</b>"));
        if (name === "many") { await p.getByTestId("grants-more").click(); ck(`${tag}: 12 grants after Show 7 more`, (await p.getByTestId("grant").count()) === 12); }
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
