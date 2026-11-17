/*
 * Browser e2e for the factory's Set up your factory screen (TR-872): /setup. A rail on the left (a list
 * under the title at 390 px): how the crew works, GitHub, the integration steps the API sends, and the
 * terminal commands. For an integration step, what is connected, what can be connected, and the form of
 * the integration being connected or changed. One generic renderer draws
 * every field by its kind (text, secret, url, select, bool, mapping). Every string comes from
 * GET /api/factory/setup, which the screen reads once; saves go through the routes that exist.
 *
 * Seeds a scratch factory cell (tests/seed-factory-setup.py, invented names), then runs the real daemon on
 * it, serving the built console. The states a plain cell cannot be in (a mapping, a select, a licence that
 * ended, a long or a large answer, Tares Cloud's apps) are the console's own samples (?sample=<name>, with
 * the sample flag), drawn by the same code from an answer shaped like the real one. Not built here, by
 * ticket: the Guardrails step's content (TR-1096). Test connection, the not-licensed rows and where the screen
 * opens from (the menu's Setup card) are here (TR-1095).
 * Every state shows only words from words.json and none scrolls sideways at 390 px.
 * Run:  npm run build && node tests/factory-setup.e2e.cjs     (from ui/, tares installed in ../.venv)
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

const PORT = Number(process.env.TARES_E2E_PORT || 8381);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const SLOTS = ["tickets", "review", "ci", "memory", "monitoring", "guardrails", "runtime"];

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

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-setup-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-setup.py"), home, String(PORT + 2), String(PORT + 3), dist, tares],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  ck("seeded the projects", !!ids["project-p"] && !!ids["project-q"]);

  const env = { HOME: process.env.HOME, PATH: process.env.PATH, PYTHONPATH: REPO, TARES_EDITION: "factory", TARES_HOME: home,
                TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off" };
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width, { sampleFlag = false } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      await ctx.addInitScript((flag) => { try { if (flag) localStorage.setItem("taresSamples", "1"); } catch (e) { /* none */ } }, sampleFlag);
      const page = await ctx.newPage();
      const writes = [];       // every write the page sends: method, path, parsed body
      page.on("request", (r) => {
        if (r.method() === "GET" || !new URL(r.url()).pathname.startsWith("/api/")) return;
        let body = null;
        try { body = r.postData() ? JSON.parse(r.postData()) : null; } catch (e) { body = r.postData(); }
        writes.push({ method: r.method(), path: new URL(r.url()).pathname, body });
      });
      return { ctx, page, writes };
    };
    const shot = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    // buttons that run past the edge of the row they are in (the page itself may not scroll, but the row is cut)
    const inside = (page) => page.evaluate(() => [...document.querySelectorAll("li[data-testid] button")].filter((b) => {
      const r = b.getBoundingClientRect(), l = b.closest("li").getBoundingClientRect();
      return r.width > 0 && (r.right > l.right + 0.5 || r.left < l.left - 0.5);
    }).map((b) => b.dataset.testid).join());
    const settle = async (page) => { await page.waitForLoadState("networkidle"); await page.waitForTimeout(250); };
    const text = (loc) => loc.innerText().then((t) => t.replace(/\s+/g, " ").trim());
    // the note shows at once and the cell is read again after it, so wait for that read too
    // the words as the API sent them: a tag is drawn in capitals, which innerText would return as capitals
    const raw = (loc) => loc.textContent().then((t) => t.trim());
    const flashed = async (page) => { await page.getByTestId("flash").waitFor(); await settle(page); };
    const flashText = async (page) => (await text(page.getByTestId("flash"))).replace(/ Dismiss$/, "");
    const read = async (page) => (await page.request.get(`${BASE}/api/factory/setup`)).json();
    const stepOf = (data, slot) => data.steps.find((s) => s.slot === slot);
    const connectedOf = (data, slot, id) => stepOf(data, slot).connected.find((c) => c.id === id);
    const open = async (page, slot, url = "/setup") => {
      await page.goto(`${BASE}${url}`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      if (slot) { await page.getByTestId(`step-${slot}`).click(); await page.locator(`[data-testid=step-view][data-slot=${slot}]`).waitFor(); }
    };
    const widths = async (label, run) => {
      for (const width of [1440, 390]) {
        const c = await newPage(width, { sampleFlag: true });
        await run(c, width, `${label} at ${width}`);
        await c.ctx.close();
      }
    };
    const sampleState = async (label, file, url, after) => widths(label, async ({ page }, width, name) => {
      await page.goto(`${BASE}${url}`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      if (after) await after(page, width, name);
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
      ck(`${name}: every button stays inside its row`, (await inside(page)) === "", await inside(page));
      await checkWords(page, ck, name);
      await shot(page, `${file}-${width}.png`);
    });

    // 1. the live cell: one read, the seven steps, the first one open, no main menu
    console.log("== the screen on a cell that is partly set up ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      const one = expectOneCall(page, "/api/factory/setup");
      await page.goto(`${BASE}/setup`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      await one(ck, "/setup");
      const api = await read(page);
      ck("the title and the exit link are the API's", (await text(page.getByTestId("setup-title"))) === api.title
         && (await text(page.getByTestId("setup-exit"))) === api.exit_label && (await page.getByTestId("setup-exit").getAttribute("href")) === "/");
      ck("the title is a crumb in the console header and the way out is a bordered button there, not a line in the page",
        (await page.getByTestId("edition-header").getByTestId("setup-title").count()) === 1 && (await page.getByTestId("edition-header").getByTestId("setup-exit").count()) === 1
        && (await page.getByTestId("setup").getByTestId("setup-title").count()) === 0 && (await page.getByTestId("setup").getByTestId("setup-exit").count()) === 0
        && (await page.getByTestId("setup-exit").evaluate((el) => { const c = getComputedStyle(el); return c.borderTopWidth === "1px" && c.textDecorationLine === "none"; })));
      ck("the main menu is hidden", (await page.locator('nav[aria-label="Main"]').count()) === 0);
      const RAIL = ["crew", "github", ...api.steps.map((x) => x.slot), "terminal"];
      ck("the rail lists the crew, GitHub, the API's steps and the terminal, in order",
        (await page.getByTestId("rail").locator('button[data-testid^="step-"]').evaluateAll((b) => b.map((x) => x.dataset.testid.slice(5)))).join() === RAIL.join(), RAIL.join());
      ck("monitoring is not a step of setup yet", !RAIL.includes("monitoring"));
      ck("on a wide screen the rail shows and the narrow list does not", (await page.getByTestId("rail").isVisible()) && !(await page.getByTestId("rail-narrow").isVisible()));
      const lines = [];
      for (const x of api.steps) lines.push(await text(page.getByTestId("rail").getByTestId(`step-${x.slot}`).getByTestId("step-line")));
      ck("each integration step's line is the API's", lines.join("|") === api.steps.map((x) => x.line).join("|"), lines.join("|"));
      ck("the progress is the API's, out of every step", (await text(page.getByTestId("progress-text"))) === api.progress.text && api.progress.total === RAIL.length, await text(page.getByTestId("progress-text")));
      ck("steps with a connection other than the built-in one are marked set", (await page.getByTestId("step-tickets").getAttribute("data-state")) === "set"
         && (await page.getByTestId("step-review").getAttribute("data-state")) === "not_set");
      ck("the first step, how the crew works, is open and current", (await page.getByTestId("step-crew").getAttribute("aria-current")) === "step"
         && (await text(page.getByTestId("page-title"))) === "How your crew works");
      await page.getByTestId("step-tickets").click();
      await page.locator("[data-testid=step-view][data-slot=tickets]").waitFor();
      ck("Tickets opens from the rail with the API's words", (await text(page.getByTestId("page-title"))) === "Tickets" && (await text(page.getByTestId("page-sub"))) === stepOf(api, "tickets").why);
      const rows = await page.getByTestId("connected").locator('[data-testid^="connected-"]').evaluateAll((b) => b.map((x) => x.dataset.testid.slice(10)));
      ck("Connected lists tares, linear and github", rows.join() === "tares,linear,github", rows.join());
      ck("the heading and the tags are the API's", (await text(page.getByTestId("connected").locator("h2"))).toLowerCase() === stepOf(api, "tickets").connected_heading.toLowerCase()
         && (await raw(page.getByTestId("connected-linear").getByTestId("tag-default"))) === "Default"
         && (await raw(page.getByTestId("connected-tares").getByTestId("tag-built_in"))) === "Built in");
      const look = (loc) => loc.evaluate((el) => { const c = getComputedStyle(el); return { family: c.fontFamily, upper: c.textTransform, border: c.borderTopColor, width: c.borderTopWidth, color: c.color }; });
      const [tagLook, headLook] = [await look(page.getByTestId("connected-linear").getByTestId("tag-default")), await look(page.getByTestId("connected").locator("h2"))];
      ck("tags and section heads are small mono capitals", /mono/i.test(tagLook.family) && tagLook.upper === "uppercase" && /mono/i.test(headLook.family) && headLook.upper === "uppercase", JSON.stringify([tagLook, headLook]));
      const [editLook, makeLook, removeLook] = [await look(page.getByTestId("connected-linear").getByTestId("action-edit")), await look(page.getByTestId("connected-tares").getByTestId("action-make_default")), await look(page.getByTestId("connected-github").getByTestId("action-remove"))];
      ck("Make default is a bordered button, Remove has a red border and red words, Edit settings the dark border", makeLook.width === "1px" && removeLook.width === "1px" && editLook.width === "1px"
        && removeLook.border !== makeLook.border && editLook.border !== makeLook.border && removeLook.color !== makeLook.color, JSON.stringify([editLook, makeLook, removeLook]));
      ck("who picks a connection is shown", (await text(page.getByTestId("connected-github").getByTestId("row-use"))) === "Used by project-p.",
        await text(page.getByTestId("connected-github").getByTestId("row-use")));
      ck("a connection that is the default cannot be removed", (await page.getByTestId("connected-linear").getByTestId("action-remove").count()) === 0
         && (await page.getByTestId("connected-linear").getByTestId("action-edit").count()) === 1);
      ck("a step with nothing left to connect has no Connect another section", (await page.getByTestId("available").count()) === 0);
      ck("the footer's button names the next step", (await text(page.getByTestId("step-next"))) === "Next: Review", await text(page.getByTestId("step-next")));
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, "partly set up cell 1440");
      await shot(page, "filled-live-1440.png");
      ck("the page wrote nothing", writes.length === 0);

      // the other steps
      await page.getByTestId("step-review").click();
      ck("a step opens from the rail", (await text(page.getByTestId("page-title"))) === "Review" && (await page.getByTestId("step-review").getAttribute("aria-current")) === "step");
      ck("a step with nothing to connect has no Connect another section", (await page.getByTestId("available").count()) === 0);
      if (RAIL.includes("guardrails")) {
        await page.getByTestId("step-guardrails").click();
        ck("Guardrails shows the API's note as text and offers no form", (await text(page.getByTestId("step-note"))) === stepOf(api, "guardrails").note
           && (await page.getByTestId("action-edit").count()) === 0 && (await page.getByTestId("action-connect").count()) === 0);
      }
      await page.getByTestId("step-terminal").click();
      ck("the last step, the terminal, ends the setup", (await text(page.getByTestId("page-title"))) === "In your terminal"
         && (await text(page.getByTestId("step-next"))) === "Finish setup" && (await page.getByTestId("step-next").getAttribute("href")) === "/");
      ck("the terminal step shows the three commands, done or not, and no planning step", (await page.getByTestId("terminal-panel").getByTestId("start-copy").count()) === 3
         && (await page.locator("[data-testid=start-step][data-key=spec]").count()) === 0);
      await page.getByTestId("step-tickets").click();
      await page.getByTestId("step-next").click();
      ck("Next goes to the next step", (await text(page.getByTestId("page-title"))) === "Review");
      await ctx.close();
    }

    // 2. choosing, filling and saving; the secret is saved and a blank keeps it
    console.log("== choosing, filling and saving ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      await open(page, "tickets");
      // a saved secret: tagged saved, a blank field, a blank save keeps it
      await page.getByTestId("connected-linear").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      const f = page.getByTestId("field-api_key");
      ck("the form is the connection's own, in edit mode", (await text(page.getByTestId("page-title"))) === "Edit Linear" && (await page.getByTestId("setup-form").getAttribute("data-mode")) === "edit");
      ck("a secret is a password field with nothing in it", (await f.getAttribute("data-kind")) === "secret" && (await page.getByTestId("input-api_key").getAttribute("type")) === "password"
         && (await page.getByTestId("input-api_key").inputValue()) === "");
      ck("it says a key is saved once, in the note, and the box has no second sentence", (await text(f.getByTestId("field-saved"))) === "A key is saved. Leave this blank to keep it."
         && (await page.getByTestId("input-api_key").getAttribute("placeholder")) === null && (await text(page.getByTestId("setup-form"))).split("Leave").length === 2);
      ck("its requirement is shown beside the label", (await text(f.getByTestId("field-requirement"))) === "Optional");
      await shot(page, "form-secret-1440.png");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const put = writes.find((w) => w.method === "PUT" && w.path === "/api/factory/integrations/tickets");
      ck("a blank secret is sent blank, so the saved key stays", put && put.body.name === "linear" && put.body.settings.api_key === "" && typeof put.body.default === "boolean", JSON.stringify(put));
      ck("and says what was saved", (await flashText(page)) === "Linear integration settings are saved.", await flashText(page));
      ck("the key is still saved on the cell", connectedOf(await read(page), "tickets", "linear").form.fields[0].saved === true);

      // a text field: required, empty, the API's sentence, nothing sent
      writes.length = 0;
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      ck("a text field holds the saved value", (await page.getByTestId("input-repo").inputValue()) === "example-org/orders-api"
         && (await text(page.getByTestId("field-repo").getByTestId("field-requirement"))) === "Required");
      await page.getByTestId("input-repo").fill("");
      await page.getByTestId("form-save").click();
      ck("an empty required field shows the API's sentence and sends nothing", (await text(page.getByTestId("field-repo").getByTestId("field-error"))) === "Enter the repository as owner/name, for example northwind/orders-api."
         && (await page.getByTestId("input-repo").getAttribute("aria-invalid")) === "true" && writes.length === 0);
      ck("and puts the focus on it", await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("data-testid") === "input-repo"));
      await shot(page, "form-error-1440.png");
      await page.getByTestId("input-repo").fill("example-org/billing");
      ck("typing clears the sentence", (await page.getByTestId("field-repo").getByTestId("field-error").count()) === 0);
      await page.getByTestId("form-save").click();
      await flashed(page);
      const g = writes.find((w) => w.method === "PUT");
      ck("a save sends the new value under its name", g && g.body.name === "github" && g.body.settings.repo === "example-org/billing", JSON.stringify(g));
      ck("the cell has it", connectedOf(await read(page), "tickets", "github").form.fields.find((x) => x.name === "repo").value === "example-org/billing");

      // a cancel sends nothing
      writes.length = 0;
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      await page.getByTestId("input-repo").fill("example-org/other");
      await page.getByTestId("form-cancel").click();
      await page.getByTestId("step-view").waitFor();
      ck("Cancel goes back and saves nothing", writes.length === 0 && (await page.getByTestId("setup-form").count()) === 0);
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      const crumb = page.locator('nav[aria-label="Breadcrumb"]');
      ck("the form has a breadcrumb: the step is the link, then the form's title", (await text(crumb)) === `Tickets / ${await text(page.getByTestId("page-title"))}`
         && (await page.getByTestId("form-back").evaluate((el) => el.tagName)) === "BUTTON" && (await page.getByTestId("form-back").evaluate((el) => getComputedStyle(el).textDecorationLine)) === "underline"
         && (await page.getByTestId("setup-form").getByRole("button", { name: "Cancel" }).evaluate((el) => getComputedStyle(el).borderTopWidth)) === "1px", await text(crumb));
      await page.getByTestId("form-back").click();
      ck("the back link goes to the step", (await text(page.getByTestId("page-title"))) === "Tickets");

      // an API refusal shows its sentence in the form
      await page.route("**/api/factory/integrations/tickets", (r) => r.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "The repository was not found." }) }));
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      await page.getByTestId("form-save").click();
      await page.getByTestId("form-error").waitFor();
      ck("a refused save shows the API's sentence and stays on the form", (await text(page.getByTestId("form-error"))).includes("The repository was not found.") && (await page.getByTestId("setup-form").count()) === 1);
      await page.unroute("**/api/factory/integrations/tickets");

      // the repository is picked from the repos GitHub lists; a saved one it no longer lists stays a choice
      writes.length = 0;
      const asked = [];
      await page.route("**/api/factory/setup/github/repos**", (r) => {
        asked.push(new URL(r.request().url()).search);
        r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ repos: ["example-org/api", "example-org/web"], error: "" }) });
      });
      await page.getByTestId("form-cancel").click();
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      const pick = page.getByTestId("field-repo").getByRole("combobox");
      await pick.waitFor();
      ck("with repos listed, the repository is a dropdown with the saved one chosen",
        (await text(pick)) === "example-org/billing" && (await page.getByTestId("field-repo").locator("input").count()) === 0, await text(pick));
      ck("its help says what to choose", (await text(page.getByTestId("field-repo"))).includes("The repository that holds the issues.")
        && !(await text(page.getByTestId("field-repo"))).includes("owner/name"));
      ck("the list is asked for with the way of connecting", asked.length >= 1 && asked[0].includes("via="), asked.join());
      await pick.click();
      ck("it offers the listed repos and the saved one", (await page.getByRole("option").allInnerTexts()).join() === "example-org/billing,example-org/api,example-org/web",
        (await page.getByRole("option").allInnerTexts()).join());
      await shot(page, "form-repo-pick-1440.png");
      await page.getByRole("option", { name: "example-org/web" }).click();
      await page.getByTestId("form-save").click();
      await flashed(page);
      const pw = writes.find((w) => w.method === "PUT");
      ck("a picked repository is saved", pw && pw.body.settings.repo === "example-org/web", JSON.stringify(pw));
      await page.unroute("**/api/factory/setup/github/repos**");
      await ctx.close();
    }

    // 3. connecting a new integration: a secret and a url; a bool; it becomes the default
    console.log("== connecting ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      await open(page, "ci");
      ck("CI says it is chosen per project while nothing is chosen here", (await text(page.getByTestId("connected-local").getByTestId("row-title"))) === "Automatic");
      ck("the Connect another list names what can be connected", (await page.getByTestId("available").locator('[data-testid^="available-"]').evaluateAll((b) => b.map((x) => x.dataset.testid.slice(10)))).join() === "github");
      await page.getByTestId("available-github").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
      ck("the form says how it connects, and asks nothing: no repo-only setting here", (await text(page.getByTestId("page-title"))) === "Connect GitHub Actions"
         && (await page.getByTestId("setup-form").getAttribute("data-mode")) === "connect" && (await text(page.getByTestId("form-save"))) === "Save and connect"
         && (await page.getByTestId("field-no_ci").count()) === 0);
      await shot(page, "form-connect-1440.png");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const put = writes.find((w) => w.method === "PUT");
      ck("a save sends the name, no settings and the form's default flag", put && put.path === "/api/factory/integrations/ci" && put.body.name === "github"
         && JSON.stringify(put.body.settings) === "{}" && put.body.default === true, JSON.stringify(put));
      ck("it says it is connected and shows it connected, as the default", (await flashText(page)) === "GitHub Actions is connected."
         && (await page.getByTestId("connected-github").getByTestId("tag-default").count()) === 1);
      ck("the rail follows", (await text(page.getByTestId("step-ci").getByTestId("step-line"))).startsWith("GitHub Actions"));
      await page.getByTestId("flash").getByRole("button", { name: "Dismiss" }).click();
      ck("the note can be dismissed", (await page.getByTestId("flash").count()) === 0);
      await ctx.close();
    }
    {
      // a bool, from a sample: the live cell has none left to fill at the factory
      const { ctx, page, writes } = await newPage(1440, { sampleFlag: true });
      await page.route("**/api/factory/integrations/**", (r) => r.fulfill({ json: {} }));
      await page.goto(`${BASE}/setup?step=ci&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-github").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
      const box = page.getByTestId("input-no_ci");
      ck("a bool is a checkbox with its label", (await page.getByTestId("field-no_ci").getAttribute("data-kind")) === "bool" && (await box.getAttribute("role")) === "checkbox"
         && (await box.getAttribute("aria-checked")) === "false");
      await page.getByTestId("field-no_ci").getByText("This repository has no CI").click();
      ck("its label toggles it", (await box.getAttribute("aria-checked")) === "true");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const b = writes.find((w) => w.method === "PUT");
      ck("a bool is sent as a real boolean", b && b.path === "/api/factory/integrations/ci" && b.body.settings.no_ci === true && b.body.name === "github", JSON.stringify(b));
      await ctx.close();
    }

    // 4. make default, and remove with the question
    console.log("== make default and remove ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      await open(page, "tickets");
      await page.getByTestId("connected-github").getByTestId("action-remove").click();
      await page.getByTestId("confirm").waitFor();
      const api = await read(page);
      const ask = connectedOf(api, "tickets", "github").confirm;
      ck("the question is the API's, and names the project that moves", (await text(page.getByTestId("confirm"))).includes(ask.title) && (await text(page.getByTestId("confirm"))).includes(ask.text) && ask.text.includes("project-p"));
      ck("the keyboard starts on the safe answer", await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("data-testid") === "confirm-no"));
      await shot(page, "confirm-1440.png");
      await page.getByTestId("confirm-no").click();
      ck("Keep it closes it and removes nothing", (await page.getByTestId("confirm").count()) === 0 && writes.length === 0 && (await page.getByTestId("connected-github").count()) === 1);
      ck("and the focus goes back to Remove", await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("data-testid") === "action-remove"));
      await page.getByTestId("connected-github").getByTestId("action-remove").click();
      await page.getByTestId("confirm-yes").click();
      await flashed(page);
      ck("the project moves to the default first, then the connection goes", writes.map((w) => `${w.method} ${w.path}`).join("; ")
         === `DELETE /api/projects/${ids["project-p"]}/integrations/tickets; DELETE /api/factory/integrations/tickets/connections/github`, writes.map((w) => `${w.method} ${w.path}`).join("; "));
      ck("the row is gone and the note says so", (await page.getByTestId("connected-github").count()) === 0 && (await flashText(page)) === "GitHub Issues is removed.", await flashText(page));
      ck("the focus is not lost: it moves to the title", await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("data-testid") === "page-title"));
      // make default
      writes.length = 0;
      await page.getByTestId("connected-tares").getByTestId("action-make_default").click();
      await flashed(page);
      ck("Make default sends the name", writes.length === 1 && writes[0].method === "PUT" && writes[0].path === "/api/factory/integrations/tickets/default" && writes[0].body.name === "tares", JSON.stringify(writes));
      ck("the tag moves and the note says so", (await page.getByTestId("connected-tares").getByTestId("tag-default").count()) === 1 && (await page.getByTestId("connected-linear").getByTestId("tag-default").count()) === 0
         && (await flashText(page)) === "Tares is now the default for Tickets.", await flashText(page));
      await ctx.close();
    }

    // 5. loading, an error and its retry, with the live route
    console.log("== loading and error ==");
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      let release;
      const gate = new Promise((r) => { release = r; });
      await page.route("**/api/factory/setup", async (r) => { await gate; await r.continue(); });
      await page.goto(`${BASE}/setup`, { waitUntil: "load" });
      await page.getByTestId("setup-loading").waitFor();
      ck(`while the read is out, the screen shows a skeleton at ${width}`, (await page.getByTestId("setup-loading").getAttribute("aria-busy")) === "true" && (await page.getByTestId("step-view").count()) === 0);
      ck(`loading: no sideways scroll at ${width}`, (await overflow(page)) <= 0, String(await overflow(page)));
      ck(`loading: the skeleton stays inside the screen at ${width}`, await page.getByTestId("setup-loading").evaluate((el) => el.getBoundingClientRect().right <= window.innerWidth + 0.5));
      await checkWords(page, ck, `loading ${width}`);
      await shot(page, `loading-${width}.png`);
      release();
      await page.getByTestId("step-view").waitFor();
      ck(`then the first step at ${width}`, (await text(page.getByTestId("page-title"))) === "How your crew works");
      await page.unroute("**/api/factory/setup");
      await ctx.close();
    }
    for (const width of [1440, 390]) {
      const { ctx, page } = await newPage(width);
      let down = true;
      await page.route("**/api/factory/setup", (r) => (down ? r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "boom" }) }) : r.continue()));
      const one = expectOneCall(page, "/api/factory/setup");
      await page.goto(`${BASE}/setup`, { waitUntil: "load" });
      await page.getByTestId("setup-error").waitFor();
      await one(ck, `error ${width}`);
      ck(`a failed read says so and offers Try again at ${width}`, (await text(page.getByTestId("setup-error"))).startsWith("The setup could not be loaded.") && (await page.getByTestId("setup-retry").count()) === 1
         && (await page.getByTestId("step-view").count()) === 0);
      ck(`the error has a bold lead, the cause, and Try again right after the text at ${width}`, (await page.getByTestId("setup-error").locator("p > b").innerText()) === "The setup could not be loaded."
         && (await text(page.getByTestId("setup-error"))).includes("Nothing was changed.") && (await page.getByTestId("setup-error").locator("p > button").count()) === 1
         && (await text(page.getByTestId("setup-error"))).endsWith("Try again"));
      ck(`the error shows no raw status at ${width}`, !(await text(page.getByTestId("setup"))).includes("500") && !(await text(page.getByTestId("setup"))).includes("boom"));
      ck(`no sideways scroll at ${width}`, (await overflow(page)) <= 0);
      await checkWords(page, ck, `error ${width}`);
      await shot(page, `error-${width}.png`);
      down = false;
      await page.getByTestId("setup-retry").click();
      await page.getByTestId("step-view").waitFor();
      ck(`Try again reads it once more and shows the setup at ${width}`, (await text(page.getByTestId("page-title"))) === "How your crew works");
      await ctx.close();
    }

    // 6. a narrow screen: the steps are a list under the title
    console.log("== 390 px ==");
    {
      const { ctx, page } = await newPage(390);
      await open(page, null);
      ck("the rail is a closed list with the current step", (await page.getByTestId("rail").isVisible()) === false && (await page.getByTestId("rail-narrow").isVisible())
         && /^Step 1 of \d+: How your crew works/.test(await text(page.getByTestId("rail-narrow").locator("summary"))), await text(page.getByTestId("rail-narrow").locator("summary")));
      await page.getByTestId("rail-narrow").locator("summary").click();
      await page.getByTestId("nstep-tickets").click();
      ck("on a phone the crumb is hidden and Exit setup stays in the header", !(await page.getByTestId("setup-title").isVisible()) && (await page.getByTestId("setup-exit").isVisible()));
      ck("a step picked in the list opens and closes the list", (await text(page.getByTestId("page-title"))) === "Tickets" && (await page.getByTestId("rail-narrow").getAttribute("open")) === null);
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, "partly set up cell 390");
      await shot(page, "filled-live-390.png");
      await page.getByTestId("connected-linear").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      ck("the form fits the screen", (await overflow(page)) <= 0);
      await shot(page, "form-390.png");
      await ctx.close();
    }

    // 7. the states a plain cell cannot be in: the samples, every field kind, long and many
    console.log("== samples ==");
    await sampleState("empty", "empty", "/setup?step=tickets&sample=empty", async (page, width, name) => {
      const lines = [];
      for (const slot of SLOTS) lines.push((await page.getByTestId("rail").getByTestId(`step-${slot}`).getByTestId("step-line").textContent()).trim());
      ck(`${name}: every integration step but the runtime says it is not set yet`, lines.filter((l) => l.endsWith("(not set yet)")).length === 6 && !lines[6].endsWith("(not set yet)"), lines.join("|"));
    });
    await sampleState("filled", "filled", "/setup?step=tickets&sample=filled");
    await sampleState("long", "long", "/setup?step=tickets&sample=long", async (page, width, name) => {
      ck(`${name}: the long project names are shown in full and wrap`, (await text(page.getByTestId("connected-linear").getByTestId("row-use"))).includes("orders-api-with-a-very-long-project-name-for-the-checkout-flow")
         && (await overflow(page)) <= 0);
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      await page.getByTestId("input-repo").scrollIntoViewIfNeeded();
      ck(`${name}: the form's long text stays inside its box`, (await page.getByTestId("setup-form").evaluate((el) => el.scrollWidth <= el.clientWidth + 1)));
    });
    await sampleState("many", "many", "/setup?step=tickets&sample=many", async (page, width, name) => {
      ck(`${name}: twelve connections are listed, one picked by twelve projects`, (await page.getByTestId("connected").locator('[data-testid^="connected-"]').count()) === 12, String(await page.getByTestId("connected").locator('[data-testid^="connected-"]').count()));
    });
    await widths("the licence ended", async ({ page }, width, name) => {
      await page.goto(`${BASE}/setup?step=tickets&sample=expired`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck(`${name}: the step's banner is the API's text`, (await text(page.getByTestId("step-banner"))).includes("Your licence ended on 3 November."));
      ck(`${name}: the connection says its licence ended`, (await raw(page.getByTestId("connected-linear").getByTestId("tag-licence_ended"))) === "Licence ended");
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0);
      await checkWords(page, ck, name);
      await shot(page, `expired-${width}.png`);
    });
    await widths("a fresh cell", async ({ page }, width, name) => {
      await page.goto(`${BASE}/setup?step=tickets&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      ck(`${name}: an integration that needs a licence is listed, with its reason, and cannot be connected`,
        (await page.getByTestId("available-jira").getByTestId("tag-not_licensed").count()) === 1 && (await page.getByTestId("available-jira").getByTestId("row-reason").count()) === 1
        && (await page.getByTestId("available-jira").getByTestId("action-connect").isDisabled()));
      ck(`${name}: the licence tags read Enterprise and Not licensed`, (await raw(page.getByTestId("available-jira").getByTestId("tag-enterprise"))) === "Enterprise");
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0);
      await shot(page, `empty-tickets-${width}.png`);
    });

    // every kind of field, from the samples: select and mapping cannot be had on a plain cell
    console.log("== every field kind ==");
    {
      const { ctx, page, writes } = await newPage(1440, { sampleFlag: true });
      await page.route("**/api/factory/integrations/**", (r) => r.fulfill({ json: {} }));
      await page.goto(`${BASE}/setup?step=tickets&sample=filled`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      // a mapping, with rows
      await page.getByTestId("connected-jira").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      ck("a mapping is a group with a row per key", (await page.getByTestId("field-status").getAttribute("data-kind")) === "mapping" && (await page.getByTestId("field-status").locator('[data-testid^="row-"]').count()) === 4);
      ck("each row has its label and its own input, named for the screen reader", (await page.getByTestId("input-status-in_review").getAttribute("aria-label")) !== null
         && (await page.getByTestId("input-status-in_review").inputValue()) === "In Review");
      ck("a mapping has its label, requirement and help", (await text(page.getByTestId("field-status").getByTestId("field-requirement"))) === "Required"
         && (await text(page.getByTestId("field-status"))).includes("Match each Tares stage"));
      ck("a url, a text and a secret are drawn too", (await page.getByTestId("field-site").getAttribute("data-kind")) === "url" && (await page.getByTestId("field-email").getAttribute("data-kind")) === "text"
         && (await page.getByTestId("field-token").getAttribute("data-kind")) === "secret");
      await shot(page, "form-mapping-1440.png");
      await page.getByTestId("input-status-done").fill("");
      await page.getByTestId("form-save").click();
      ck("an empty row shows the sentence and marks only that row", (await text(page.getByTestId("field-status").getByTestId("field-error"))) === "Enter the status for each stage."
         && (await page.getByTestId("input-status-done").getAttribute("aria-invalid")) === "true" && (await page.getByTestId("input-status-todo").getAttribute("aria-invalid")) === null
         && writes.length === 0);
      await page.getByTestId("input-status-done").fill(" Closed ");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const m = writes.find((w) => w.method === "PUT");
      ck("a mapping is sent as an object, trimmed", m && m.body.name === "jira" && JSON.stringify(m.body.settings.status) === JSON.stringify({ todo: "To Do", in_progress: "In Progress", in_review: "In Review", done: "Closed" })
         && m.body.settings.token === "", JSON.stringify(m));
      // a select
      writes.length = 0;
      await page.getByTestId("step-memory").click();
      await page.getByTestId("connected-cognee").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      const sel = page.getByTestId("input-mode");
      ck("a select shows its value", (await page.getByTestId("field-mode").getAttribute("data-kind")) === "select" && (await text(sel)) === "Balanced");
      await sel.click();
      const opts = await page.getByRole("option").allInnerTexts();
      ck("it lists the API's options", opts.map((o) => o.trim()).join() === "Fast,Balanced,Thorough", opts.join());
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "form-select-open-1440.png") });   // not full page: that closes the list
      await page.getByRole("option", { name: "Thorough" }).click();
      ck("choosing one shows it", (await text(sel)) === "Thorough");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const s = writes.find((w) => w.method === "PUT");
      ck("a select is sent as its option", s && s.body.name === "cognee" && s.body.settings.mode === "Thorough" && s.body.settings.url === "https://cognee.northwind.example", JSON.stringify(s));
      await ctx.close();
    }

    // Tares Cloud: the app block
    console.log("== the app ==");
    {
      const { ctx, page } = await newPage(1440, { sampleFlag: true });
      // the app's connect page: answered here, so the test sees where the browser was sent and goes nowhere
      let sent = "";
      await page.route("https://connect.example.test/**", (r) => { sent = r.request().url(); return r.fulfill({ contentType: "text/html", body: "<p>connect page</p>" }); });
      await page.goto(`${BASE}/setup?step=tickets&sample=cloud`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("app-block").waitFor();
      ck("a connection with an app starts with the app", (await text(page.getByTestId("app-block"))).includes("Approve the GlassFlow Linear app.") && (await page.getByTestId("field-api_key").count()) === 0
         && (await page.getByTestId("form-save").count()) === 0);
      ck("the start has one Cancel, and the way to the key is a line of its own below the buttons", (await page.getByTestId("form-cancel").count()) === 1 && (await page.getByTestId("app-use-key").count()) === 1
         && (await page.getByTestId("app-open").evaluate((a) => a.getBoundingClientRect().bottom)) <= (await page.getByTestId("app-use-key").evaluate((a) => a.getBoundingClientRect().top)));
      await shot(page, "app-start-1440.png");
      await page.getByTestId("app-open").click();
      await page.waitForURL((u) => u.hostname === "connect.example.test");
      const back = new URL(sent).searchParams.get("return") || "";
      ck("Open the app goes to its connect page in this window, with this step to come back to",
        new URL(sent).pathname === "/linear" && new URL(back).origin === BASE && new URL(back).pathname === "/setup"
        && new URL(back).searchParams.get("step") === "tickets", sent);
      await page.goto(`${BASE}/setup?step=tickets&sample=cloud`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("app-block").waitFor();
      await page.getByTestId("app-use-key").click();
      ck("the key is one click away", (await page.getByTestId("field-api_key").count()) === 1 && (await page.getByTestId("form-save").count()) === 1 && (await page.getByTestId("app-switch").count()) === 1);
      await page.getByTestId("app-use-app").click();
      ck("and so is the app", (await page.getByTestId("app-block").count()) === 1);
      await checkWords(page, ck, "app start");
      await page.getByTestId("form-cancel").click();
      await page.goto(`${BASE}/setup?step=tickets&sample=ghdone`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-github").getByTestId("action-connect").click();
      await page.getByTestId("app-done").waitFor();
      ck("a finished app asks only for what it cannot know", (await text(page.getByTestId("app-done"))).startsWith("GitHub is connected.") && (await page.getByTestId("field-repo").count()) === 1
         && (await page.getByTestId("field-credential").count()) === 0 && (await page.getByTestId("input-repo").count()) === 1);
      ck("and saves it as this step's integration", (await text(page.getByTestId("form-save"))) === "Save and connect");
      ck("the green line is under the fields and just above Save and connect", await page.evaluate(() => {
        const d = document.querySelector('[data-testid="app-done"]'), f = document.querySelector('[data-testid="field-repo"]'), b = document.querySelector('[data-testid="form-save"]');
        return !!(d && f && b && (f.compareDocumentPosition(d) & Node.DOCUMENT_POSITION_FOLLOWING) && (d.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
          && getComputedStyle(d).color !== getComputedStyle(document.body).color);
      }));
      await shot(page, "app-done-1440.png");
      await checkWords(page, ck, "app done");
      await page.goto(`${BASE}/setup?step=tickets&sample=approw`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("a row connected with the app says so, and is disconnected, not removed", (await page.getByTestId("connected-linear").getByTestId("tag-app").count()) === 1
         && (await text(page.getByTestId("connected-linear").getByTestId("action-remove"))) === "Disconnect" && (await page.getByTestId("connected-linear").getByTestId("action-edit").count()) === 0);
      await ctx.close();
    }

    // back from the app's connect page: the step opens the app's form by itself, done, and one click saves it as
    // the step's integration (the app alone only lets the cell in, it is not yet where the tickets live)
    console.log("== back from the app ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      let pick = null, saved = false;
      const patch = (d) => {
        const st = d.steps.find((x) => x.slot === "tickets");
        if (!pick) pick = JSON.parse(JSON.stringify(st.available.find((a) => a.id === "linear" && a.form && a.allowed) || st.available.find((a) => a.form && a.allowed)));
        const base = st.available.filter((a) => a.id !== pick.id);
        const app = { url: "https://connect.example.test/linear?workspace=x", service: pick.title, app_name: `GlassFlow ${pick.title} app`, button: `Connect ${pick.title}`, repo_step: false,
                      steps: ["Approve the app."], note: "Nothing else to set up.", use_key_label: "Use an API key instead", use_app_label: "Use the app instead",
                      stage: "done", done: `${pick.title} is connected through the GlassFlow ${pick.title} app.` };
        if (!saved) st.available = [{ ...pick, app, form: { ...pick.form, fields: [] } }, ...base];
        else {
          st.available = base;
          st.connected = [...st.connected, { id: pick.id, title: pick.title, description: pick.description, tags: [{ key: "app", text: "App" }], default: false, built_in: false, via: "app", who: "",
            picked_by: [], follows: 0, use: "", actions: [{ key: "remove", label: "Disconnect" }], blocked: null, confirm: null, form: null, last_test: null }];
        }
      };
      await page.route("**/api/factory/setup", async (r) => {
        const d = await (await r.fetch()).json();
        patch(d);
        await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(d) });
      });
      await page.route("**/api/factory/integrations/tickets", async (r) => { saved = true; await r.fulfill({ status: 200, contentType: "application/json", body: "{}" }); });
      patch(await read(page));    // what to pick: the first integration of Tickets that can be connected
      await page.goto(`${BASE}/setup?step=tickets&cloud=${pick.id}-connected`, { waitUntil: "load" });
      await page.getByTestId("setup-form").waitFor({ timeout: 10000 }).catch(() => {});
      ck("the cloud's word is dropped from the address, so a reload does not open the form again", !new URL(page.url()).searchParams.has("cloud"), page.url());
      ck("and the app's form is open again, done, with Save and connect", (await page.getByTestId("setup-form").count()) === 1 && (await page.getByTestId("app-done").count()) === 1
         && (await text(page.getByTestId("form-save"))) === "Save and connect");
      await shot(page, "app-back-1440.png");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const put = writes.find((w) => w.method === "PUT" && w.path === "/api/factory/integrations/tickets");
      ck("saving sends the app's integration for the step", put && put.body.name === pick.id, JSON.stringify(put));
      ck("and the step lists it as connected", (await page.getByTestId(`connected-${pick.id}`).count()) === 1 && (await flashText(page)) === `${pick.title} is connected.`, await flashText(page));
      await ctx.close();
    }

    // Test connection (TR-1095): the settings in the form go to the service and nothing is saved. The answers a real service
    // gives (a refused key, a connected account) are the route's, replaced here by the answer the mock shows.
    console.log("== test connection ==");
    const FAILED = { ok: false, message: "Linear said the API key is not valid. Check the key and try again." };
    const PASSED = { ok: true, message: "Connected as Dana Whitfield, workspace Northwind." };
    const testOpen = async (page) => {
      await page.goto(`${BASE}/setup?step=tickets&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
    };
    const testRoute = async (page, answer, calls) => {
      await page.route("**/api/factory/integrations/*/test", async (r) => {
        calls.push({ path: new URL(r.request().url()).pathname, body: JSON.parse(r.request().postData() || "null") });
        await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
      });
    };
    const testState = async (label, file, answer, run) => widths(label, async ({ page, writes }, width, name) => {
      const calls = [];
      await testRoute(page, answer, calls);
      await testOpen(page);
      ck(`${name}: before a test the form says what the button does, and nothing is shown as a result`,
        (await text(page.getByTestId("test-help"))) === "Test connection checks the settings above with Linear. Nothing is saved by it." && (await page.getByTestId("test-line").count()) === 0);
      ck(`${name}: the Test connection button is below the fields and above Save`,
        await page.evaluate(() => { const y = (id) => document.querySelector(`[data-testid=${id}]`).getBoundingClientRect().top;
          return y("input-api_key") < y("test-connection") && y("test-connection") < y("form-save"); }));
      await page.getByTestId("input-api_key").fill("  lin_api_invented  ");
      await page.getByTestId("test-connection").click();
      await page.getByTestId("test-line").waitFor();
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0, String(await overflow(page)));
      await checkWords(page, ck, name);
      await shot(page, `${file}-${width}.png`);
      await run(page, calls, writes, name);
    });
    await testState("connection test failed", "test-failed", FAILED, async (page, calls, writes, name) => {
      const line = page.getByTestId("test-line");
      ck(`${name}: the line says Failed and then the integration's own sentence`, (await text(line)) === `Failed. ${FAILED.message}`, await text(line));
      ck(`${name}: it is inside the status region for the screen reader, drawn in the error colours`, (await line.evaluate((el) => el.parentElement.getAttribute("role"))) === "status" && (await line.getAttribute("data-ok")) === "false"
         && (await line.evaluate((el) => getComputedStyle(el).fontWeight)) === "600");
      ck(`${name}: the settings went to the test route, trimmed, and nothing was saved`, calls.length === 1 && calls[0].path === "/api/factory/integrations/tickets/test"
         && calls[0].body.name === "linear" && calls[0].body.settings.api_key === "lin_api_invented" && writes.filter((w) => w.method !== "POST" || !w.path.endsWith("/test")).length === 0, JSON.stringify([calls, writes]));
      await page.getByTestId("input-api_key").fill("lin_api_other");
      ck(`${name}: changing a field takes the result away, because it belongs to the old settings`,
        (await page.getByTestId("test-line").count()) === 0 && (await page.getByTestId("test-help").count()) === 1);
    });
    await testState("connection test passed", "test-passed", PASSED, async (page, calls, writes, name) => {
      const line = page.getByTestId("test-line");
      ck(`${name}: the line says Passed and then the integration's own sentence`, (await text(line)) === `Passed. ${PASSED.message}`, await text(line));
      ck(`${name}: it is drawn in the ok colours, not the error ones`, (await line.getAttribute("data-ok")) === "true" && (await line.evaluate((el) => getComputedStyle(el).fontWeight)) !== "600");
      ck(`${name}: a pass saves nothing: the form is still open and Save is still the way to keep it`,
        (await page.getByTestId("form-save").isVisible()) && writes.filter((w) => w.method === "PUT").length === 0);
    });
    {
      // a test with a required field empty asks for it and sends nothing
      const { ctx, page, writes } = await newPage(1440, { sampleFlag: true });
      const calls = [];
      await testRoute(page, PASSED, calls);
      await testOpen(page);
      await page.getByTestId("test-connection").click();
      await page.getByTestId("test-line").waitFor();
      ck("an empty required field: the line asks for it and marks the field, and no test is sent",
        (await text(page.getByTestId("test-line"))) === "Failed. Fill in the required settings first." && (await page.getByTestId("input-api_key").getAttribute("aria-invalid")) === "true"
        && calls.length === 0 && writes.length === 0, JSON.stringify([calls, writes]));
      await ctx.close();
    }
    {
      // a refusal from the cell shows as a failed test in the same place, with the cell's words
      const { ctx, page } = await newPage(1440, { sampleFlag: true });
      await page.route("**/api/factory/integrations/*/test", (r) => r.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "The address is not a web address." }) }));
      await testOpen(page);
      await page.getByTestId("input-api_key").fill("lin_api_invented");
      await page.getByTestId("test-connection").click();
      await page.getByTestId("test-line").waitFor();
      ck("a refusal from the cell is a failed test with its sentence", (await text(page.getByTestId("test-line"))) === "Failed. The address is not a web address.", await text(page.getByTestId("test-line")));
      await ctx.close();
    }
    {
      // the live route: a key the cell cannot use never saves anything, and the answer comes back as the same line
      const { ctx, page, writes } = await newPage(1440);
      await page.goto(`${BASE}/setup?step=tickets`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      const before = JSON.stringify(await read(page));
      await page.getByTestId("available-github").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
      if (await page.getByTestId("app-use-key").count()) await page.getByTestId("app-use-key").click();
      await page.getByTestId("input-repo").fill("not a repository");
      const sent = page.waitForResponse((r) => r.url().includes("/test") && r.request().method() === "POST");
      await page.getByTestId("test-connection").click();
      const r = await sent;
      await page.getByTestId("test-line").waitFor();
      ck("the live test route answers, the page shows its sentence as a failed test, and the cell is unchanged",
        r.status() < 500 && (await text(page.getByTestId("test-line"))).startsWith("Failed. ") && JSON.stringify(await read(page)) === before
        && writes.every((w) => w.method === "POST" && w.path.endsWith("/test")), `${r.status()} ${await text(page.getByTestId("test-line"))}`);
      await ctx.close();
    }
    {
      // a test answers for the settings it ran with: while it runs the button is busy and a second press sends nothing,
      // and an answer that comes back after a field changed is not shown
      const { ctx, page } = await newPage(1440, { sampleFlag: true });
      let release; const gate = new Promise((res) => { release = res; });
      const calls = [];
      await page.route("**/api/factory/integrations/*/test", async (r) => {
        calls.push(JSON.parse(r.request().postData() || "null"));
        if (calls.length === 1) await gate;
        await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PASSED) });
      });
      await testOpen(page);
      const status = page.getByTestId("test-status");
      ck("the result's live region is on the page before any test, and empty", (await status.getAttribute("role")) === "status" && (await text(status)) === "");
      await page.getByTestId("input-api_key").fill("lin_api_first");
      const btn = page.getByTestId("test-connection");
      await btn.click();
      await page.waitForFunction(() => document.querySelector("[data-testid=test-connection]")?.getAttribute("aria-busy") === "true");
      await btn.evaluate((el) => el.click());
      await page.waitForTimeout(300);
      ck("while a test runs the button is busy and a second press sends nothing", (await btn.getAttribute("aria-busy")) === "true" && calls.length === 1, String(calls.length));
      await page.getByTestId("input-api_key").fill("lin_api_second");
      ck("changing a field while a test runs ends the wait: the button is free again and nothing is shown",
        (await btn.getAttribute("aria-busy")) === null && (await page.getByTestId("test-line").count()) === 0 && (await page.getByTestId("test-help").count()) === 1);
      release();
      await page.waitForTimeout(400);
      ck("the answer for the old settings is dropped when it arrives", (await page.getByTestId("test-line").count()) === 0 && (await page.getByTestId("test-help").count()) === 1);
      await btn.click();
      await page.getByTestId("test-line").waitFor();
      ck("a new test for the new settings is shown", (await text(page.getByTestId("test-line"))) === `Passed. ${PASSED.message}` && calls.length === 2 && calls[1].settings.api_key === "lin_api_second", JSON.stringify(calls));
      ck("and it is inside the live region", (await status.getByTestId("test-line").count()) === 1);
      await ctx.close();
    }
    {
      // an app connection has no key to try: no Test connection button while the app is the way in
      const { ctx, page } = await newPage(1440, { sampleFlag: true });
      await page.goto(`${BASE}/setup?step=tickets&sample=cloud`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
      ck("with the app as the way in, there is no Test connection", (await page.getByTestId("test-connection").count()) === 0);
      await page.getByTestId("app-use-key").click();
      ck("and with a key as the way in, there is", (await page.getByTestId("test-connection").count()) === 1);
      // a result belongs to the way it was got: leave the key for the app and come back, and it is gone
      let release; const gate = new Promise((res) => { release = res; });
      let n = 0;
      await page.route("**/api/factory/integrations/*/test", async (r) => {
        n += 1;
        if (n === 2) await gate;
        await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PASSED) });
      });
      await page.getByTestId("input-api_key").fill("lin_api_invented");
      await page.getByTestId("test-connection").click();
      await page.getByTestId("test-line").waitFor();
      await page.getByTestId("app-use-app").click();
      await page.getByTestId("app-use-key").click();
      ck("after the app and back to the key, the earlier result is gone", (await page.getByTestId("test-line").count()) === 0 && (await page.getByTestId("test-help").count()) === 1);
      await page.getByTestId("test-connection").click();
      await page.waitForFunction(() => document.querySelector("[data-testid=test-connection]")?.getAttribute("aria-busy") === "true");
      await page.getByTestId("app-use-app").click();
      await page.getByTestId("app-use-key").click();
      release();
      await page.waitForTimeout(400);
      ck("a test still running when the way in changed shows nothing when it answers", (await page.getByTestId("test-line").count()) === 0 && (await page.getByTestId("test-connection").getAttribute("aria-busy")) === null);
      await ctx.close();
    }

    // an integration the cell has no licence for
    console.log("== enterprise integration not licensed ==");
    await widths("enterprise integration not licensed", async ({ page }, width, name) => {
      await page.goto(`${BASE}/setup?step=tickets&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      const row = page.getByTestId("available-jira");
      const reason = row.getByTestId("row-reason");
      const said = (await raw(reason)).replace(/\s+/g, " ");
      ck(`${name}: Jira is in the list with the tag Enterprise by its name, and Not licensed at the start of the reason`,
        (await raw(row.getByTestId("tag-enterprise"))) === "Enterprise" && (await row.getByTestId("tag-not_licensed").count()) === 1
        && (await reason.getByTestId("tag-not_licensed").count()) === 1 && (await raw(reason.getByTestId("tag-not_licensed"))) === "Not licensed"
        && (await row.getByTestId("tag-enterprise").evaluate((el) => !!el.closest("[data-testid=row-reason]"))) === false);
      ck(`${name}: the reason reads Not licensed, then the API's words, and says the crew keeps its built-in choice`,
        said.startsWith("Not licensed This cell has no licence. Without it, Jira cannot be connected.") && said.includes("built-in choice"), said);
      ck(`${name}: the tag sits on the same line as the first words of the reason`,
        await reason.evaluate((el) => { const t = el.querySelector("[data-testid=tag-not_licensed]").getBoundingClientRect(); const r = document.createRange();
          r.setStart(el.lastChild, 1); r.setEnd(el.lastChild, 2); const w = r.getBoundingClientRect(); return Math.abs((t.top + t.bottom) / 2 - (w.top + w.bottom) / 2) < t.height; }));
      ck(`${name}: it links to the enterprise licence page, in a new tab`, (await row.getByTestId("row-licence-link").getAttribute("href")) === "https://docs.glassflow.ai/tares/enterprise"
         && (await row.getByTestId("row-licence-link").getAttribute("target")) === "_blank" && (await text(row.getByTestId("row-licence-link"))) === "About the enterprise licence");
      ck(`${name}: it cannot be connected`, await row.getByTestId("action-connect").isDisabled());
      ck(`${name}: a row that can be connected has no licence link`, (await page.getByTestId("available-linear").getByTestId("row-licence-link").count()) === 0);
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0);
      await checkWords(page, ck, name);
      await shot(page, `not-licensed-${width}.png`);
    });
    {
      // Tares Cloud needs no licence from the person: the row stays marked and cannot be connected, but it links nowhere
      const { ctx, page } = await newPage(1440, { sampleFlag: true });
      await page.route(/\/health(\?|$)/, async (r) => {
        const res = await r.fetch();
        await r.fulfill({ response: res, json: { ...(await res.json()), workspace_api_url: "https://workspaces.example.invalid" } });
      });
      await page.goto(`${BASE}/setup?step=tickets&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      const row = page.getByTestId("available-jira");
      ck("on Tares Cloud: Jira is still in the list, marked, and cannot be connected",
        (await row.getByTestId("tag-not_licensed").count()) === 1 && await row.getByTestId("action-connect").isDisabled());
      ck("on Tares Cloud: no link to the enterprise licence page", (await row.getByTestId("row-licence-link").count()) === 0 && (await page.locator(`a[href="https://docs.glassflow.ai/tares/enterprise"]`).count()) === 0);
      await ctx.close();
    }

    // the steps around the integrations: how the crew works (saved on confirm, merging on its own asked
    // first) and the terminal commands, which tick by themselves as the machine reports
    console.log("== the crew and the terminal ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      await open(page, null);
      ck("setup opens on how the crew works", (await page.locator("[data-testid=step-view][data-slot=crew]").count()) === 1
         && (await page.getByTestId("step-crew").getAttribute("data-state")) === "not_set");
      ck("the form starts on your approval for each merge", await page.getByTestId("choice-L3-review").isChecked());
      await page.getByTestId("choice-L4-ship").click();
      ck("the crew merging on its own is asked first, with no Save beside it", (await page.getByTestId("merge-confirm-box").isVisible())
         && (await page.getByTestId("choices-save").count()) === 0);
      await shot(page, "crew-ask-1440.png");
      await page.getByTestId("merge-cancel").click();
      ck("Keep my approval goes back to it", (await page.getByTestId("merge-confirm-box").count()) === 0 && await page.getByTestId("choice-L3-review").isChecked());
      const n0 = Number(await page.getByTestId("builders-count").innerText());
      await page.getByTestId("builders-more").click();
      ck("builders count up", Number(await page.getByTestId("builders-count").innerText()) === n0 + 1);
      ck("nothing is saved before the button", writes.length === 0, JSON.stringify(writes));
      await checkWords(page, ck, "crew step");
      await page.getByTestId("choices-save").click();
      await page.locator("[data-testid=step-view][data-slot=github]").waitFor();
      const put = writes.find((w) => w.method === "PUT" && w.path === "/api/crew/settings");
      ck("Save and continue saves the three choices and moves on to GitHub", put && put.body.autonomy === "L3-review"
         && put.body.builders_max === n0 + 1 && typeof put.body.releaser === "boolean", JSON.stringify(put));
      await page.waitForFunction(() => document.querySelector("[data-testid=step-crew]")?.dataset.state === "set", null, { timeout: 15000 }).catch(() => {});
      ck("the rail ticks the crew step", (await page.getByTestId("step-crew").getAttribute("data-state")) === "set");

      await page.getByTestId("step-terminal").click();
      await page.locator("[data-testid=step-view][data-slot=terminal]").waitFor();
      const steps = page.getByTestId("terminal-panel").getByTestId("start-step");
      ck("three commands, each with its copy button, and what they need said once above them", (await steps.count()) === 3
         && (await page.getByTestId("terminal-panel").getByTestId("start-copy").count()) === 3 && (await page.getByTestId("prerequisites").count()) === 1);
      ck("the crew panel stands beside them", (await page.getByTestId("your-crew").isVisible()));
      const box = await page.getByTestId("terminal-panel").locator("code").first().evaluate((el) => {
        const b = el.parentElement.getBoundingClientRect(), row = el.closest("[data-testid=start-step]").getBoundingClientRect();
        return b.width < row.width / 2;
      });
      ck("a command's box is as wide as the command, not the row", box);
      const r = await page.request.post(`${BASE}/api/crew/machine`, { data: { machine: "example-laptop", version: "1.2.3" } });
      ck("the command line reports the machine", r.status() < 300, String(r.status()));
      await page.locator("[data-testid=start-step][data-key=login][data-done=true]").waitFor({ timeout: 20000 }).catch(() => {});
      ck("install and sign in tick by themselves, with no reload", (await page.locator("[data-testid=start-step][data-key=install]").getAttribute("data-done")) === "true"
         && (await page.locator("[data-testid=start-step][data-key=login]").getAttribute("data-done")) === "true");
      ck("a done step keeps its command, to run again on another machine",
        (await page.locator("[data-testid=start-step][data-key=login]").getByTestId("start-copy").count()) === 1);
      ck("the crew panel names the machine", (await text(page.getByTestId("crew-machine-state"))) === "example-laptop");
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, "terminal step");
      await shot(page, "terminal-1440.png");
      await ctx.close();
    }

    // where /setup opens from: the Setup card on top of the menu, while setup is not done
    console.log("== entry points ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/projects`, { waitUntil: "load" });
      await page.getByTestId("menu-setup").waitFor();
      const p = (await (await page.request.get(`${BASE}/api/factory/setup/progress`)).json());
      ck("the menu's Setup card links to /setup and says how far it is", (await page.getByTestId("menu-setup").getAttribute("href")) === "/setup"
         && (await text(page.getByTestId("menu-setup-count"))) === p.text, await text(page.getByTestId("menu-setup")));
      await shot(page, "entry-menu-1440.png");
      await page.getByTestId("menu-setup").click();
      await page.getByTestId("step-view").waitFor();
      ck("and it opens the setup screen", new URL(page.url()).pathname === "/setup", page.url());
      await ctx.close();
    }
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/settings`, { waitUntil: "load" });
      await page.getByTestId("nav-crew").waitFor();
      ck("Settings has no setup entry: setup is the menu's", (await page.getByTestId("nav-setup").count()) === 0);
      await ctx.close();
    }

    // a sample name without the flag shows the cell's own setup
    console.log("== samples need the flag ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/setup?step=tickets&sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("?sample= without the flag shows the cell's own setup", (await page.getByTestId("progress-text").textContent()).trim() === (await read(page)).progress.text);
      await ctx.close();
    }
    // the plain edition does not have the page
    console.log("== the route ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/setup?step=review`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("?step= opens that step", (await text(page.getByTestId("page-title"))) === "Review");
      await page.getByTestId("step-ci").click();
      ck("and moving to another step keeps it in the address, so a connect page can send the person back to it",
        new URL(page.url()).searchParams.get("step") === "ci", page.url());
      await ctx.close();
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
