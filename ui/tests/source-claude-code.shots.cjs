/*
 * Before and after shots of Connect Claude Code (TR-1394). TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/source-claude-code.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockClaudeCode } = require("./source-claude-code.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9977);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, { width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockClaudeCode(page, mock);
    await page.goto(`${d.base}/sources/claude-code`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`key-${dark ? "dark" : "light"}-${width}.png`, { width, dark, auth: true });
    }
    await shoot("open-light-1440.png");
    await shoot("connected-light-1440.png", { connected: true });
    await shoot("connected-light-390.png", { width: 390, connected: true });
  } finally {
    await browser.close();
  }
  process.exit(0);
})();
