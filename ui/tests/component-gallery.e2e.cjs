/*
 * Browser e2e for TR-725: /dev/components shows every edition component, square and flat, in light
 * and dark, and fits a 390px screen. Runs the real daemon on a scratch home, serving the built console.
 * Run:  npm run build && node tests/component-gallery.e2e.cjs     (from ui/, tares installed in ../.venv)
 * Set SHOTS_DIR to keep gallery-light.png, gallery-dark.png and gallery-narrow.png.
 * TARES_E2E_PORT sets the daemon port (default 8831; the MCP port is the next one).
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));

const PORT = Number(process.env.TARES_E2E_PORT || 8831);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 100) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const TESTIDS = ["btn-primary", "btn-secondary", "btn-quiet", "list-section", "panel", "tabs", "dialog-open", "empty-state", "stat",
  "progress", "initials", "status-label", "skeleton", "input", "textarea", "select", "switch", "checkbox", "banner", "toast-open",
  "sparkline", "line-chart", "probe-input", "probe-textarea", "probe-select", "probe-switch", "probe-checkbox", "inert-link", "live-link"];

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-gallery-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const env = { ...process.env, TARES_EDITION: "platform", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    const id = (t) => page.getByTestId(t);
    const shot = async (n) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, n), fullPage: true }); };
    const style = (t, props) => id(t).first().evaluate((e, ps) => { const s = getComputedStyle(e); return ps.map((p) => s[p]); }, props);
    const num = (s) => Number(String(s).replace(/,/g, ""));
    // [x, y] pairs of a path `d` ("M0 80L50 40") or of a polyline `points` ("0,80 50,40"); a NaN never matches, so it shows as a missing pair.
    const pairs = (s) => [...String(s).matchAll(/(-?\d+(?:\.\d+)?(?:e-?\d+)?)[ ,](-?\d+(?:\.\d+)?(?:e-?\d+)?)/g)].map((m) => [Number(m[1]), Number(m[2])]);
    const near = (a, b, eps = 0.01) => Math.abs(a - b) <= eps;
    const pathD = (t) => id(t).locator("path").first().getAttribute("d");
    const yLabels = async (t) => (await id(t).locator("[data-y-axis] span").allInnerTexts()).map(num);
    const wide = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    const open = async () => { await page.goto(`${BASE}/dev/components`, { waitUntil: "networkidle" }); await id("dev-components").waitFor(); };

    await open();
    for (const t of TESTIDS) ck(`${t} is on the page`, await id(t).first().isVisible());

    // Square and flat: radius 0, no shadow.
    for (const t of ["btn-primary", "btn-secondary", "panel", "input"]) {
      const [r, sh] = await style(t, ["borderRadius", "boxShadow"]);
      ck(`${t} has square corners and no shadow`, r === "0px" && sh === "none", `${r} / ${sh}`);
    }
    await id("dialog-open").click();
    await id("dialog").waitFor();
    const [dr, dsh] = await style("dialog", ["borderRadius", "boxShadow"]);
    ck("the dialog opens, square, with no shadow", dr === "0px" && dsh === "none", `${dr} / ${dsh}`);
    await page.keyboard.press("Escape");
    await id("dialog").waitFor({ state: "detached" });

    await id("toast-open").click();
    const toast = page.locator("li[data-state=open]").filter({ hasText: "Settings for project-x are saved." });
    ck("the toast shows", await toast.isVisible());

    // The charts: a polyline, and two series as paths with a dashed mark.
    ck("the sparkline draws a polyline", (await id("sparkline").locator("polyline").count()) === 1);
    ck("the line chart has two series as paths", (await id("line-chart").locator("path").count()) === 2);
    ck("it has at least one dashed mark with a label", (await id("line-chart").locator("line[stroke-dasharray] + line title").count()) >= 1);
    ck("it names both series in its legend", /Errors/.test(await id("line-chart").innerText()) && /Requests per minute/.test(await id("line-chart").innerText()));
    ck("an empty chart says so", await page.getByText("No data in this window.").isVisible());
    ck("and draws no path", (await id("line-chart-empty").locator("path").count()) === 0);

    // Values are not trusted: a point that is not a finite number is dropped, the rest are sorted and still scaled to the real maximum.
    const dirtyD = await pathD("line-chart-dirty");
    const dirty = pairs(dirtyD);
    ck("a line chart drops null, undefined, NaN and Infinity values (3 of 7 points are drawn)", dirty.length === 3 && !/NaN|Infinity/.test(dirtyD), dirtyD);
    ck("a line chart sorts its points by time", dirty.length === 3 && dirty[0][0] === 0 && near(dirty[1][0], 50) && dirty[2][0] === 100, dirtyD);
    ck("the real values keep their height instead of clamping flat", new Set(dirty.map(([, y]) => y)).size === 3 && dirty[2][1] === 0, dirtyD);
    ck("its y axis follows the real maximum (0, 5, 10)", JSON.stringify(await yLabels("line-chart-dirty")) === "[0,5,10]", String(await yLabels("line-chart-dirty")));
    const sparkDirty = pairs(await id("sparkline-dirty").locator("polyline").getAttribute("points"));
    ck("a sparkline drops values that are not numbers and keeps the x of the others", sparkDirty.length === 3 && sparkDirty[0][0] === 0 && near(sparkDirty[1][0], 100 / 3) && sparkDirty[2][0] === 100, JSON.stringify(sparkDirty));
    ck("a sparkline mark still sits on its original point", near(Number(await id("sparkline-dirty").locator("line").first().getAttribute("x1")), 100 / 3), await id("sparkline-dirty").locator("line").first().getAttribute("x1"));
    ck("a 200000-point line chart draws", (await id("line-chart-huge-failed").count()) === 0 && await id("line-chart-huge").locator("path").first().evaluate((e) => { const d = e.getAttribute("d"); return d.length > 1e6 && !/NaN|Infinity/.test(d); }));
    ck("a 200000-point sparkline draws, without its 200 gaps", (await id("sparkline-huge-failed").count()) === 0 && await id("sparkline-huge").locator("polyline").evaluate((e) => { const p = e.getAttribute("points"); return p.split(" ").length === 199800 && !/NaN|Infinity/.test(p); }));

    // The y labels carry as many decimals as the gridline values need.
    const small = await yLabels("line-chart-small");
    const smallText = await id("line-chart-small").locator("[data-y-axis] span").allInnerTexts();
    ck("a chart with a top of 0.005 labels its gridlines 0, 0.0025 and 0.005", small.length === 3 && near(small[0], 0, 1e-9) && near(small[1], 0.0025, 1e-9) && near(small[2], 0.005, 1e-9), smallText.join(" / "));
    ck("and the three labels are distinct", new Set(smallText).size === 3, smallText.join(" / "));
    ck("a chart with a top of 20 labels them 0, 10 and 20", JSON.stringify(await yLabels("line-chart")) === "[0,10,20]", String(await yLabels("line-chart")));

    // One point is a short segment at its time, not a line across the chart.
    for (const t of ["line-chart-one", "line-chart-one-gaps"]) {
      const m = /^M([\d.]+) [\d.]+H([\d.]+)$/.exec((await pathD(t)) || "");
      ck(`${t}: a lone point is a short segment centred on its time`, !!m && Number(m[2]) - Number(m[1]) > 0 && Number(m[2]) - Number(m[1]) <= 20 && near((Number(m[1]) + Number(m[2])) / 2, 50), m ? m[0] : "no path");
    }
    for (const [t, from, to] of [["sparkline-one", 47, 53], ["sparkline-one-gaps", 0, 6]]) {
      const p = pairs(await id(t).locator("polyline").getAttribute("points"));
      ck(`${t}: a lone value is a short segment at its x`, p.length === 2 && near(p[0][0], from) && near(p[1][0], to), JSON.stringify(p));
    }

    // The marks are listed as text, oldest first, including the ones outside the plotted range.
    const marks = id("line-chart").locator("ul[data-marks-list]");
    const markLabels = await marks.locator("li span").allInnerTexts();
    ck("the chart lists its marks as text, oldest first, out-of-range ones included", JSON.stringify(markLabels) === JSON.stringify(["Deploy of service-d", "Deploy of service-a", "Deploy of service-b", "Deploy of service-c", "Deploy of service-e"]), markLabels.join(" | "));
    ck("each listed mark has a time", (await marks.locator("li time").count()) === 5);
    ck("only the marks inside the range are drawn as lines", (await id("line-chart").locator("svg g[data-mark]").count()) === 3);

    // F13, F14: a caller's aria-invalid reaches the control, and an inert link neither runs its onClick nor navigates.
    const invalid = {};
    for (const t of ["probe-input", "probe-textarea", "probe-select", "probe-switch", "probe-checkbox"]) invalid[t] = await id(t).getAttribute("aria-invalid");
    ck("a caller's aria-invalid shows on Input, Textarea, Select, Switch and Checkbox", Object.values(invalid).every((v) => v === "true"), JSON.stringify(invalid));
    const hash = () => page.evaluate(() => location.hash);
    const clicks = async () => ({ inert: await id("inert-clicks").innerText(), live: await id("live-clicks").innerText() });
    const h0 = await hash();
    const attrs = {};
    for (const t of ["inert-link", "busy-link"]) attrs[t] = await id(t).evaluate((e) => [e.getAttribute("tabindex"), e.getAttribute("aria-disabled")]);
    ck("an inert link has tabindex -1 and aria-disabled", Object.values(attrs).every(([ti, ad]) => ti === "-1" && ad === "true"), JSON.stringify(attrs));
    for (const t of ["inert-link", "busy-link"]) await id(t).evaluate((e) => e.click());
    ck("a programmatic click on an inert link runs no onClick and does not navigate", (await clicks()).inert === "Inert clicks: 0" && (await hash()) === h0, `${(await clicks()).inert}, ${await hash()}`);
    let focused = true;
    for (const t of ["inert-link", "busy-link"]) {
      await id(t).evaluate((e) => e.focus());
      focused = focused && (await id(t).evaluate((e) => document.activeElement === e));
      await page.keyboard.press("Enter");
    }
    ck("Enter on a focused inert link runs no onClick and does not navigate", focused && (await clicks()).inert === "Inert clicks: 0" && (await hash()) === h0, `focused ${focused}, ${(await clicks()).inert}, ${await hash()}`);
    await id("live-link").click();
    await id("live-link").evaluate((e) => e.focus());
    await page.keyboard.press("Enter");
    ck("a live link still runs its onClick on click and on Enter", (await clicks()).live === "live clicks: 2", (await clicks()).live);

    // F4: the primitives match the console's own rules.
    const opacity = {};
    for (const [name, sel] of [["button", "button[data-slot=button]:disabled"], ["input", "input[data-slot=input]:disabled"], ["tab", "[role=tab]:disabled"],
      ["select", "button[role=combobox]:disabled"], ["switch", "button[role=switch]:disabled"], ["checkbox", "button[role=checkbox]:disabled"], ["inert link", "[data-testid=inert-link]"]]) {
      opacity[name] = await page.locator(sel).first().evaluate((e) => getComputedStyle(e).opacity);
    }
    ck("every disabled control computes opacity 0.45, as the console's disabled buttons do", Object.values(opacity).every((v) => v === "0.45"), JSON.stringify(opacity));
    const accent = await page.evaluate(() => { const p = document.createElement("span"); p.style.color = "var(--accent)"; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; });
    for (const t of ["input", "textarea"]) {
      await id(t).focus();
      await page.waitForTimeout(400);   // the border colour transitions
      const [b] = await style(t, ["borderTopColor"]);
      ck(`a focused ${t} turns its border to the accent`, b === accent, `${b} vs ${accent}`);
    }

    ck("no horizontal scroll at 1440px", (await wide()) <= 0, `${await wide()}px`);
    await page.getByTestId("theme-light").click();
    await shot("gallery-light.png");

    await page.evaluate(() => localStorage.setItem("tares-theme", "dark"));
    await open();
    ck("dark theme applies from storage", (await page.evaluate(() => document.documentElement.getAttribute("data-theme"))) === "dark");
    ck("the page background changes with the theme", (await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) !== "rgb(255, 255, 255)");
    await shot("gallery-dark.png");

    await page.setViewportSize({ width: 390, height: 844 });
    await open();
    ck("no horizontal scroll at 390px", (await wide()) <= 0, `${await wide()}px`);
    ck("the line chart fits at 390px", await id("line-chart").evaluate((e) => e.getBoundingClientRect().right <= window.innerWidth));
    await shot("gallery-narrow.png");

    ck("no console errors", errors.length === 0, errors.join(" | "));
  } finally {
    if (browser) await browser.close();
    daemon.kill();
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
