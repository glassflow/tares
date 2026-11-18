import { expect, test, type Page } from "@playwright/test";

// TR-1269: every section of /dev/components, in light, dark and at 390px, against a committed
// picture. A kit token or a component's padding that moves fails here, with the diff image in the
// report. Baselines live in tests/visual/__screenshots__/<variant>-<section>.png and are made in the
// Linux image only (playwright.visual.config.ts). To change them on purpose: npm run test:visual:update,
// look at every changed picture, commit them with the change that moved them.

test.skip(process.platform !== "linux", "Pixels match only in the Playwright Linux image: run npm run test:visual");

const VARIANTS = [
  { name: "light", theme: "light", width: 1440 },
  { name: "dark", theme: "dark", width: 1440 },
  { name: "narrow", theme: "light", width: 390 },
] as const;

/** The gallery as every run sees it: the theme set before the first paint, the page's three calls
 *  answered here, and every font face loaded before a picture is taken. */
async function openGallery(page: Page, theme: string) {
  await page.addInitScript((t) => localStorage.setItem("tares-theme", t), theme);
  await page.route("**/health", (r) => r.fulfill({ json: { ok: true } }));
  await page.route("**/api/licence", (r) => r.fulfill({ status: 404, json: {} }));
  await page.route("**/api/whoami", (r) => r.fulfill({ status: 404, json: {} }));
  await page.goto("/dev/components", { waitUntil: "networkidle" });
  await page.getByTestId("dev-components").waitFor();
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe(theme);
  await page.evaluate(async () => {
    await Promise.all([...document.fonts].map((f) => f.load().catch(() => undefined)));
    await document.fonts.ready;
  });
}

for (const v of VARIANTS) {
  test(`gallery, ${v.name}`, async ({ page }) => {
    await page.setViewportSize({ width: v.width, height: 900 });
    await openGallery(page, v.theme);
    const names = await page.locator("[data-gallery-section]").evaluateAll((els) => els.map((e) => e.getAttribute("data-gallery-section") ?? ""));
    expect(names.length, "the gallery has its sections").toBeGreaterThan(15);
    expect(new Set(names).size, `section names are unique: ${names.join(", ")}`).toBe(names.length);
    for (const name of names) {
      const section = page.locator(`[data-gallery-section="${name}"]`);
      await section.scrollIntoViewIfNeeded();
      await expect.soft(section, `${v.name}: ${name}`).toHaveScreenshot(`${v.name}/${name}.png`);
    }
  });
}
