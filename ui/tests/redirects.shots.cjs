/*
 * Before and after shots of the old addresses and the not-found page (TR-1394). TARES_UI_DIST picks
 * the build; both run the same addresses with the same lookups (redirects.fixture.cjs).
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/redirects.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockLookups } = require("./redirects.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9983);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, to, { width = 1440, dark = false, settle = true, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockLookups(page, mock);
    await page.goto(`${d.base}${to}`, { waitUntil: "load" });
    if (settle) { await page.waitForLoadState("networkidle"); await page.waitForTimeout(400); }
    else await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      const tag = `${dark ? "dark" : "light"}-${width}`;
      await shoot(`gone-${tag}.png`, "/triggers/gone-rule", { width, dark });
      await shoot(`notfound-${tag}.png`, "/no/such/page", { width, dark });
    }
    await shoot("looking-light-1440.png", "/triggers/shop-errors", { slow: 5000, settle: false });
    await shoot("gone-sre-light-1440.png", "/dispatches/d-404", { edition: "sre" });
  } finally {
    await browser.close();
    process.exit(0);
  }
})();
