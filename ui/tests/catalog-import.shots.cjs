/*
 * Before and after shots of Import connections (TR-1394). TARES_UI_DIST picks the build; both pages are
 * driven by role and label, so the same steps run on the old page and the new one. The imports reach
 * the real daemon, so the order matters: replace runs last.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/catalog-import.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockImport, GOOD, BAD } = require("./catalog-import.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9981);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  // file: what to paste. steps: "replace" picks replace, "import" presses the import button.
  const shoot = async (name, { width = 1440, dark = false, file, steps = [], ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockImport(page, mock);
    await page.goto(`${d.base}/sources/import`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    if (file) await page.locator("textarea").fill(file);
    if (steps.includes("replace")) await page.getByLabel(/^(replace \(clear catalog first\)|Replace everything)$/).click();
    if (steps.includes("import")) {
      await page.getByRole("button", { name: /^(Import|Import the file|Replace with the file)$/ }).click();
      await page.waitForLoadState("networkidle");
    }
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, name), fullPage: true });
    console.log(" ", name);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await ctx.close();
  };
  try {
    for (const width of [1440, 390]) for (const dark of [false, true]) {
      await shoot(`page-${dark ? "dark" : "light"}-${width}.png`, { width, dark });
    }
    await shoot("done-light-1440.png", { file: GOOD, steps: ["import"] });
    await shoot("done-light-390.png", { width: 390, file: GOOD, steps: ["import"] });
    await shoot("bad-light-1440.png", { file: BAD, steps: ["import"] });
    await shoot("refused-light-1440.png", { file: GOOD, importStatus: 403, steps: ["import"] });
    await shoot("replace-light-1440.png", { file: GOOD, steps: ["replace", "import"] });
  } finally {
    await browser.close();
  }
  process.exit(0);
})();
