/*
 * Before-and-after shots of a connection's page on /sources/<name> (TR-1394), written to run against any build:
 * it serves the fixed connection (sources.fixture.cjs mockDetail), waits for the page to settle and shoots it
 * whole. The Fields tab light and dark at 1440 and 390, Recent events, the delete dialog, and a push connection
 * with nothing received. It finds tabs and buttons by their text, which the old page and the new one share.
 * TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/source-detail.shots.cjs
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { ALERTS, ORDERS, mockDetail } = require("./sources.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9969);
const OUT = process.env.SHOTS_DIR;
const tab = (label) => async (page) => page.getByRole("button", { name: label, exact: true })
  .or(page.getByRole("tab", { name: label, exact: true })).first().click();
const STATES = [
  ...[1440, 390].flatMap((width) => [false, true].map((dark) => ({ name: `fields-${dark ? "dark" : "light"}-${width}`, width, dark }))),
  { name: "events-light-1440", width: 1440, then: tab("Recent events") },
  { name: "events-light-390", width: 390, then: tab("Recent events") },
  { name: "delete-light-1440", width: 1440, detail: { dependents: [{ kind: "trigger", name: "orders-watch" }] }, then: tab("Delete") },
  { name: "push-light-1440", width: 1440, detail: { source: ALERTS, fields: { sampled: 0, fields: [], labels: [] }, events: [] } },
];

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  const browser = await chromium.launch();
  try {
    for (const s of STATES) {
      const ctx = await browser.newContext({ viewport: { width: s.width, height: s.width < 600 ? 844 : 900 }, ...(s.dark ? { colorScheme: "dark" } : {}) });
      const page = await ctx.newPage();
      const detail = s.detail ?? {};
      await mockDetail(page, detail);
      await page.goto(`${d.base}/sources/${(detail.source ?? ORDERS).name}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      if (s.then) { await s.then(page); await page.waitForTimeout(800); }
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
