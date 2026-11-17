/*
 * Before and after shots of All resources (TR-1395). TARES_UI_DIST picks the build; both open the same
 * addresses with the same rows (resources.fixture.cjs).
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/resources.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockResources } = require("./resources.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9987);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, to, { width = 1440, dark = false, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockResources(page, mock);
    await page.goto(`${d.base}${to}`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`connections-${dark ? "dark" : "light"}-${width}.png`, "/resources", { width, dark });
    }
    await shoot("keys-light-1440.png", "/resources/keys");
    await shoot("tools-dark-1440.png", "/resources/tools", { dark: true });
    await shoot("skills-empty-light-1440.png", "/resources/skills");
    await shoot("failed-light-1440.png", "/resources", { fail: true });
    await shoot("rules-sre-light-1440.png", "/resources/triggers", { edition: "sre" });
  } finally {
    await browser.close();
    process.exit(0);
  }
})();
