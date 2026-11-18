/*
 * Browser e2e for TR-738: the factory Settings page (/settings).
 *
 * The page has a two-level sidebar (Crew, Models, Team, Workspace, Advanced) in place of the
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
const SAMPLES = ["busy", "newc", "ro", "rel", "long", "many", "load", "err", "morning", "morningoff", "morningemail", "morningself", "morningblocked"];
// The tools (GitHub, Linear, Slack, Claude Code) moved to Integrations; their old addresses go there.
const GROUPS = ["Crew", "Models", "Team", "Workspace", "Advanced"];

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
const api2 = async (base, token, method, route, body) => {
  const r = await fetch(base + route, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
};

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
    const newPage = async (width, { sampleFlag = false, token = TOKEN, dark = false } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
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

    // 2. the tools are on Integrations: each old address opens its section there
    for (const [tab, step, title] of [["github", "github", "Code"], ["linear", "tickets", "Tickets"], ["slack", "notifications", "Notifications"],
                                      ["claude_code", "runtime", "Runtime"]]) {
      await page.goto(`${BASE}/settings?tab=${tab}`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname === "/integrations");
      await page.locator(`[data-testid=step-view][data-slot=${step}]`).waitFor();
      ck(`/settings?tab=${tab} opens ${title} on Integrations`, new URL(page.url()).searchParams.get("step") === step
         && (await page.getByTestId("page-title").innerText()) === title, page.url());
    }
    ck("the GitHub section lists the accounts", await (async () => {
      await page.goto(`${BASE}/integrations?step=github`, { waitUntil: "load" });
      await page.getByRole("heading", { name: "Accounts", exact: true }).waitFor();
      return (await page.getByText("example-org").count()) > 0;
    })());
    const integ = async (page, step) => {
      await page.goto(`${BASE}/integrations?step=${step}`, { waitUntil: "load" });
      await page.locator(`[data-testid=step-view][data-slot=${step}]`).waitFor();
      await settle(page);
    };
    {
      const DEAD = "http://127.0.0.1:9/api/v3";
      const shots = async (tag) => {
        if (!SHOTS) return;
        for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
          const n = await newPage(w, { dark });
          await integ(n.page, "github");
          await n.page.getByText("example-org").first().waitFor();
          await shot(n.page, `github-${tag}-${w}${dark ? "-dark" : ""}.png`);
          await n.ctx.close();
        }
      };
      await shots("as-found");
      await api(TOKEN, "POST", "/api/integrations/github", { name: "enterprise", token: "ghp_e2e_1392", api_url: DEAD });
      await shots("two-accounts");
      await api(TOKEN, "DELETE", "/api/integrations/github/enterprise");
      await integ(page, "github");

      const gh = page.getByTestId("github");
      const row = (n) => gh.locator(`[data-testid="github-row"][data-name="${n}"]`);
      await row("main").waitFor();
      ck("the seeded token reads as whose it is and what it does",
        (await row("main").getByTestId("github-who").innerText()) === "Personal token of example-org"
        && /^Polls the repositories you add\. Nothing uses it yet\.$/.test((await row("main").getByTestId("github-events").innerText()).replace(/\s+/g, " ").trim()),
        `${await row("main").innerText()}`);
      ck("a self-hosted cell offers to create the App and to add a token",
        (await gh.getByRole("button", { name: "Create GitHub App" }).count()) === 1 && (await gh.getByRole("button", { name: "Add a personal token" }).count()) === 1);
      await checkWords(page, ck, "GitHub item");

      await gh.getByRole("button", { name: "Add a personal token" }).click();
      const form = gh.getByTestId("github-token-form");
      ck("Add a personal token opens its form, with labelled fields, and hides the buttons",
        (await form.getByLabel("Name").count()) === 1 && (await form.getByLabel("Token").count()) === 1 && (await form.getByLabel("API URL").count()) === 1
        && (await gh.getByRole("button", { name: "Add a personal token" }).count()) === 0);
      ck("Save waits for a name and a token", await form.getByRole("button", { name: "Save" }).isDisabled());
      await form.getByLabel("Name").fill("not a name");
      await form.getByLabel("Token").fill("ghp_e2e_1392");
      await form.getByLabel("API URL").fill(DEAD);
      await form.getByRole("button", { name: "Save" }).click();
      ck("a name the daemon refuses is said under the field, as a sentence",
        await until(async () => (await form.getByLabel("Name").getAttribute("aria-invalid")) === "true")
        && (await form.getByText(/^Name must be alphanumeric/).count()) === 1);
      await shot(page, "github-refused-1440.png");
      await form.getByLabel("Name").fill("enterprise");
      await form.getByLabel("Token").press("Enter");
      ck("Enter saves: the form closes and the account is listed",
        await until(async () => (await row("enterprise").count()) === 1 && (await gh.getByTestId("github-token-form").count()) === 0));
      ck("the daemon has it", (await api(TOKEN, "GET", "/api/integrations/github")).body?.credentials?.some((c) => c.name === "enterprise" && c.api_url === DEAD));
      ck("its API URL shows beside its name", (await row("enterprise").innerText()).includes(DEAD));

      await row("enterprise").getByRole("button", { name: "Test" }).click();
      const result = row("enterprise").getByTestId("github-test");
      ck("Test says it failed, and why", await until(async () => (await result.count()) === 1 && /^Failed: could not reach GitHub/.test((await result.innerText()).trim())),
        (await result.count()) ? await result.innerText() : "no result");

      await row("enterprise").getByRole("button", { name: "Rotate" }).click();
      const rot = row("enterprise").getByTestId("github-rotate");
      ck("Rotate opens a labelled field for the new token", (await rot.getByLabel("New token").count()) === 1 && await rot.getByRole("button", { name: "Save new token" }).isDisabled());
      await rot.getByLabel("New token").fill("ghp_e2e_1392_new");
      await rot.getByLabel("New token").press("Enter");
      ck("Enter saves the new token and closes the field", await until(async () => (await row("enterprise").getByTestId("github-rotate").count()) === 0));

      await row("enterprise").getByRole("button", { name: "Delete" }).click();
      const dlg = page.getByTestId("github-delete-dialog");
      ck("Delete asks first, and says what uses it", (await dlg.getByRole("heading", { name: "Delete enterprise?" }).count()) === 1 && (await dlg.innerText()).includes("Nothing uses it."));
      await dlg.getByRole("button", { name: "Delete" }).click();
      ck("and then it is gone, here and in the daemon",
        await until(async () => (await row("enterprise").count()) === 0)
        && !(await api(TOKEN, "GET", "/api/integrations/github")).body?.credentials?.some((c) => c.name === "enterprise"));

      await gh.getByRole("button", { name: "Create GitHub App" }).click();
      const app = gh.getByTestId("github-app-form");
      ck("Create GitHub App opens its form, named github-app by default", (await app.getByLabel("Name in Tares").inputValue()) === "github-app" && (await app.getByLabel("GitHub organization").count()) === 1);
      await app.getByRole("button", { name: "Cancel" }).click();
      ck("Cancel closes it", (await gh.getByTestId("github-app-form").count()) === 0 && (await gh.getByRole("button", { name: "Create GitHub App" }).count()) === 1);
      const n = await newPage(390);
      await integ(n.page, "github");
      await n.page.getByTestId("github-row").first().waitFor();
      ck("the GitHub item fits at 390 px", (await overflow(n.page)) <= 0);
      await n.ctx.close();
    }
    // Notifications on a self-hosted cell, on Integrations: the bot token and the signing secret (TR-1392).
    // The page has said what Slack is for, so the panels are terse (Saved. / Not set yet.). Pictures
    // first, of states set through the API, so the same steps picture the panels before and after.
    {
      const shots = async (tag) => {
        if (!SHOTS) return;
        for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
          const n = await newPage(w, { dark });
          await integ(n.page, "notifications");
          await shot(n.page, `slack-${tag}-${w}${dark ? "-dark" : ""}.png`);
          await n.ctx.close();
        }
      };
      await shots("empty");
      await api(TOKEN, "PUT", "/api/settings/slack-bot-token", { token: "xoxb-e2e-1392" });
      await shots("token-saved");
      await api(TOKEN, "DELETE", "/api/settings/slack-bot-token");
      await integ(page, "notifications");

      const tok = page.getByTestId("slack-token");
      const sec = page.getByTestId("slack-secret");
      const field = tok.getByLabel("Bot User OAuth Token");
      ck("Slack shows the bot token and the signing secret, each under its own heading",
        (await tok.getByRole("heading", { name: "Bot token", exact: true }).count()) === 1 && (await sec.getByRole("heading", { name: "Signing secret", exact: true }).count()) === 1);
      ck("neither is set yet, and each says so",
        /^Not set yet\.$/.test(await text(page, "slack-token-state")) && /^Not set yet\.$/.test(await text(page, "slack-secret-state")),
        `${await text(page, "slack-token-state")} | ${await text(page, "slack-secret-state")}`);
      ck("each field has a visible label", (await field.count()) === 1 && (await sec.getByLabel("Signing Secret").count()) === 1);
      ck("Save waits for something to save", await tok.getByRole("button", { name: "Save" }).isDisabled());
      ck("nothing to clear yet", (await tok.getByRole("button", { name: "Clear the saved token" }).count()) === 0);
      await checkWords(page, ck, "Slack item");

      await field.fill("not-a-token");
      await tok.getByRole("button", { name: "Save" }).click();
      ck("a value that is not a bot token is refused, under the field, as a sentence",
        await until(async () => (await field.getAttribute("aria-invalid")) === "true") && (await tok.getByText(/^That does not look like a Slack bot token/).count()) === 1);
      await shot(page, "slack-refused-1440.png");
      await field.fill("xoxb-e2e-1392");
      ck("typing again clears the error", (await field.getAttribute("aria-invalid")) !== "true");
      await field.press("Enter");
      ck("Enter saves, and a saved token reads as saved", await until(async () => /^Saved\.$/.test(await text(page, "slack-token-state"))), await text(page, "slack-token-state"));
      ck("the field is empty again: a secret is never shown back", (await field.inputValue()) === "");
      ck("the daemon has it", (await api(TOKEN, "GET", "/api/settings/slack-bot-token")).body?.stored === true);
      await tok.getByRole("button", { name: "Clear the saved token" }).click();
      ck("Clear removes it", await until(async () => /^Not set/.test(await text(page, "slack-token-state"))) && (await api(TOKEN, "GET", "/api/settings/slack-bot-token")).body?.stored === false);
      const n = await newPage(390);
      await integ(n.page, "notifications");
      ck("the Slack item fits at 390 px", (await overflow(n.page)) <= 0);
      await n.ctx.close();
    }
    // Observability: where agents' traces go (TR-1392). Pictures first, of states set through the
    // API, so the same steps picture the panel before and after.
    {
      const shots = async (tag) => {
        if (!SHOTS) return;
        for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
          const n = await newPage(w, { dark });
          await go(n.page, "observability");
          await shot(n.page, `tracing-${tag}-${w}${dark ? "-dark" : ""}.png`);
          await n.ctx.close();
        }
      };
      const before = (await api(TOKEN, "GET", "/api/settings/tracing")).body;
      await shots("as-found");
      await api(TOKEN, "PUT", "/api/settings/tracing", { enabled: true, provider: "otlp", endpoint: "https://collector.example:4318", headers: "authorization=Bearer e2e" });
      await shots("otlp-on");
      await api(TOKEN, "PUT", "/api/settings/tracing", { enabled: false, provider: "rius", endpoint: "", headers: "", api_key: "" });
      await go(page, "observability");

      const panel = page.getByTestId("tracing");
      ck("Observability has one heading of that name", (await page.getByRole("heading", { name: "Observability", exact: true }).count()) === 1);
      ck("tracing is off and says so", /^Off\. Nothing is sent/.test(await text(page, "tracing-state")), await text(page, "tracing-state"));
      ck("the switch, the provider and the key each have a visible label",
        (await panel.getByLabel("Send agent traces").count()) === 1 && (await panel.getByLabel("Where traces go").count()) === 1 && (await panel.getByLabel("Rius API key").count()) === 1);
      ck("Rius has no key yet, and says what that means", /^No API key yet/.test(await text(page, "tracing-key-state")), await text(page, "tracing-key-state"));
      ck("Save waits for something to save", await panel.getByRole("button", { name: "Save" }).isDisabled());
      await checkWords(page, ck, "Observability item");

      await panel.getByLabel("Send agent traces").click();
      ck("switched on to Rius without a key, it says nothing arrives yet",
        await until(async () => /^On, but nothing reaches Rius until there is an API key/.test(await text(page, "tracing-state"))), await text(page, "tracing-state"));
      const key = panel.getByLabel("Rius API key");
      ck("the switch is free again before typing", await until(async () => !(await panel.getByLabel("Send agent traces").isDisabled())));
      await key.fill("gf_e2e_1392");
      await key.press("Enter");
      ck("Enter saves the key, and tracing reads as on, to Rius",
        await until(async () => /^On\. Traces go to Rius/.test(await text(page, "tracing-state"))) && /^API key set, saved here/.test(await text(page, "tracing-key-state")), await text(page, "tracing-state"));
      ck("the key field is empty again: a secret is never shown back", (await key.inputValue()) === "");
      await panel.getByRole("button", { name: "Clear the saved key" }).click();
      ck("Clear removes the key", await until(async () => /^No API key yet/.test(await text(page, "tracing-key-state"))) && (await api(TOKEN, "GET", "/api/settings/tracing")).body?.key_stored === false);

      await panel.getByLabel("Where traces go").click();
      await page.getByRole("option", { name: /Any OpenTelemetry endpoint/ }).click();
      const ep = panel.getByLabel("Endpoint");
      await ep.fill("collector:4318");
      await panel.getByRole("button", { name: "Save" }).click();
      ck("an endpoint that is not a URL is refused, under the field, as a sentence",
        await until(async () => (await ep.getAttribute("aria-invalid")) === "true") && (await panel.getByText(/^Endpoint must be an http\(s\) URL/).count()) === 1);
      await shot(page, "tracing-refused-1440.png");
      await ep.fill("https://collector.example:4318");
      ck("typing again clears the error", (await ep.getAttribute("aria-invalid")) !== "true");
      await ep.press("Enter");
      ck("saved, tracing goes to that endpoint",
        await until(async () => /^On\. Traces go to https:\/\/collector\.example:4318/.test(await text(page, "tracing-state"))), await text(page, "tracing-state"));
      const n = await newPage(390);
      await go(n.page, "observability");
      ck("the Observability item fits at 390 px", (await overflow(n.page)) <= 0);
      await n.ctx.close();
      await api(TOKEN, "PUT", "/api/settings/tracing", { enabled: before.enabled, provider: "", endpoint: "", headers: "", api_key: "" });
    }

    // 2b. model providers (TR-1392). Pictures first, of states set through the API, so the same
    // steps picture the panel before and after. Every endpoint is a closed port, so discovery fails
    // at once instead of reaching a real provider; the cell ends with none, as it started.
    {
      const DEAD = "http://127.0.0.1:9/v1";
      const shots = async (tag, wait) => {
        if (!SHOTS) return;
        for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
          const n = await newPage(w, { dark });
          await go(n.page, "providers");
          await n.page.getByText(wait).first().waitFor();
          await shot(n.page, `providers-${tag}-${w}${dark ? "-dark" : ""}.png`);
          await n.ctx.close();
        }
      };
      await shots("as-found", /model provider yet/i);
      await api(TOKEN, "PUT", "/api/settings/providers/new", { kind: "anthropic", key: "sk-ant-e2e-1392", base_url: "http://127.0.0.1:9" });
      await api(TOKEN, "PUT", "/api/settings/providers/new", { kind: "openai_compatible", name: "LiteLLM", key: "sk-e2e-1392", base_url: DEAD });
      await shots("two-providers", "LiteLLM");
      await api(TOKEN, "DELETE", "/api/settings/providers/litellm");
      await api(TOKEN, "DELETE", "/api/settings/providers/anthropic");
      await go(page, "providers");

      const panel = page.getByTestId("providers");
      const row = (id) => panel.locator(`[data-testid="providers-row"][data-id="${id}"]`);
      ck("Providers has one heading of that name", (await page.getByRole("heading", { name: "Providers", exact: true }).count()) === 1);
      ck("a cell with no provider says so, and what waits on one",
        /^No model provider yet\. Add one: agents cannot be switched on and Ask cannot answer until then\.$/.test(await text(page, "providers-empty")), await text(page, "providers-empty"));
      await checkWords(page, ck, "Providers item");
      await panel.getByRole("button", { name: "Add a provider" }).click();
      const form = panel.getByTestId("providers-form");
      ck("Add a provider opens a form with labelled fields, on Anthropic",
        (await form.getByLabel("Provider").count()) === 1 && (await form.getByLabel("API key", { exact: true }).count()) === 1 && (await form.getByLabel("Base URL (optional)").count()) === 1
        && /Claude models/.test(await form.innerText()));
      ck("Save waits for a key", await form.getByRole("button", { name: "Save" }).isDisabled());
      await form.getByLabel("Provider").click();
      await page.getByRole("option", { name: "Ollama", exact: true }).click();
      ck("Ollama fills its name and address, and needs no key",
        (await form.getByLabel("Name").inputValue()) === "Ollama" && (await form.getByLabel("Base URL").inputValue()) === "http://localhost:11434/v1"
        && (await form.getByLabel("API key (optional)").count()) === 1 && !(await form.getByRole("button", { name: "Save" }).isDisabled()));
      const url = form.getByLabel("Base URL");
      await url.fill("localhost:11434");
      await form.getByRole("button", { name: "Save" }).click();
      ck("a base URL the daemon refuses is said under the field, as a sentence",
        await until(async () => (await url.getAttribute("aria-invalid")) === "true") && (await form.getByText(/^Base URL must be an http\(s\) URL/).count()) === 1);
      await shot(page, "providers-refused-1440.png");
      await url.fill(DEAD);
      ck("typing again clears the error", (await url.getAttribute("aria-invalid")) !== "true");
      await url.press("Enter");
      ck("Enter saves, and says the endpoint did not answer",
        await until(async () => (await panel.getByTestId("providers-form").count()) === 0 && (await panel.getByTestId("providers-note").count()) === 1)
        && /^Saved, but the endpoint did not answer/.test(await text(page, "providers-note")), await panel.innerText());
      ck("the first provider is the default, and its row says it needs no key",
        (await row("ollama").getByTestId("providers-default").count()) === 1 && (await row("ollama").getByTestId("providers-key-state").innerText()).trim() === "Needs no key.",
        await row("ollama").innerText());
      ck("its models line says why there is no list", /^The endpoint did not answer; is it running and reachable from this daemon\? Agents can still name a model by hand\.$/.test((await row("ollama").getByTestId("providers-models").innerText()).trim()),
        await row("ollama").getByTestId("providers-models").innerText());

      await api(TOKEN, "PUT", "/api/settings/providers/new", { kind: "anthropic", key: "sk-ant-e2e-1392", base_url: "http://127.0.0.1:9" });
      await go(page, "providers");
      ck("a saved Anthropic key reads as saved here", (await row("anthropic").getByTestId("providers-key-state").innerText()).trim() === "Key saved here.", await row("anthropic").innerText());
      await row("anthropic").getByRole("button", { name: "Make default" }).click();
      ck("Make default moves the default", await until(async () => (await row("anthropic").getByTestId("providers-default").count()) === 1 && (await row("ollama").getByTestId("providers-default").count()) === 0)
        && (await api(TOKEN, "GET", "/api/settings/providers")).body?.default === "anthropic");
      await row("anthropic").getByRole("button", { name: "Edit" }).click();
      ck("Edit names the provider and keeps the saved key unless one is typed",
        /^Anthropic\. Claude models/.test((await form.innerText()).trim()) && (await form.getByLabel("API key", { exact: true }).getAttribute("placeholder")) === "Leave empty to keep the saved key"
        && (await form.getByLabel("Provider").count()) === 0);
      await form.getByRole("button", { name: "Cancel" }).click();
      ck("Cancel closes it", (await panel.getByTestId("providers-form").count()) === 0 && (await panel.getByRole("button", { name: "Add a provider" }).count()) === 1);

      await row("ollama").getByRole("button", { name: "Remove" }).click();
      const dialog = page.getByTestId("providers-remove-dialog");
      ck("Remove asks first, and says where its agents go", await until(async () => (await dialog.count()) === 1)
        && /Remove Ollama\?/.test(await dialog.innerText()) && /move to the default/.test(await dialog.innerText()), await dialog.innerText().catch(() => ""));
      await page.getByTestId("providers-remove-confirm").click();
      ck("Remove removes it, here and in the daemon", await until(async () => (await row("ollama").count()) === 0 && (await dialog.count()) === 0)
        && !(await api(TOKEN, "GET", "/api/settings/providers")).body?.providers.some((p) => p.id === "ollama"));
      const n = await newPage(390);
      await go(n.page, "providers");
      await n.page.getByTestId("providers-row").first().waitFor();
      ck("the Providers item fits at 390 px", (await overflow(n.page)) <= 0);
      await n.ctx.close();
      await api(TOKEN, "DELETE", "/api/settings/providers/anthropic");
      ck("the cell ends with no provider, as it started", ((await api(TOKEN, "GET", "/api/settings/providers")).body?.providers ?? []).every((p) => !p.configured && !p.stored));
    }

    // 2c. access keys (TR-1392). Pictures first, of states set through the API, so the same steps
    // picture the panel before and after: the two seeded keys, then a project key and a revoked one.
    {
      const shots = async (tag) => {
        if (!SHOTS) return;
        for (const [w, dark] of [[1440, false], [1440, true], [390, false]]) {
          const n = await newPage(w, { dark });
          await go(n.page, "keys");
          await n.page.getByText("writer").first().waitFor();
          await shot(n.page, `keys-${tag}-${w}${dark ? "-dark" : ""}.png`);
          await n.ctx.close();
        }
      };
      await shots("as-found");
      const pkey = (await api(TOKEN, "POST", "/api/keys", { name: "px-reader", project: ids.project })).body;
      const old = (await api(TOKEN, "POST", "/api/keys", { name: "old-ci", scopes: ["ingest"] })).body;
      await api(TOKEN, "DELETE", `/api/keys/${old.id}`);
      await shots("project-and-revoked");
      const pname = ((await api(TOKEN, "GET", "/api/projects")).body?.projects ?? []).find((p) => p.id === ids.project)?.name;
      await go(page, "keys");

      const panel = page.getByTestId("keys");
      const row = (name) => panel.locator('[data-testid="keys-row"]').filter({ has: page.getByText(name, { exact: true }) });
      const rowText = (name, id) => row(name).getByTestId(id).innerText().then((t) => t.replace(/\s+/g, " ").trim());
      ck("Access keys has one heading of that name, and no introduction", (await page.getByRole("heading", { name: "Access keys", exact: true }).count()) === 1 && !/Give each agent/.test(await text(page, "keys")));
      ck("each key says what it can do, in words: read", (await rowText("reader", "keys-can")) === "Can read.", await rowText("reader", "keys-can"));
      ck("each key says what it can do, in words: send data, not ingest", (await rowText("writer", "keys-can")) === "Can send data.", await rowText("writer", "keys-can"));
      ck("a key nobody used says so, and when it was made", /^Never used\. Made (just now|\d+ \w+ ago)\.$/.test(await rowText("writer", "keys-used")), await rowText("writer", "keys-used"));
      ck("a project key names its project, not its id", !!pname && (await rowText("px-reader", "keys-can")) === `Can read ${pname} and record findings in it.`, `${await rowText("px-reader", "keys-can")} / ${pname}`);
      ck("each key says whose it is", /^Belongs to /.test(await rowText("writer", "keys-person")), await rowText("writer", "keys-person"));
      ck("a revoked key is hidden until asked for", (await row("old-ci").count()) === 0 && (await text(page, "keys-revoked-toggle")) === "Show 1 revoked key", await text(page, "keys-revoked-toggle"));
      await page.getByTestId("keys-revoked-toggle").click();
      ck("asked for, it says it is revoked and offers no Revoke", (await row("old-ci").getByTestId("keys-revoked").count()) === 1 && (await row("old-ci").getByTestId("keys-revoke").count()) === 0
         && /^Revoked (just now|\d+ \w+ ago)\.$/.test(await rowText("old-ci", "keys-used")), await rowText("old-ci", "keys-used"));
      await page.getByTestId("keys-revoked-toggle").click();
      ck("Hide revoked keys hides it again", (await row("old-ci").count()) === 0 && (await text(page, "keys-revoked-toggle")) === "Show 1 revoked key");
      await checkWords(page, ck, "Access keys item");

      // creating one: the name and what it can do are said under their fields
      await page.getByTestId("keys-add").click();
      const name = page.getByTestId("keys-name");
      ck("Create a key opens a form, on the name", (await page.getByTestId("keys-form").count()) === 1 && await name.evaluate((e) => e === document.activeElement));
      ck("the form starts on Read only", (await page.getByTestId("keys-scope-read").getAttribute("aria-checked")) === "true"
         && (await page.getByTestId("keys-scope-ingest").getAttribute("aria-checked")) === "false" && (await page.getByTestId("keys-scope-admin").getAttribute("aria-checked")) === "false");
      await name.press("Enter");
      ck("an empty name is said under the Name field", (await name.getAttribute("aria-invalid")) === "true" && /Give the key a name\./.test(await text(page, "keys-form")), await text(page, "keys-form"));
      await name.fill("e2e-agent");
      ck("typing clears it", (await name.getAttribute("aria-invalid")) !== "true");
      await page.getByTestId("keys-scope-read").click();
      await name.press("Enter");
      ck("nothing it can do is said under the choices", (await text(page, "keys-scope-error")) === "Pick at least one thing it can do.");
      await page.getByTestId("keys-scope-read").click();
      await page.getByTestId("keys-scope-ingest").click();
      await shot(page, "keys-form-1440.png");
      await name.press("Enter");
      await page.getByTestId("keys-created").waitFor();
      ck("Enter creates it and closes the form", (await page.getByTestId("keys-form").count()) === 0);
      ck("the new key is shown once, with what it can do", /^Key created: e2e-agent\. Can read and send data\. Copy it now, it is not shown again\./.test(await text(page, "keys-created"))
         && /^nvf_/.test(await text(page, "keys-created-secret")), await text(page, "keys-created"));
      ck("the new key is in the list", (await row("e2e-agent").count()) === 1 && (await rowText("e2e-agent", "keys-can")) === "Can read and send data.");
      await shot(page, "keys-created-1440.png");
      await page.getByTestId("keys-created-copy").click();
      await page.waitForTimeout(100);
      ck("Copy key says it copied, or how to copy by hand", (await text(page, "keys-created-copy")) === "Copied" || /Could not copy\. Select the key and copy it by hand\./.test(await text(page, "keys-created")));
      await page.getByTestId("keys-created-done").click();
      ck("I have copied it hides the secret for good", (await page.getByTestId("keys-created").count()) === 0 && !(await text(page, "keys")).includes("nvf_e2e"));
      await page.getByTestId("keys-add").click();
      await page.getByTestId("keys-name").press("Escape");
      ck("Escape closes the form", (await page.getByTestId("keys-form").count()) === 0);

      // revoking asks first, and says what stops
      await row("e2e-agent").getByTestId("keys-revoke").click();
      const dlg = page.getByTestId("keys-revoke-dialog");
      await dlg.waitFor();
      ck("Revoke asks first, and says what stops working", (await page.getByRole("dialog", { name: "Revoke e2e-agent?" }).count()) === 1
         && /It stops working at once and its subscriptions are removed\./.test(await dlg.innerText()), await dlg.innerText());
      await shot(page, "keys-revoke-1440.png");
      await dlg.getByRole("button", { name: "Cancel" }).click();
      ck("Cancel keeps it", (await dlg.count()) === 0 && (await row("e2e-agent").getByTestId("keys-revoke").count()) === 1);
      await row("e2e-agent").getByTestId("keys-revoke").click();
      await page.getByTestId("keys-revoke-confirm").click();
      await dlg.waitFor({ state: "detached" });
      await page.waitForFunction(() => ![...document.querySelectorAll('[data-testid="keys-row"]')].some((r) => r.textContent.includes("e2e-agent")));
      const listed = (await api(TOKEN, "GET", "/api/keys")).body?.keys ?? [];
      ck("Revoke revokes it, and the list drops it", !!listed.find((k) => k.name === "e2e-agent")?.revoked_at && (await text(page, "keys-revoked-toggle")) === "Show 2 revoked keys", await text(page, "keys-revoked-toggle"));

      const n = await newPage(390);
      await go(n.page, "keys");
      await n.page.getByText("writer").first().waitFor();
      ck("the Access keys item fits at 390 px", (await overflow(n.page)) <= 0, String(await overflow(n.page)));
      await n.ctx.close();
      await api(TOKEN, "DELETE", `/api/keys/${pkey.id}`);
    }

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
    for (const [address, id] of [["/settings#crew", "crew"], ["/settings#models", "providers"], ["/settings?tab=anthropic", "providers"],
                                 ["/settings?tab=agents", "budget"], ["/settings?tab=access", "keys"], ["/settings/advanced?tab=observability", "observability"],
                                 ["/settings/advanced", "agent_tools"]]) {
      await page.goto(`${BASE}${address}`, { waitUntil: "load" });
      await page.getByTestId(`pane-${id}`).waitFor();
      ck(`${address} lands on ${id}`, new URL(page.url()).pathname === "/settings", page.url());
    }

    // 11. the GitHub App return and the Tares Cloud return now land on Integrations
    {
      await page.goto(`${BASE}/settings#tools`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname === "/integrations");
      ck("/settings#tools opens Code on Integrations", new URL(page.url()).searchParams.get("step") === "github", page.url());
      await page.goto(`${BASE}/settings?tab=github&installed=1`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname === "/integrations");
      ck("/settings?tab=github keeps the rest of its address on the way", new URL(page.url()).searchParams.get("installed") === "1"
         && new URL(page.url()).searchParams.get("step") === "github", page.url());
      await page.goto(`${BASE}/settings?cloud=github-connected`, { waitUntil: "load" });
      await page.waitForURL((u) => u.pathname === "/integrations");
      await page.getByTestId("cloud-back").waitFor();
      ck("/settings?cloud= lands on Code and says what the cloud said", (await page.getByTestId("cloud-back").innerText()).startsWith("GitHub is connected."));
      await page.waitForURL((u) => !u.searchParams.has("cloud"));
      ck("and the message is said once: the address drops it", new URL(page.url()).searchParams.get("step") === "github", page.url());
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
      for (const tab of ["keys", "providers", "observability"]) {
        await go(p, tab);
        await p.locator(`[data-testid="pane-${tab}"] ${tab === "observability" ? '[data-testid="tracing-state"]' : tab === "github" ? '[data-testid="github-row"]' : tab === "providers" ? '[data-testid="providers"]' : '[data-testid="keys"]'}`).first().waitFor();
        ck(`1000: ${tab} does not scroll sideways`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
      }
      // Observability's select was the last native one in a pane (TR-1392 moved it to the kit);
      // the section picker at 390 px is a native select with the same chevron
      {
        const m = await newPage(390);
        await go(m.page, "keys");
        const sel = m.page.getByTestId("settings-select");
        await sel.waitFor();
        await sel.focus();
        const rep = await sel.evaluate((e) => getComputedStyle(e).backgroundRepeat);
        ck("a focused select draws its chevron once", rep === "no-repeat", rep);
        await m.ctx.close();
      }
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

    // 13b. Morning status: the samples in dark mode, then the real cell, self-hosted and on Tares Cloud
    {
      const CHANNELS = { channels: [{ id: "C1", name: "factory-status", is_private: false }], reason: null };
      const morningPage = async (width, name) => {
        const n = await newPage(width, { sampleFlag: true, dark: true });
        await n.page.route("**/api/slack/channels*", (r) => r.fulfill({ json: CHANNELS }));
        await n.page.goto(`${BASE}/settings?sample=${name}&tab=morning`, { waitUntil: "load" });
        await n.page.getByTestId("morning-status").waitFor();
        await settle(n.page);
        return n;
      };
      const SHOTNAME = { morning: "both", morningemail: "email-only", morningself: "selfhosted", morningblocked: "blocked", morningoff: "off" };
      for (const width of [1440, 390]) {
        for (const name of ["morning", "morningoff", "morningemail", "morningself", "morningblocked"]) {
          const { ctx: c, page: p } = await morningPage(width, name);
          const tag = `morning ${name} at ${width}`;
          ck(`${tag}: no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
          await checkWords(p, ck, tag);
          await shot(p, `morning-${SHOTNAME[name]}-${width}.png`);
          if (name === "morning") {
            ck(`${tag}: Slack and Email are ticked`, (await p.getByTestId("morning-to-slack").getAttribute("data-state")) === "checked" && (await p.getByTestId("morning-to-email").getAttribute("data-state")) === "checked");
            ck(`${tag}: the channel picker shows`, (await p.locator(".slack-pick").count()) === 1);
            const boxes = p.getByTestId("morning-recipient");
            ck(`${tag}: people with an email are listed, Lee (none) is not`, (await boxes.count()) === 3, `${await boxes.count()}`);
            ck(`${tag}: two are ticked`, (await p.locator('[data-testid="morning-recipient"][data-state="checked"]').count()) === 2);
            const unticked = p.locator('[data-testid="morning-recipient"][data-state="unchecked"]').first();
            const bg = await unticked.evaluate((e) => { const a = document.createElement("div"); a.style.background = "var(--input-bg)"; document.body.appendChild(a); const want = getComputedStyle(a).backgroundColor; a.remove(); return [getComputedStyle(e).backgroundColor, want]; });
            ck(`${tag}: an unticked box uses the field background`, bg[0] === bg[1], bg.join(" vs "));
            ck(`${tag}: it is not white`, bg[0] !== "rgb(255, 255, 255)", bg[0]);
            const box = p.getByTestId("morning-to-slack");
            const gap = await box.evaluate((e) => Math.round(e.nextElementSibling.getBoundingClientRect().left - e.getBoundingClientRect().right));
            ck(`${tag}: the box sits apart from its label`, gap >= 8, `${gap}px`);
            ck(`${tag}: the last result is shown`, (await text(p, "morning-last-result")) === "Last sent Tuesday 09:00 to Slack and 2 people.", await text(p, "morning-last-result"));
            ck(`${tag}: no missing message`, (await p.getByTestId("morning-missing").count()) === 0);
            ck(`${tag}: Save and Send one now are live`, await p.getByTestId("morning-save").isEnabled() && await p.getByTestId("morning-send-now").isEnabled());
            const writes = [];
            p.on("request", (r) => { if (r.url().includes("/api/factory/settings/morning")) writes.push(r.method()); });
            await p.getByTestId("morning-save").click();
            await p.getByTestId("morning-note").waitFor();
            ck(`${tag}: a sample's Save writes nothing and says so`, writes.length === 0 && (await text(p, "morning-note")) === "Saved. It goes out every weekday at 09:00.", writes.join(","));
            await p.getByTestId("morning-send-now").click();
            await settle(p);
            ck(`${tag}: a sample's Send one now calls nothing`, writes.length === 0, writes.join(","));
          }
          if (name === "morningemail") {
            ck(`${tag}: Slack is not connected but the page still works`, (await p.getByTestId("morning-no-slack").count()) === 1 && (await p.getByTestId("morning-to-slack").isDisabled()) && (await p.getByTestId("morning-toggle").isEnabled()));
            ck(`${tag}: Email is ticked with its people`, (await p.getByTestId("morning-to-email").getAttribute("data-state")) === "checked" && (await p.getByTestId("morning-recipient").count()) === 3);
            ck(`${tag}: no channel picker for an unconnected Slack`, (await p.locator(".slack-pick").count()) === 0);
            ck(`${tag}: Save is live`, await p.getByTestId("morning-save").isEnabled());
          }
          if (name === "morningself") {
            ck(`${tag}: Email cannot be ticked and says why`, (await p.getByTestId("morning-to-email").isDisabled()) && /Not available on this cell/.test(await text(p, "morning-send-to")), await text(p, "morning-send-to"));
            ck(`${tag}: no list of people`, (await p.getByTestId("morning-recipients").count()) === 0);
            ck(`${tag}: Slack still works`, await p.getByTestId("morning-to-slack").isEnabled());
          }
          if (name === "morningblocked") {
            ck(`${tag}: says what is missing`, (await text(p, "morning-missing")) === "Pick at least one person to email.", await p.getByTestId("morning-missing").count() ? await text(p, "morning-missing") : "none");
            ck(`${tag}: Save and Send one now wait`, await p.getByTestId("morning-save").isDisabled() && await p.getByTestId("morning-send-now").isDisabled());
          }
          if (name === "morningoff") {
            ck(`${tag}: off says nothing is sent`, (await text(p, "morning-summary")) === "Off. Nothing is sent.", await text(p, "morning-summary"));
            ck(`${tag}: turning it on with nowhere to send says so`, await (async () => {
              await p.getByTestId("morning-toggle").click();
              return (await text(p, "morning-missing")) === "Choose where to send it: Slack, Email, or both." && await p.getByTestId("morning-save").isDisabled();
            })());
          }
          await c.close();
        }
      }

      // the real self-hosted cell: email is not available, and without Slack nothing can be sent
      {
        const n = await newPage(1440, { dark: true });
        await go(n.page, "morning");
        ck("self-hosted: Email is off and says it is not available", (await n.page.getByTestId("morning-to-email").isDisabled()) && /Not available on this cell/.test(await text(n.page, "morning-send-to")));
        ck("self-hosted: Slack not connected says so", (await n.page.getByTestId("morning-no-slack").count()) === 1);
        ck("self-hosted without Slack: the switch and Save wait", (await n.page.getByTestId("morning-toggle").isDisabled()) && (await n.page.getByTestId("morning-save").isDisabled()));
        ck("the page says the feature is off", (await api(TOKEN, "GET", "/api/factory/settings")).body.morning.enabled === false);
        await checkWords(n.page, ck, "morning self-hosted");
        await n.ctx.close();
      }

      // a read key is refused the settings page already, and the routes too
      {
        const r = await api(ids.read_key, "PUT", "/api/factory/settings/morning", { enabled: false });
        ck("a read key cannot save Morning status", r.status === 403, r.status);
        const r2 = await api(ids.read_key, "POST", "/api/factory/settings/morning/send", {});
        ck("a read key cannot send one", r2.status === 403, r2.status);
      }

      // a real Tares Cloud cell against a local stand-in for the control plane: no mail is sent
      const FAKE_PORT = PORT + 5;
      const asks = [];
      const fake = http.createServer((req, res) => {
        let body = "";
        req.on("data", (d) => body += d);
        req.on("end", () => {
          asks.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(body || "{}") });
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ accepted: true, sent: 1, total: 1, failed: [] }));
        });
      });
      await new Promise((r) => fake.listen(FAKE_PORT, "127.0.0.1", r));
      const home2 = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-settings-cloud-"));
      const PORT2 = PORT + 2;
      const BASE2 = `http://127.0.0.1:${PORT2}`;
      execFileSync(python, ["-c", `
import sys, os
sys.path.insert(0, os.getcwd())
from tares.store import Store
s = Store(os.path.join(sys.argv[1], "tares.duckdb"))
s.create_person("Alex", "alex@example.com")
s.create_person("Kim", "kim@example.com")
s.con.close()
`, home2], { cwd: REPO, encoding: "utf8", env: { ...env, TARES_HOME: home2 } });
      const cloudEnv = { ...env, TARES_HOME: home2, TARES_PORT: String(PORT2), TARES_MCP_PORT: String(PORT2 + 1),
                         TARES_WORKSPACE_API_URL: `http://127.0.0.1:${FAKE_PORT}/api/workspaces/demo` };
      const daemon2 = spawn(tares, ["up", "--port", String(PORT2), "--data-dir", home2], { cwd: REPO, stdio: "ignore", env: cloudEnv });
      try {
        ck("cloud daemon up", await waitFor(`${BASE2}/health`));
        for (const width of [1440, 390]) {
          const tag = `morning cloud at ${width}`;
          const n = await newPage(width, { dark: true });
          const p = n.page;
          await p.goto(`${BASE2}/settings?tab=morning`, { waitUntil: "load" });
          await p.getByTestId("morning-status").waitFor();
          await settle(p);
          if (width === 1440) {
            ck(`${tag}: no Slack here, Email is live`, (await p.getByTestId("morning-to-slack").isDisabled()) && (await p.getByTestId("morning-to-email").isEnabled()));
            ck(`${tag}: the switch is live`, await p.getByTestId("morning-toggle").isEnabled());
            await p.getByTestId("morning-to-email").click();
            await p.getByTestId("morning-recipients").waitFor();
            await checkWords(p, ck, tag);
            ck(`${tag}: the people with email`, (await p.getByTestId("morning-recipient").count()) >= 2, `${await p.getByTestId("morning-recipient").count()}`);
            await p.getByTestId("morning-recipient").first().click();
            await p.getByTestId("morning-toggle").click();
            await p.getByTestId("morning-save").click();
            await p.getByTestId("morning-note").waitFor();
            const saved = (await api2(BASE2, TOKEN, "GET", "/api/factory/settings")).body.morning;
            ck(`${tag}: saved and read back`, saved.enabled === true && saved.to_email === true && saved.recipients.length === 1 && !saved.to_slack, JSON.stringify(saved));
            await p.getByTestId("morning-send-now").click();
            await p.getByTestId("morning-last-result").waitFor();
            await until(async () => /Last sent/.test(await text(p, "morning-last-result")));
            const last = await text(p, "morning-last-result");
            ck(`${tag}: the result line says it went`, /^Last sent \w+ \d\d:\d\d to 1 person\.$/.test(last), last);
            ck(`${tag}: the cell asked the control plane once, with its own token, to the workspace address`,
               asks.length === 1 && asks[0].auth === `Bearer ${TOKEN}` && asks[0].url === "/api/workspaces/demo/status-email" && asks[0].body.recipients.length === 1, JSON.stringify(asks.map((a) => a.url)));
            ck(`${tag}: the email is plain text with a top line`, /Nothing is waiting on anyone\./.test(asks[0].body.text) && !asks[0].body.text.includes("**") && asks[0].body.subject.startsWith("Morning status"), asks[0].body.subject);
            ck(`${tag}: saved settings keep the send out of the scheduled date`, (await api2(BASE2, TOKEN, "GET", "/api/factory/settings")).body.morning.enabled === true);
          } else {
            await until(async () => (await p.getByTestId("morning-last-result").count()) > 0);
            ck(`${tag}: the result is shown`, /^Last sent /.test(await text(p, "morning-last-result")), "");
            ck(`${tag}: no sideways scroll`, (await overflow(p)) <= 0, `${await overflow(p)}px`);
          }
          await shot(p, `morning-after-send-${width}.png`);
          await n.ctx.close();
        }
      } finally {
        daemon2.kill();
        fake.close();
        fs.rmSync(home2, { recursive: true, force: true });
      }
    }

    // 14. every sample state at 1440 and 390: its words, no sideways scroll, a screenshot
    for (const width of [1440, 390]) {
      for (const name of SAMPLES) {
        const { ctx: c, page: p } = await newPage(width, { sampleFlag: true });
        const tag = `sample ${name} at ${width}`;
        let reads = 0;
        p.on("request", (r) => { if (r.url().includes("/api/factory/settings")) reads++; });
        const pane = name.startsWith("morning") ? "morning" : ["busy", "newc", "long", "many"].includes(name) ? "allowed" : "crew";
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
