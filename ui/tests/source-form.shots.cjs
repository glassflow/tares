/*
 * Before-and-after shots of the connection form (TR-1394), written to run against any build: it only opens
 * /sources/new, picks a connector card and shoots the whole page. TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/source-form.shots.cjs
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { kindButton } = require("./source-new.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9965);
const OUT = process.env.SHOTS_DIR;
const CONNECTORS = ["loki", "http_poll", "postgres", "prometheus", "github", "reference"];

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  const browser = await chromium.launch();
  try {
    for (const width of [1440, 390]) {
      for (const c of CONNECTORS) {
        const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 } });
        const page = await ctx.newPage();
        await page.route("**/api/integrations/github", (r) => r.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
        await page.goto(`${d.base}/sources/new`, { waitUntil: "load" });
        await kindButton(page, c).click();
        await page.waitForTimeout(600);
        await page.screenshot({ path: path.join(OUT, `${c}-${width}.png`), fullPage: true });
        await ctx.close();
      }
    }
  } finally { await browser.close(); d.stop(); }
})().catch((e) => { console.error(e); process.exit(1); });
