/*
 * Before-and-after shots of the Connections page on /sources (TR-1394), written to run against any build: it
 * serves the fixed list (sources.fixture.cjs), waits for the page to settle and shoots it whole. Light and dark,
 * 1440 and 390, plus the empty and failing states. TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/sources.shots.cjs
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mock } = require("./sources.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9967);
const OUT = process.env.SHOTS_DIR;
const STATES = [
  ...[1440, 390].flatMap((width) => [false, true].map((dark) => ({ name: `list-${dark ? "dark" : "light"}-${width}`, width, dark }))),
  { name: "empty-light-1440", width: 1440, list: [] },
  { name: "error-light-1440", width: 1440, list: 500 },
];

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  const browser = await chromium.launch();
  try {
    for (const s of STATES) {
      const ctx = await browser.newContext({ viewport: { width: s.width, height: s.width < 600 ? 844 : 900 }, ...(s.dark ? { colorScheme: "dark" } : {}) });
      const page = await ctx.newPage();
      await mock(page, { list: s.list });
      await page.goto(`${d.base}/sources`, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      await page.screenshot({ path: path.join(OUT, `${s.name}.png`), fullPage: true });
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await ctx.close();
      console.log(`  ${s.name}.png`);
    }
  } finally {
    await browser.close();
    d.stop();
  }
})();
