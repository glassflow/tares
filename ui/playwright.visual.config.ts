import { defineConfig } from "@playwright/test";

// The screenshot check of the component gallery (TR-1269). It runs only inside the Playwright Linux
// image CI uses, mcr.microsoft.com/playwright:v1.60.0-noble: a Mac draws the same page with other font
// hinting, so its pixels never match. From a Mac: npm run test:visual (or test:visual:update), which
// run this config in that image through Docker. The image tag and @playwright/test move together.
//
// The gallery needs no daemon: vite preview serves ui/dist, and the spec answers the three calls the
// page makes (/health, /api/licence, /api/whoami) itself, so every run draws the same page.

const PORT = 4173;

export default defineConfig({
  testDir: "tests/visual",
  snapshotPathTemplate: "{testDir}/__screenshots__/{arg}{ext}",
  outputDir: "test-results/visual",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "test-results/visual-report" }]] : "list",
  expect: {
    // No tolerance for colour (threshold 0): baselines are drawn and compared in the same image, so an
    // unchanged page matches exactly, and a token nudged by a few units must fail (at 0.05 a 6-unit
    // shift of --ok passed). The ratio leaves room for nothing but antialiasing on a curve.
    toHaveScreenshot: { maxDiffPixelRatio: 0.0005, threshold: 0, animations: "disabled", caret: "hide", scale: "css" },
  },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    contextOptions: { reducedMotion: "reduce" },
    colorScheme: "light",
    deviceScaleFactor: 1,
    locale: "en-GB",
    timezoneId: "UTC",
  },
  webServer: {
    command: `npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/dev/components`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
