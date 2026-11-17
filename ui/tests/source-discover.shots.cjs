/*
 * Before and after shots of Discover (TR-1394). Drives the old page and the new one through the same
 * steps (source-discover.fixture.cjs `addButton` finds either add button). TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/source-discover.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { DOCKER_DOWN, addButton, mockDiscover } = require("./source-discover.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9975);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  const shoot = async (name, { width = 1440, dark = false, step, ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockDiscover(page, mock);
    await page.goto(`${d.base}/sources/discover`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    if (step) await step(page);
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  const addThem = async (page) => { await addButton(page).click(); await page.waitForTimeout(400); };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`scan-${dark ? "dark" : "light"}-${width}.png`, { width, dark });
    }
    await shoot("added-light-1440.png", { step: addThem });
    await shoot("failed-light-1440.png", { failOnce: ["metrics"], step: addThem });
    await shoot("failed-light-390.png", { width: 390, failOnce: ["metrics"], step: addThem });
    await shoot("docker-light-1440.png", { scan: DOCKER_DOWN });
  } finally {
    await browser.close();
  }
  process.exit(0);
})();
