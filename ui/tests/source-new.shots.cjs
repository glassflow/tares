/*
 * Before-and-after shots of /sources/new (TR-1394), written to run against any build: the kind picker light
 * and dark at 1440 and 390, the YAML box after an import, a picked kind, and a push connection just created on
 * a cell that requires keys. It finds the kinds by key and the buttons by their text, which the old page and
 * the new one share (source-new.fixture.cjs `kindButton`). TARES_UI_DIST picks the build.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/source-new.shots.cjs
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { kindButton, mockNew } = require("./source-new.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9971);
const OUT = process.env.SHOTS_DIR;
const button = (page, re) => page.getByRole("button", { name: re }).first();
const create = (key, name) => async (page) => {
  await kindButton(page, key).click();
  await page.getByTestId("connection-form").waitFor();
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByRole("button", { name: "Create connection" }).click();
};
const STATES = [
  ...[1440, 390].flatMap((width) => [false, true].map((dark) => ({ name: `kinds-${dark ? "dark" : "light"}-${width}`, width, dark }))),
  { name: "yaml-light-1440", width: 1440, mock: { imported: { sources: ["metrics"], triggers: ["metrics-watch"] } }, then: async (page) => {
    await button(page, /yaml/i).click();
    await page.locator("textarea").first().fill("sources:\n  - name: metrics\n    connector: prometheus\n");
    await page.getByTestId("yaml-submit").or(button(page, /^Add to catalog$/)).first().click();
  } },
  { name: "form-light-1440", width: 1440, then: async (page) => kindButton(page, "loki").click() },
  { name: "form-light-390", width: 390, then: async (page) => kindButton(page, "loki").click() },
  { name: "created-light-1440", width: 1440, mock: { auth: true }, then: create("webhook", "deploys") },
  { name: "created-light-390", width: 390, mock: { auth: true }, then: create("webhook", "deploys") },
];

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  const browser = await chromium.launch();
  try {
    for (const s of STATES) {
      const ctx = await browser.newContext({ viewport: { width: s.width, height: s.width < 600 ? 844 : 900 }, ...(s.dark ? { colorScheme: "dark" } : {}) });
      const page = await ctx.newPage();
      await mockNew(page, s.mock ?? {});
      await page.goto(`${d.base}/sources/new`, { waitUntil: "networkidle" });
      await kindButton(page, "loki").waitFor({ timeout: 15000 });
      if (s.then) { await s.then(page); }
      await page.waitForTimeout(900);
      await page.screenshot({ path: path.join(OUT, `${s.name}.png`), fullPage: true });
      await page.unrouteAll({ behavior: "ignoreErrors" });
      await ctx.close();
      console.log(`  ${s.name}.png`);
    }
  } finally {
    await browser.close();
    d.stop();
  }
})().catch((e) => { console.error(e); process.exit(1); });
