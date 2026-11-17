/*
 * Browser e2e for Connect Claude Code on /sources/claude-code (TR-1394): the steps and their words, both
 * copy lines (with a clipboard, and without one), what to do about a key when the cell needs one, when it
 * is open and when Tares can't say, the first session arriving while the page is open, light and dark,
 * 1440 and 390. The daemon is real; health and the connections list are mocked at the network
 * (source-claude-code.fixture.cjs).
 * Run:  npm run build && node tests/source-claude-code.e2e.cjs     (from ui/, tares installed in ../.venv)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { offendersIn } = require("./words.cjs");
const { mockClaudeCode } = require("./source-claude-code.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9976);
let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => { cond ? pass++ : fail++; console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`)); };

const PAGE_TEXT = () => {
  const el = document.querySelector("[data-testid=claude-code-page]");
  const style = document.createElement("style");
  style.textContent = "[data-words-exempt],[data-user-text]{display:none !important}";
  document.head.appendChild(style);
  try { return el ? el.innerText : ""; } finally { style.remove(); }
};
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const flat = async (loc) => (await loc.innerText()).replace(/\s+/g, " ").trim();
const INSTALL = "/plugin marketplace add glassflow/tares\n/plugin install tares@tares";

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

  // clipboard: "ok" records what was copied, "none" has no clipboard at all (a page over plain http).
  const open = async ({ width = 1440, dark = false, clipboard = "ok", ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript((mode) => {
      window.__copied = [];
      const value = mode === "none" ? undefined : { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } };
      Object.defineProperty(navigator, "clipboard", { value, configurable: true });
    }, clipboard);
    await mockClaudeCode(page, mock);
    await page.goto(`${d.base}/sources/claude-code`, { waitUntil: "load" });
    await page.getByTestId("claude-code-page").waitFor({ timeout: 15000 });
    const close = ctx.close.bind(ctx);
    ctx.close = async () => { await page.unrouteAll({ behavior: "ignoreErrors" }); await close(); };
    return { ctx, page, errors };
  };
  const key = (page) => page.getByTestId("claude-code-key");
  const keySays = async (page, re) => {
    await page.waitForFunction((src) => new RegExp(src).test(document.querySelector("[data-testid=claude-code-key]")?.textContent ?? ""), re.source, { timeout: 5000 }).catch(() => {});
    return flat(key(page));
  };
  const words = async (page, label) => {
    const found = offendersIn(await page.evaluate(PAGE_TEXT));
    ck(`${label}: no internal names`, !found.filter((w) => w !== "em dash").length, found.join(", "));
    ck(`${label}: no em dashes`, !found.includes("em dash"), found.join(", "));
  };

  try {
    // ── an open cell, nothing connected yet ────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open();
      ck("steps: titled in page words", (await page.getByRole("heading", { level: 1 }).innerText()) === "Connect Claude Code");
      ck("steps: three, in order", (await page.locator("[data-testid=claude-code-page] ol > li").count()) === 3);
      ck("steps: the install commands, as they are typed",
         (await page.getByTestId("claude-code-install-commands").textContent()) === INSTALL, await page.getByTestId("claude-code-install-commands").textContent());
      ck("steps: the commands show as two lines", (await page.getByTestId("claude-code-install-commands").innerText()) === INSTALL,
         JSON.stringify(await page.getByTestId("claude-code-install-commands").innerText()));
      ck("steps: the commands are system text", (await page.getByTestId("claude-code-install-commands").getAttribute("data-words-exempt")) !== null);
      ck("steps: this Tares's own address", (await page.getByTestId("claude-code-address").textContent()) === d.base, await page.getByTestId("claude-code-address").textContent());
      const said = await keySays(page, /open/);
      ck("open: says to leave the key empty", said === "It also asks for a key. This Tares is open, so leave it empty.", said);
      ck("open: no Settings link", (await key(page).getByRole("link").count()) === 0);
      ck("waiting: says it waits for the first session", (await page.getByTestId("claude-code-state").textContent()) === "Waiting for the first session");
      ck("waiting: no connected banner", (await page.getByTestId("claude-code-connected").count()) === 0);
      await words(page, "open");

      // copying
      await page.getByRole("button", { name: "Copy commands" }).click();
      await page.getByRole("button", { name: "Copied" }).first().waitFor({ timeout: 3000 }).catch(() => {});
      ck("copy: the commands are copied whole", JSON.stringify(await page.evaluate(() => window.__copied)) === JSON.stringify([INSTALL]),
         JSON.stringify(await page.evaluate(() => window.__copied)));
      ck("copy: the button says so", (await page.getByTestId("claude-code-install").getByRole("button").innerText()) === "Copied");
      await page.getByRole("button", { name: "Copy address" }).click();
      await page.waitForTimeout(200);
      ck("copy: the address is copied", (await page.evaluate(() => window.__copied[1])) === d.base);
      await page.waitForTimeout(1700);
      ck("copy: the button goes back", (await page.getByTestId("claude-code-install").getByRole("button").innerText()) === "Copy commands");
      ck("copy: nothing threw", errors.length === 0, errors.join("; "));
      await ctx.close();
    }

    // ── no clipboard ───────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page, errors } = await open({ clipboard: "none" });
      const btn = page.getByTestId("claude-code-point").getByRole("button");
      await btn.click();
      const alert = page.getByTestId("claude-code-point").getByRole("alert");
      await alert.waitFor({ timeout: 3000 }).catch(() => {});
      ck("no clipboard: says it could not copy", (await alert.count()) === 1 && (await alert.innerText()) === "Could not copy. Select the address and copy it by hand.");
      ck("no clipboard: the button does not claim it copied", (await btn.innerText()) === "Copy address", await btn.innerText());
      ck("no clipboard: nothing threw", errors.length === 0, errors.join("; "));
      await words(page, "no clipboard");
      await ctx.close();
    }

    // ── a cell that needs a key ────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ auth: true });
      const said = await keySays(page, /needs one/);
      ck("key: says this Tares needs one", said.startsWith("It also asks for a key. This Tares needs one: make one in Settings that can Read and Send data"), said);
      const settings = key(page).getByRole("link", { name: "Settings" });
      ck("key: Settings opens the keys", (await settings.count()) === 1 && (await settings.getAttribute("href")) === "/settings?tab=keys");
      await words(page, "key");
      await ctx.close();
    }

    // ── Tares can't say ────────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ auth: "fails" });
      await page.waitForTimeout(1500);
      const said = await flat(key(page));
      ck("unknown: does not say the cell is open", !/open/.test(said), said);
      ck("unknown: says what to do if asked", said.startsWith("If it also asks for a key, make one in Settings"), said);
      await ctx.close();
    }

    // ── connected, and the first session arriving ──────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ connected: true, name: "my claude" });
      const banner = page.getByTestId("claude-code-connected");
      await banner.waitFor({ timeout: 5000 });
      ck("connected: says so", (await flat(banner)).startsWith("Claude Code is connected. Its sessions arrive in my claude."), await flat(banner));
      const named = banner.locator("[data-user-text]");
      ck("connected: the name is user text", (await named.count()) === 1 && (await named.innerText()) === "my claude");
      ck("connected: opens that connection, by its own name",
         (await banner.getByRole("link", { name: "Open the connection" }).getAttribute("href")) === "/sources/my%20claude");
      ck("connected: the last step says sessions arrive", (await page.getByTestId("claude-code-state").textContent()) === "Receiving sessions");
      await words(page, "connected");
      await ctx.close();
    }
    {
      let arrived = false;
      const { ctx, page } = await open({ connected: () => arrived });
      ck("arriving: waits at first", (await page.getByTestId("claude-code-state").textContent()) === "Waiting for the first session");
      arrived = true;
      await page.getByTestId("claude-code-connected").waitFor({ timeout: 15000 }).catch(() => {});
      ck("arriving: the page notices without a reload", (await page.getByTestId("claude-code-connected").count()) === 1);
      ck("arriving: the step turns over", (await page.getByTestId("claude-code-state").textContent()) === "Receiving sessions");
      await ctx.close();
    }

    // ── dark, and a phone ──────────────────────────────────────────────────────────────────────────
    {
      const { ctx, page } = await open({ dark: true, auth: true });
      await keySays(page, /needs one/);
      await words(page, "dark");
      await ctx.close();
    }
    for (const dark of [false, true]) {
      const label = `390 ${dark ? "dark" : "light"}`;
      const { ctx, page } = await open({ width: 390, dark, auth: true, connected: true });
      await page.getByTestId("claude-code-connected").waitFor({ timeout: 5000 });
      ck(`${label}: no sideways scroll`, (await sideways(page)) <= 0, String(await sideways(page)));
      await ctx.close();
    }
  } catch (err) {
    ck("suite ran to the end", false, err.stack || String(err));
  } finally {
    await browser.close();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
