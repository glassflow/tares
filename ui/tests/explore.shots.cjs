/*
 * Before and after shots of Explore (TR-1395). TARES_UI_DIST picks the build; both open the same
 * address with the same labels and timeline (explore.fixture.cjs).
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/explore.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockExplore } = require("./explore.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9990);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, { width = 1440, dark = false, then, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockExplore(page, mock);
    await page.goto(`${d.base}/explore`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(500);
    // The old page and the new one name the agent tab the same; a missing tab leaves the shot as is.
    if (then) { await then(page).catch(() => {}); await page.evaluate(() => window.scrollTo(0, 0)); await page.waitForTimeout(500); }
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  const agentTab = (page) => page.getByText("What the agent gets", { exact: true }).first().click({ timeout: 2000 });
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`timeline-${dark ? "dark" : "light"}-${width}.png`, { width, dark });
    }
    await shoot("agent-light-1440.png", { then: agentTab });
    await shoot("agent-dark-390.png", { width: 390, dark: true, then: agentTab });
    await shoot("empty-light-1440.png", { empty: true });
    await shoot("failed-light-1440.png", { fail: { until: 99 } });
    await shoot("read-failed-dark-1440.png", { dark: true, readFail: true });
  } finally {
    await browser.close();
    process.exit(0);
  }
})();
