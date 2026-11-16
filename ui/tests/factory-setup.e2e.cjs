/*
 * Browser e2e for the factory's Set up your factory screen (TR-872): /setup. Seven steps in a rail on the
 * left (a list under the title at 390 px), and for the chosen step what is connected, what can be
 * connected, and the form of the integration being connected or changed. One generic renderer draws
 * every field by its kind (text, secret, url, select, bool, mapping). Every string comes from
 * GET /api/factory/setup, which the screen reads once; saves go through the routes that exist.
 *
 * Seeds a scratch factory cell (tests/seed-factory-setup.py, invented names), then runs the real daemon on
 * it, serving the built console. The states a plain cell cannot be in (a mapping, a select, a licence that
 * ended, a long or a large answer, Tares Cloud's apps) are the console's own samples (?sample=<name>, with
 * the sample flag), drawn by the same code from an answer shaped like the real one. Not built here, by
 * ticket: Test connection and where the screen opens from (TR-1095), the Guardrails step's content (TR-1096).
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
      ck("the rail lists the seven steps in order", (await page.getByTestId("rail").locator('button[data-testid^="step-"]').evaluateAll((b) => b.map((x) => x.dataset.testid.slice(5)))).join() === SLOTS.join());
      ck("on a wide screen the rail shows and the narrow list does not", (await page.getByTestId("rail").isVisible()) && !(await page.getByTestId("rail-narrow").isVisible()));
      const lines = await page.getByTestId("rail").getByTestId("step-line").allInnerTexts();
      ck("each step's line is the API's", lines.join("|") === api.steps.map((s) => s.line).join("|"), lines.join("|"));
      ck("the progress is the API's", (await text(page.getByTestId("progress-text"))) === api.progress.text && api.progress.text === "3 of 7 steps set", await text(page.getByTestId("progress-text")));
      ck("steps with a connection other than the built-in one are marked set", (await page.getByTestId("step-tickets").getAttribute("data-state")) === "set"
         && (await page.getByTestId("step-review").getAttribute("data-state")) === "not_set");
      ck("the first step is open and current", (await page.getByTestId("step-tickets").getAttribute("aria-current")) === "step"
         && (await text(page.getByTestId("page-title"))) === "Tickets" && (await text(page.getByTestId("page-sub"))) === stepOf(api, "tickets").why);
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
      ck("who picks a connection is shown", (await text(page.getByTestId("connected-github").getByTestId("row-use"))) === "Picked by project-p.");
      ck("a connection that is the default cannot be removed", (await page.getByTestId("connected-linear").getByTestId("action-remove").count()) === 0
         && (await page.getByTestId("connected-linear").getByTestId("action-edit").count()) === 1);
      ck("a step with nothing left to connect says so", (await text(page.getByTestId("available-empty"))) === stepOf(api, "tickets").available_empty);
      ck("the footer is the API's", (await text(page.getByTestId("step-footer"))).startsWith(stepOf(api, "tickets").footer.hint)
         && (await text(page.getByTestId("step-next"))) === stepOf(api, "tickets").footer.next);
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, "partly set up cell 1440");
      await shot(page, "filled-live-1440.png");
      ck("the page wrote nothing", writes.length === 0);

      // the other steps
      await page.getByTestId("step-review").click();
      ck("a step opens from the rail", (await text(page.getByTestId("page-title"))) === "Review" && (await page.getByTestId("step-review").getAttribute("aria-current")) === "step");
      ck("a step with nothing to connect shows its own sentence", (await text(page.getByTestId("available-empty"))) === stepOf(api, "review").available_empty);
      await page.getByTestId("step-guardrails").click();
      ck("Guardrails shows the API's note as text and offers no form", (await text(page.getByTestId("step-note"))) === stepOf(api, "guardrails").note
         && (await page.getByTestId("action-edit").count()) === 0 && (await page.getByTestId("action-connect").count()) === 0);
      await page.getByTestId("step-runtime").click();
      ck("the last step ends the setup", (await text(page.getByTestId("step-next"))) === "Finish setup" && (await page.getByTestId("step-next").getAttribute("href")) === "/");
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
      await ctx.close();
    }

    // 3. connecting a new integration: a secret and a url; a bool; it becomes the default
    console.log("== connecting ==");
    {
      const { ctx, page, writes } = await newPage(1440);
      await open(page, "monitoring");
      ck("the Connect another list names what can be connected", (await page.getByTestId("available").locator('[data-testid^="available-"]').evaluateAll((b) => b.map((x) => x.dataset.testid.slice(10)))).join() === "off,rius");
      await page.getByTestId("available-rius").getByTestId("action-connect").click();
      await page.getByTestId("setup-form").waitFor();
      ck("the form says how it connects", (await text(page.getByTestId("page-title"))) === "Connect Rius" && (await page.getByTestId("setup-form").getAttribute("data-mode")) === "connect"
         && (await text(page.getByTestId("form-save"))) === "Save and connect");
      ck("a secret that is not saved has no saved note", (await page.getByTestId("field-api_key").getByTestId("field-saved").count()) === 0);
      ck("a url field is a text input for an address", (await page.getByTestId("field-endpoint").getAttribute("data-kind")) === "url" && (await page.getByTestId("input-endpoint").getAttribute("inputmode")) === "url");
      ck("help is shown under the field", (await text(page.getByTestId("field-api_key"))).includes("Sent to Rius as a bearer key"));
      await page.getByTestId("input-api_key").fill("  rius_invented_key  ");
      await page.getByTestId("input-endpoint").fill("https://rius.example.org");
      await shot(page, "form-connect-1440.png");
      await page.getByTestId("form-save").click();
      await flashed(page);
      const put = writes.find((w) => w.method === "PUT");
      ck("a save sends the trimmed values and the form's default flag", put && put.path === "/api/factory/integrations/monitoring" && put.body.name === "rius"
         && put.body.settings.api_key === "rius_invented_key" && put.body.settings.endpoint === "https://rius.example.org" && put.body.default === true, JSON.stringify(put));
      ck("it says it is connected and shows it connected, as the default", (await flashText(page)) === "Rius is connected."
         && (await page.getByTestId("connected-rius").count()) === 1 && (await page.getByTestId("connected-rius").getByTestId("tag-default").count()) === 1);
      ck("monitoring holds one connection, so Rius takes the place of OTLP, which can be connected again", (await page.getByTestId("connected").locator('[data-testid^="connected-"]').count()) === 1
         && (await page.getByTestId("available-otlp").count()) === 1);
      ck("the rail follows", (await text(page.getByTestId("step-monitoring").getByTestId("step-line"))).startsWith("Rius"));
      await page.getByTestId("flash").getByRole("button", { name: "Dismiss" }).click();
      ck("the note can be dismissed", (await page.getByTestId("flash").count()) === 0);

      // a bool
      writes.length = 0;
      await page.getByTestId("step-ci").click();
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
      ck(`then the first step at ${width}`, (await text(page.getByTestId("page-title"))) === "Tickets");
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
      ck(`Try again reads it once more and shows the setup at ${width}`, (await text(page.getByTestId("page-title"))) === "Tickets");
      await ctx.close();
    }

    // 6. a narrow screen: the steps are a list under the title
    console.log("== 390 px ==");
    {
      const { ctx, page } = await newPage(390);
      await open(page, null);
      ck("the rail is a closed list with the current step", (await page.getByTestId("rail").isVisible()) === false && (await page.getByTestId("rail-narrow").isVisible())
         && (await text(page.getByTestId("rail-narrow").locator("summary"))).startsWith("Step 1 of 7: Tickets"));
      await page.getByTestId("rail-narrow").locator("summary").click();
      await page.getByTestId("nstep-monitoring").click();
      ck("on a phone the crumb is hidden and Exit setup stays in the header", !(await page.getByTestId("setup-title").isVisible()) && (await page.getByTestId("setup-exit").isVisible()));
      ck("a step picked in the list opens and closes the list", (await text(page.getByTestId("page-title"))) === "Monitoring" && (await page.getByTestId("rail-narrow").getAttribute("open")) === null);
      ck("no sideways scroll", (await overflow(page)) <= 0);
      await checkWords(page, ck, "partly set up cell 390");
      await shot(page, "filled-live-390.png");
      await page.getByTestId("connected-rius").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      ck("the form fits the screen", (await overflow(page)) <= 0);
      await shot(page, "form-390.png");
      await ctx.close();
    }

    // 7. the states a plain cell cannot be in: the samples, every field kind, long and many
    console.log("== samples ==");
    await sampleState("empty", "empty", "/setup?sample=empty", async (page, width, name) => {
      ck(`${name}: a fresh cell has only the runtime step set, and says so`, (await page.getByTestId("progress-text").textContent()).trim() === "1 of 7 steps set", await page.getByTestId("progress-text").textContent());
      const lines = await page.getByTestId("rail").getByTestId("step-line").allTextContents();
      ck(`${name}: every other step's line says it is not set yet`, lines.filter((l) => l.endsWith("(not set yet)")).length === 6 && !lines[6].endsWith("(not set yet)"), lines.join("|"));
    });
    await sampleState("filled", "filled", "/setup?sample=filled");
    await sampleState("long", "long", "/setup?sample=long", async (page, width, name) => {
      ck(`${name}: the long project names are shown in full and wrap`, (await text(page.getByTestId("connected-linear").getByTestId("row-use"))).includes("orders-api-with-a-very-long-project-name-for-the-checkout-flow")
         && (await overflow(page)) <= 0);
      await page.getByTestId("connected-github").getByTestId("action-edit").click();
      await page.getByTestId("setup-form").waitFor();
      await page.getByTestId("input-repo").scrollIntoViewIfNeeded();
      ck(`${name}: the form's long text stays inside its box`, (await page.getByTestId("setup-form").evaluate((el) => el.scrollWidth <= el.clientWidth + 1)));
    });
    await sampleState("many", "many", "/setup?sample=many", async (page, width, name) => {
      ck(`${name}: twelve connections are listed, one picked by twelve projects`, (await page.getByTestId("connected").locator('[data-testid^="connected-"]').count()) === 12, String(await page.getByTestId("connected").locator('[data-testid^="connected-"]').count()));
    });
    await widths("the licence ended", async ({ page }, width, name) => {
      await page.goto(`${BASE}/setup?sample=expired`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck(`${name}: the step's banner is the API's text`, (await text(page.getByTestId("step-banner"))).includes("Your licence ended on 3 November."));
      ck(`${name}: the connection says its licence ended`, (await raw(page.getByTestId("connected-linear").getByTestId("tag-licence_ended"))) === "Licence ended");
      ck(`${name}: no sideways scroll`, (await overflow(page)) <= 0);
      await checkWords(page, ck, name);
      await shot(page, `expired-${width}.png`);
    });
    await widths("a fresh cell", async ({ page }, width, name) => {
      await page.goto(`${BASE}/setup?sample=empty`, { waitUntil: "load" });
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
      await page.goto(`${BASE}/setup?sample=filled`, { waitUntil: "load" });
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
      let opened = 0;
      await page.addInitScript(() => { window.open = (...a) => { (window.__opened = window.__opened || []).push(a); return null; }; });
      await page.goto(`${BASE}/setup?sample=cloud`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("app-block").waitFor();
      ck("a connection with an app starts with the app", (await text(page.getByTestId("app-block"))).includes("Approve the GlassFlow Linear app.") && (await page.getByTestId("field-api_key").count()) === 0
         && (await page.getByTestId("form-save").count()) === 0);
      ck("the start has one Cancel, and the way to the key is a line of its own below the buttons", (await page.getByTestId("form-cancel").count()) === 1 && (await page.getByTestId("app-use-key").count()) === 1
         && (await page.getByTestId("app-open").evaluate((a) => a.getBoundingClientRect().bottom)) <= (await page.getByTestId("app-use-key").evaluate((a) => a.getBoundingClientRect().top)));
      await shot(page, "app-start-1440.png");
      await page.getByTestId("app-open").click();
      await page.getByTestId("app-waiting").waitFor();
      opened = await page.evaluate(() => (window.__opened || []).length);
      ck("Open the app opens its address in a new window and waits", opened === 1 && (await page.evaluate(() => window.__opened[0][0])) === "https://connect.example.test/linear"
         && (await text(page.getByTestId("app-waiting"))).startsWith("Waiting for Linear"));
      ck("while it waits there is one waiting box and one Cancel, and no steps, note or key line",
        (await page.getByTestId("app-waiting").count()) === 1 && (await page.getByTestId("form-cancel").count()) === 1 && (await page.getByTestId("app-block").count()) === 0
        && (await page.getByTestId("app-use-key").count()) === 0 && (await page.getByTestId("app-open").count()) === 0 && (await page.getByTestId("app-open-again").count()) === 1
        && !(await text(page.getByTestId("setup-form"))).includes("What happens next") && !(await text(page.getByTestId("setup-form"))).includes("Cannot use the app"), await text(page.getByTestId("setup-form")));
      await shot(page, "app-waiting-1440.png");
      await page.getByTestId("app-open-again").click();
      ck("Open again opens the address once more and keeps waiting", (await page.evaluate(() => window.__opened.length)) === 2 && (await page.getByTestId("app-waiting").count()) === 1);
      await page.getByTestId("form-cancel").click();
      ck("Cancel stops waiting and goes back to the step", (await page.getByTestId("setup-form").count()) === 0 && (await page.getByTestId("step-view").count()) === 1);
      await page.getByTestId("available-linear").getByTestId("action-connect").click();
      await page.getByTestId("app-block").waitFor();
      await page.getByTestId("app-use-key").click();
      ck("the key is one click away", (await page.getByTestId("field-api_key").count()) === 1 && (await page.getByTestId("form-save").count()) === 1 && (await page.getByTestId("app-switch").count()) === 1);
      await page.getByTestId("app-use-app").click();
      ck("and so is the app", (await page.getByTestId("app-block").count()) === 1);
      await checkWords(page, ck, "app start");
      await page.getByTestId("form-cancel").click();
      await page.goto(`${BASE}/setup?sample=ghdone`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await page.getByTestId("available-github").getByTestId("action-connect").click();
      await page.getByTestId("app-done").waitFor();
      ck("a finished app asks only for what it cannot know", (await text(page.getByTestId("app-done"))).startsWith("GitHub is connected.") && (await page.getByTestId("field-repo").count()) === 1
         && (await page.getByTestId("field-credential").count()) === 0 && (await page.getByTestId("input-repo").count()) === 1);
      ck("the green line is under the fields and just above Save and connect", await page.evaluate(() => {
        const d = document.querySelector('[data-testid="app-done"]'), f = document.querySelector('[data-testid="field-repo"]'), b = document.querySelector('[data-testid="form-done"], [data-testid="form-save"]');
        return !!(d && f && b && (f.compareDocumentPosition(d) & Node.DOCUMENT_POSITION_FOLLOWING) && (d.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
          && getComputedStyle(d).color !== getComputedStyle(document.body).color);
      }));
      await shot(page, "app-done-1440.png");
      await checkWords(page, ck, "app done");
      await page.goto(`${BASE}/setup?sample=approw`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("a row connected with the app says so, and is disconnected, not removed", (await page.getByTestId("connected-linear").getByTestId("tag-app").count()) === 1
         && (await text(page.getByTestId("connected-linear").getByTestId("action-remove"))) === "Disconnect" && (await page.getByTestId("connected-linear").getByTestId("action-edit").count()) === 0);
      await ctx.close();
    }

    // the waiting page reads the cell again by itself, then shows the connection (F9 and F2)
    console.log("== the app poll ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.addInitScript(() => { window.open = (...a) => { (window.__opened = window.__opened || []).push(a); return null; }; });
      let connected = false, reads = 0, pick = null;
      const patch = (d) => {
        const st = d.steps.find((x) => x.slot === "tickets");
        if (!pick) pick = JSON.parse(JSON.stringify(st.available.find((a) => a.form && a.allowed)));
        const base = st.available.filter((a) => a.id !== pick.id);
        if (!connected) {
          st.available = [{ ...pick, app: { url: "https://connect.example.test/poll", service: pick.title, app_name: `GlassFlow ${pick.title}`, button: `Connect ${pick.title}`, repo_step: false, steps: ["Approve the app."], note: "Nothing else to set up.", use_key_label: "Use an API key instead", use_app_label: "Use the app instead", stage: "start", done: null } }, ...base];
        } else {
          st.available = base;
          st.connected = [...st.connected, { id: pick.id, title: pick.title, description: pick.description, tags: [{ key: "app", text: "App" }], default: false, built_in: false, via: "app", who: "", picked_by: [], follows: 0, use: "",
            actions: [{ key: "remove", label: "Disconnect" }], blocked: null, confirm: { title: `Disconnect ${pick.title}?`, text: "Nothing else changes.", yes: "Yes, disconnect", no: "Keep it", moves_projects: false }, form: null, last_test: null }];
        }
      };
      await page.route("**/api/factory/setup", async (r) => {
        reads += 1;
        const d = await (await r.fetch()).json();
        patch(d);
        await r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(d) });
      });
      await page.route("**/api/factory/integrations/tickets/connections/*", async (r) => { connected = false; await r.fulfill({ status: 200, contentType: "application/json", body: "{}" }); });
      await page.goto(`${BASE}/setup`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      await settle(page);
      await page.getByTestId(`available-${pick.id}`).getByTestId("action-connect").click();
      await page.getByTestId("app-open").click();
      await page.getByTestId("app-waiting").waitFor();
      const before = reads;
      await page.waitForTimeout(3600);
      ck("the waiting page reads the cell again by itself", reads > before, `${before} then ${reads}`);
      ck("and it is still waiting while the app is not approved", (await page.getByTestId("app-waiting").count()) === 1);
      connected = true;
      await page.getByTestId("flash").waitFor({ timeout: 8000 }).catch(() => {});
      const said = (await page.getByTestId("flash").count()) ? await flashText(page) : "(no note)";
      ck("once the app is approved the form closes and the note says it is connected", (await page.getByTestId("setup-form").count()) === 0 && said === `${pick.title} is connected.`
         && (await page.getByTestId(`connected-${pick.id}`).count()) === 1, said);
      await page.getByTestId(`connected-${pick.id}`).getByTestId("action-remove").click();
      await page.getByTestId("confirm-yes").click();
      await page.getByTestId("flash").filter({ hasText: "disconnected" }).waitFor();
      await settle(page);
      ck("after Remove the Connect form does not open again by itself", (await page.getByTestId("setup-form").count()) === 0 && (await page.getByTestId(`available-${pick.id}`).count()) === 1
         && (await page.getByTestId(`connected-${pick.id}`).count()) === 0);
      await ctx.close();
    }

    // a sample name without the flag shows the cell's own setup
    console.log("== samples need the flag ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/setup?sample=empty`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("?sample= without the flag shows the cell's own setup", (await page.getByTestId("progress-text").textContent()).trim() === (await read(page)).progress.text);
      await ctx.close();
    }
    // the plain edition does not have the page
    console.log("== the route ==");
    {
      const { ctx, page } = await newPage(1440);
      await page.goto(`${BASE}/setup?step=memory`, { waitUntil: "load" });
      await page.getByTestId("step-view").waitFor();
      ck("?step= opens that step", (await text(page.getByTestId("page-title"))) === "Memory");
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
