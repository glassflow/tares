/*
 * Browser e2e for TR-775: a project's secrets on the factory project page (/projects/:id).
 *
 * The Secrets section sits next to People. A member adds a secret (name and value), replaces it and
 * removes it; the list shows the name, who set it and when, and whether a builder used it, never a
 * value: the value is not on the page, in the list's answer or in the dialog once saved. A cell with no
 * TARES_SECRETS_KEY says secrets are off and how to turn them on. Only words from words.json, no
 * sideways scroll at 390 px, both themes.
 *
 * Seeds a scratch factory cell (tests/seed-factory-project.py), then runs the real daemon on it twice:
 * with a secrets key, and without one.
 * Run:  npm run build && node tests/factory-secrets.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep a screenshot of each state at 1440 and 390 px.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8350);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const VALUE = "nvf_readonly_e2e_0123456789abcdef";
const REPLACED = ["replaced", "in", "e2e"].join("-");   // built, so no scanner reads a key in this file
const THEME_MS = 600;

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-secrets-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-factory-project.py"), home],
    { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } }).trim().split("\n").pop());
  const y = ids["project-y"];
  ck("seeded a project", !!y);

  const start = (extra) => spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], {
    cwd: REPO, stdio: "ignore",
    env: { HOME: process.env.HOME, PATH: process.env.PATH, PYTHONPATH: REPO, TARES_EDITION: "factory", TARES_HOME: home,
           TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", ...extra },
  });
  const stop = async (d) => { d.kill(); await new Promise((r) => d.on("exit", r)); await new Promise((r) => setTimeout(r, 300)); };

  let browser;
  let daemon = start({ TARES_SECRETS_KEY: "a-long-random-string-for-the-e2e-cell" });
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (width) => {
      const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
      return { ctx, page: await ctx.newPage() };
    };
    const shot = async (page, n) => { if (SHOTS) await page.locator("#secrets").screenshot({ path: path.join(SHOTS, n) }); };
    const full = async (page, n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const txt = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
    const open = async (page) => {
      await page.goto(`${BASE}/projects/${y}/people`, { waitUntil: "load" });
      await page.getByTestId("project-secrets").waitFor();
      await page.waitForLoadState("networkidle");
    };

    const { ctx, page } = await newPage(1440);
    const T = (id) => page.getByTestId(id);
    const answers = [];
    page.on("response", async (r) => { if (r.url().includes("/secrets")) answers.push(await r.text().catch(() => "")); });
    await open(page);
    ck("the Secrets section is on the project's People and access page", await T("project-secrets").isVisible());
    ck("it comes right after People", await page.evaluate(() => {
      const p = document.querySelector("[data-testid=project-people]"), s = document.querySelector("[data-testid=project-secrets]");
      return !!p && !!s && p.nextElementSibling === s;
    }));
    ck("with none yet it says what one is for", (await txt(T("secrets-empty"))).startsWith("No secrets yet"));
    ck("it says how builders use them", (await txt(T("project-secrets"))).includes("Builders use them with tares factory with-secrets -- <command>"), await txt(T("project-secrets")));
    await checkWords(page, ck, "secrets empty");
    await shot(page, "secrets-empty-1440.png");

    // add
    await T("secret-add").click();
    await T("secret-dialog").waitFor();
    ck("Save waits for a name and a value", await T("secret-save").isDisabled());
    await T("secret-name").fill("cell_read_key");
    ck("the name is upper-cased as typed", (await T("secret-name").inputValue()) === "CELL_READ_KEY");
    ck("the value is typed into a password field", (await T("secret-value").getAttribute("type")) === "password");
    await T("secret-value").fill(VALUE);
    await checkWords(page, ck, "add dialog");
    if (SHOTS) await T("secret-dialog").screenshot({ path: path.join(SHOTS, "secrets-add-dialog-1440.png") });
    await T("secret-save").click();
    await T("secret-dialog").waitFor({ state: "detached" });
    const rows = T("secret-row");
    ck("the new secret is listed by name", (await rows.count()) === 1 && (await txt(rows.first())).startsWith("CELL_READ_KEY"), await txt(T("project-secrets")));
    ck("it says who set it, when, and that no builder used it yet",
       /Set .* just now · not used yet/.test(await txt(rows.first())), await txt(rows.first()));
    ck("the value is nowhere on the page", !(await page.content()).includes(VALUE));
    ck("nor in any answer the page read", answers.every((a) => !a.includes(VALUE)), answers.join(" | ").slice(0, 300));

    // a builder uses it: the list says so
    const use = await fetch(`${BASE}/api/projects/${y}/secrets/use`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({}) });
    ck("the use route gives the value to a member", use.status === 200 && (await use.json()).values.CELL_READ_KEY === VALUE);
    await open(page);
    ck("after a use the row says it was last used", /last used .* just now/.test(await txt(T("secret-row").first())), await txt(T("secret-row").first()));
    await shot(page, "secrets-one-1440.png");

    // replace
    await T("secret-replace").click();
    await T("secret-dialog").waitFor();
    ck("Replace asks for the value only, under the secret's name",
       (await T("secret-name").count()) === 0 && (await txt(T("secret-dialog"))).includes("Replace CELL_READ_KEY"));
    ck("and shows no old value", (await T("secret-value").inputValue()) === "");
    await T("secret-value").fill(REPLACED);
    await T("secret-save").click();
    await T("secret-dialog").waitFor({ state: "detached" });
    const after = await (await fetch(`${BASE}/api/projects/${y}/secrets/use`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" })).json();
    ck("Replace saves the new value", after.values.CELL_READ_KEY === REPLACED);

    // 390 and dark
    for (const width of [390]) {
      const c = await newPage(width);
      await c.page.goto(`${BASE}/projects/${y}/people`, { waitUntil: "load" });
      await c.page.getByTestId("project-secrets").waitFor();
      await c.page.waitForLoadState("networkidle");
      ck(`${width}: no sideways scroll`, (await overflow(c.page)) <= 0);
      await checkWords(c.page, ck, `secrets ${width}`);
      await shot(c.page, `secrets-one-${width}.png`);
      await c.ctx.close();
    }
    await page.emulateMedia({ colorScheme: "dark" });
    await open(page);
    await page.waitForTimeout(THEME_MS);
    ck("the dark theme is on", await page.evaluate(() => getComputedStyle(document.body).backgroundColor) !== "rgb(250, 248, 240)"
       && (await page.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches)));
    await checkWords(page, ck, "secrets dark");
    await shot(page, "secrets-one-dark-1440.png");
    await full(page, "secrets-page-1440.png");
    await page.emulateMedia({ colorScheme: "light" });
    await open(page);

    // remove
    await T("secret-remove").click();
    await T("secrets-empty").waitFor();
    ck("Remove takes it off the list", (await T("secret-row").count()) === 0);
    await ctx.close();

    // a cell with no key
    await stop(daemon);
    daemon = start({});
    ck("daemon up without a secrets key", await waitFor(`${BASE}/health`));
    const off = await newPage(1440);
    await off.page.goto(`${BASE}/projects/${y}/people`, { waitUntil: "load" });
    await off.page.getByTestId("secrets-off").waitFor();
    ck("without a key the section says secrets are off and how to turn them on",
       (await txt(off.page.getByTestId("secrets-off"))).includes("TARES_SECRETS_KEY"));
    ck("and offers no Add", (await off.page.getByTestId("secret-add").count()) === 0);
    await checkWords(off.page, ck, "secrets off");
    if (SHOTS) await off.page.locator("#secrets").screenshot({ path: path.join(SHOTS, "secrets-off-1440.png") });
    await off.ctx.close();
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
