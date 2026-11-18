/*
 * Before and after shots of Reads (TR-1395). TARES_UI_DIST picks the build; both open /reads against the
 * same daemon and the same read log (reads.fixture.cjs). The old page has no client dropdown, so every
 * shot shows the default filter (agents over MCP); "all" shots search nothing and pick every client on
 * the new page only.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/reads.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockReads } = require("./reads.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9984);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, { width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockReads(page, mock);
    await page.goto(`${d.base}/reads`, { waitUntil: "load" });
    await page.waitForTimeout(mock.rows === "fail" ? 1500 : 1000);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`reads-${dark ? "dark" : "light"}-${width}.png`, { width, dark });
    }
    await shoot("empty-light-1440.png", { rows: [] });
    await shoot("failed-light-1440.png", { rows: "fail" });
  } finally {
    await browser.close();
    process.exit(0);
  }
})();
