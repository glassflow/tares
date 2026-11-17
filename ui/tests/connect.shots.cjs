/*
 * Before and after shots of Connect (TR-1395). TARES_UI_DIST picks the build; both open the same
 * addresses (?tab= picks the tab on the old page and the new) against a signed-in daemon, with the same
 * endpoint, tools and rules (connect.fixture.cjs).
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/connect.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockConnect } = require("./connect.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9994);
const TOKEN = "tok-connect-shots";
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid", auth: TOKEN });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, tab, { width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    await ctx.addInitScript((t) => { try { localStorage.setItem("tares_token", t); } catch { /* none */ } }, TOKEN);
    const page = await ctx.newPage();
    await mockConnect(page, mock);
    await page.goto(`${d.base}/connect${tab ? `?tab=${tab}` : ""}`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(3000);   // past the MCP probe's own 2.5 s timeout
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`mcp-${dark ? "dark" : "light"}-${width}.png`, null, { width, dark });
    }
    await shoot("mcp-absent-light-1440.png", null, { mcp: "none" });
    await shoot("webhook-light-1440.png", "push");
    await shoot("webhook-dark-390.png", "push", { width: 390, dark: true });
    await shoot("rest-light-1440.png", "rest");
    await shoot("tools-light-1440.png", "tools");
    await shoot("tools-dark-390.png", "tools", { width: 390, dark: true });
    await shoot("tools-failed-light-1440.png", "tools", { tools: "fail" });
  } finally {
    await browser.close();
    process.exit(0);
  }
})();
