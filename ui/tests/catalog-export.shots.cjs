/*
 * Before and after shots of Export connections (TR-1394). TARES_UI_DIST picks the build; both pages are
 * driven by role, so the same steps run on the old page and the new one.
 * Run:  SHOTS_DIR=… [TARES_UI_DIST=…] node tests/catalog-export.shots.cjs     (from ui/)
 */
const path = require("path");
const { chromium } = require(path.join(path.resolve(__dirname, ".."), "node_modules", "playwright"));
const { startDaemon } = require("./licensed.cjs");
const { mockExport } = require("./catalog-export.fixture.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 9979);
const OUT = process.env.SHOTS_DIR;
if (!OUT) { console.error("set SHOTS_DIR"); process.exit(2); }

(async () => {
  const d = await startDaemon({ port: PORT, edition: "factory", licence: "valid" });
  process.on("exit", () => d.stop());
  const browser = await chromium.launch();
  // steps: "export" presses the export button, "secrets" turns secrets on first.
  const shoot = async (name, { width = 1440, dark = false, steps = [], ...mock } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height: width < 600 ? 844 : 900 }, ...(dark ? { colorScheme: "dark" } : {}) });
    const page = await ctx.newPage();
    await mockExport(page, mock);
    await page.goto(`${d.base}/sources/export`, { waitUntil: "load" });
    await page.waitForLoadState("networkidle");
    if (steps.includes("secrets")) await page.getByText(/^Include (connector )?secrets/).click();
    if (steps.includes("export")) {
      await page.getByRole("button", { name: /^Export \d+/ }).click();
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
      await shoot(`list-${dark ? "dark" : "light"}-${width}.png`, { width, dark });
    }
    await shoot("file-light-1440.png", { steps: ["export"] });
    await shoot("file-light-390.png", { width: 390, steps: ["export"] });
    await shoot("secrets-light-1440.png", { steps: ["secrets", "export"] });
    await shoot("refused-light-1440.png", { exportStatus: 403, steps: ["export"] });
  } finally {
    await browser.close();
  }
  process.exit(0);
})();
