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
const { chromium } = require("./browser.cjs");
const { checkType } = require("./type.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8831);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;

let pass = 0, fail = 0;
// TR-1265: every status in STATUS_TONES renders from the one StatusLabel, and amber is live work only.
// The gallery walks the table, so a status or a domain added later is checked here without a new line.
async function checkStatuses(page, theme) {
  const got = await page.evaluate(() => {
    const paint = (v) => { const p = document.createElement("span"); p.style.color = `var(${v})`; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; };
    const amber = [paint("--accent"), paint("--accent-deep"), paint("--warn")];
    const labels = [...document.querySelectorAll("[data-testid=status-every] [data-status]")].map((e) => {
      const s = getComputedStyle(e);
      return { key: `${e.dataset.domain}:${e.dataset.status}`, tone: e.dataset.tone, text: e.textContent.trim(), color: s.color,
               line: s.textDecorationLine, lineColor: s.textDecorationColor, lineStyle: s.textDecorationStyle,
               shown: e.getBoundingClientRect().width > 0 };
    });
    return { amber, labels, domains: new Set(labels.map((l) => l.key.split(":")[0])).size };
  });
  const { amber, labels } = got;
  const isAmber = (c) => amber.includes(c);
  ck(`${theme}: the gallery lists every status table (${got.domains} domains, ${labels.length} statuses)`, got.domains >= 12 && labels.length >= 57, `${got.domains} / ${labels.length}`);
  ck(`${theme}: every status shows its words`, labels.every((l) => l.shown && l.text.length > 0), labels.filter((l) => !l.shown || !l.text).map((l) => l.key).join(", "));
  const stray = labels.filter((l) => l.tone !== "working" && (isAmber(l.color) || isAmber(l.lineColor)));
  ck(`${theme}: no status but live work uses the action amber`, stray.length === 0, stray.map((l) => `${l.key} ${l.color}`).join(", "));
  const working = labels.filter((l) => l.tone === "working");
  ck(`${theme}: live work is amber, underlined in the accent`, working.length > 0 && working.every((l) => isAmber(l.color) && l.lineColor === amber[0] && l.line.includes("underline")),
     working.map((l) => `${l.key} ${l.color} ${l.lineColor}`).join(", "));
  const waits = labels.filter((l) => l.tone === "waiting");
  ck(`${theme}: a waiting status has a dashed underline`, waits.length > 0 && waits.every((l) => l.lineStyle === "dashed" && l.line.includes("underline")),
     waits.map((l) => `${l.key} ${l.lineStyle}`).join(", "));
  const done = labels.filter((l) => l.tone === "done");
  ck(`${theme}: a done status is plain`, done.length > 0 && done.every((l) => !l.line.includes("underline")), done.map((l) => `${l.key} ${l.line}`).join(", "));
}

// TR-1264: one Loading and one EmptyState. Loading tells a screen reader the area is busy and holds only
// Skeleton bars; EmptyState is a titled block for a page with nothing in it, or a line for an empty section.
// The suites run under reduced motion (tests/browser.cjs), where the bars hold still on purpose, so the pulse is
// asked of the class here; motion.e2e.cjs sees it move.
async function checkStates(page) {
  const got = await page.evaluate(() => {
    const busy = (id) => {
      const e = document.querySelector(`[data-testid=${id}]`);
      if (!e) return null;
      const bars = [...e.querySelectorAll("[aria-hidden=true]")];
      return { role: e.getAttribute("role"), busy: e.getAttribute("aria-busy"), label: e.getAttribute("aria-label"),
               bars: bars.length, pulse: bars.length > 0 && bars.every((b) => b.classList.contains("animate-pulse")), words: e.textContent.trim() };
    };
    const empty = (id) => {
      const e = document.querySelector(`[data-testid=${id}]`);
      return e && { kind: e.dataset.empty, heading: !!e.querySelector("h3"), words: e.textContent.trim() };
    };
    return { loading: busy("loading"), blocks: busy("loading-blocks"), block: empty("empty-state"), line: empty("empty-line") };
  });
  for (const [name, l] of [["Loading", got.loading], ["LoadingBlocks", got.blocks]]) {
    ck(`${name} is one busy status for a screen reader`, l && l.role === "status" && l.busy === "true" && l.label === "Loading", JSON.stringify(l));
    ck(`${name} holds pulsing bars and no words`, l && l.bars >= 3 && l.pulse && l.words === "", JSON.stringify(l));
  }
  ck("an empty state with a title is a block with a heading", got.block && got.block.kind === "block" && got.block.heading, JSON.stringify(got.block));
  ck("an empty state without a title is one line in the voice, no heading", got.line && got.line.kind === "line" && !got.line.heading && /yet\./.test(got.line.words),
     JSON.stringify(got.line));
}

// TR-1270: a headline numeral is Doto, the face Rius uses, in both themes; its label is Geist Mono. Departure
// Mono is gone: no font face of that name is left for the page to load.
async function checkNumerals(page, theme) {
  const got = await page.evaluate(async () => {
    const stat = document.querySelector("[data-testid=stat]");
    const [label, value] = stat ? [...stat.children] : [];
    const loaded = await document.fonts.load('900 26px "Doto Variable"', "0123456789");
    return { value: value && getComputedStyle(value).fontFamily, weight: value && getComputedStyle(value).fontWeight,
             label: label && getComputedStyle(label).fontFamily, doto: loaded.length,
             departure: [...document.fonts].filter((f) => /departure/i.test(f.family)).length };
  });
  ck(`${theme}: a Stat value is set in Doto, black, and the face loads`, /^"?Doto Variable/.test(got.value || "") && got.weight === "900" && got.doto > 0, JSON.stringify(got));
  ck(`${theme}: a Stat label is Geist Mono`, /^"?Geist Mono/.test(got.label || ""), JSON.stringify(got));
  ck(`${theme}: no Departure Mono face is left`, got.departure === 0, JSON.stringify(got));
}

// TR-1667: WeekChart is a plot over a table with a column per week: every value is read without hovering, a
// bar sits over its own week, and the data is drawn in ink.
async function checkWeekChart(page) {
  const got = await page.evaluate(() => {
    const paint = (v) => { const p = document.createElement("span"); p.style.color = `var(${v})`; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; };
    const chart = (id) => document.querySelector(`[data-testid=${id}]`);
    const texts = (root, series) => [...root.querySelectorAll(`tr[data-series="${series}"] td`)].map((c) => c.textContent);
    const bars = chart("week-chart-bars");
    const heads = [...bars.querySelectorAll("th[data-week]")];
    const rects = [...bars.querySelectorAll("rect[data-bar]")].map((r) => {
      const box = r.getBoundingClientRect(), mid = box.left + box.width / 2;
      return { series: r.dataset.bar, height: Number(r.getAttribute("height")), fill: getComputedStyle(r).fill, opacity: getComputedStyle(r).opacity,
               week: heads.findIndex((h) => { const b = h.getBoundingClientRect(); return mid >= b.left && mid <= b.right; }),
               col: Math.floor(Number(r.getAttribute("x")) / 10) };
    });
    const lines = chart("week-chart-lines");
    const empty = chart("week-chart-empty");
    return {
      weeks: heads.map((h) => h.dataset.week), heads: heads.map((h) => h.textContent),
      merged: texts(bars, "Merged"), rects, ink: paint("--ink"), soft: paint("--ink-soft"),
      lines: [...lines.querySelectorAll("g[data-line]")].map((g) => ({ series: g.dataset.line, d: g.querySelector("path").getAttribute("d"),
        dash: g.querySelector("path").getAttribute("stroke-dasharray"), dots: g.querySelectorAll("line[data-dot]").length, stroke: getComputedStyle(g.querySelector("path")).stroke })),
      listed: lines.querySelectorAll("tr[data-series]").length, half: texts(lines, "Half"),
      swatches: [...lines.querySelectorAll("tr[data-series] th svg")].map((s) => { const l = s.querySelector("line"); return l ? l.getAttribute("stroke-dasharray") || "none" : "empty"; }),
      empty: { words: empty.textContent, table: !!empty.querySelector("table") },
    };
  });
  ck("week chart: a column per week, named by its day, the running one marked", got.weeks.length === 8 && got.heads[0] === "Oct 5" && got.heads[7] === "Nov 23so far" && got.heads.filter((h) => /so far/.test(h)).length === 1, got.heads.join(" | "));
  ck("week chart: every value is in view as text, and a week with no value says so", JSON.stringify(got.merged) === JSON.stringify(["3", "5", "4", "0", "7", "6", "-no value", "2"]), got.merged.join(" | "));
  const merged = got.rects.filter((r) => r.series === "Merged");
  ck("week chart: a bar for each value above 0, none for 0 or no value", merged.length === 6 && got.rects.length === 13, `${merged.length} of ${got.rects.length}`);
  ck("week chart: each bar sits over its own week's column", got.rects.every((r) => r.week === r.col) && JSON.stringify(merged.map((r) => r.week)) === "[0,1,2,4,5,7]", JSON.stringify(got.rects.map((r) => [r.week, r.col])));
  const top = Math.max(...got.rects.map((r) => r.height));
  ck("week chart: heights share one scale from 0 (4 is half of 8)", Math.abs(merged[2].height - top / 2) < 0.01 && top === 116, `${merged[2] && merged[2].height} of ${top}`);
  ck("week chart: bars are drawn in ink, the second series softer", merged.every((r) => r.fill === got.ink) && got.rects.filter((r) => r.series === "Reviewed").every((r) => r.fill === got.soft), JSON.stringify([got.ink, got.soft, merged[0] && merged[0].fill]));
  ck("week chart: the running week's bars are lighter", got.rects.every((r) => (r.week === 7) === (r.opacity === "0.5")), JSON.stringify(got.rects.map((r) => r.opacity)));
  ck("week chart: four lines are drawn and five series listed", got.lines.length === 4 && got.listed === 5, `${got.lines.length} / ${got.listed}`);
  ck("week chart: a week with no value breaks the line", got.lines.every((l) => (l.d.match(/M/g) || []).length === 2 && l.dots === 7 && !/NaN/.test(l.d)), JSON.stringify(got.lines.map((l) => [l.d, l.dots])));
  ck("week chart: lines differ by ink and by dash, the fourth in ink again", new Set(got.lines.map((l) => l.stroke)).size === 3 && got.lines[3]?.stroke === got.lines[0]?.stroke && JSON.stringify(got.lines.map((l) => l.dash)) === JSON.stringify([null, "5 3", "1.5 3", "8 3 1.5 3"]), JSON.stringify(got.lines.map((l) => [l.stroke, l.dash])));
  ck("week chart: each drawn line has its swatch in the legend, with the same dash", JSON.stringify(got.swatches) === JSON.stringify(["none", "5 3", "1.5 3", "8 3 1.5 3", "empty"]), JSON.stringify(got.swatches));
  ck("week chart: the caller's format says the unit", got.half[0] === "5 h" && got.half[1] === "6.5 h", got.half.join(" | "));
  ck("week chart: with no values it says so and draws no table", /Nothing to chart: no data in these weeks\./.test(got.empty.words) && !got.empty.table, got.empty.words);
  ck("week chart: the table is named by its caption", (await page.getByRole("table", { name: "Tickets merged and reviewed per week" }).count()) === 1);
  const region = page.getByRole("region", { name: "Cost per week, API-equivalent" });
  ck("week chart: weeks that do not fit scroll in a region the keyboard reaches", (await region.getAttribute("tabindex")) === "0" && await region.evaluate((e) => e.scrollWidth > e.clientWidth), "");
  const held = await region.evaluate((e) => { e.scrollLeft = 400; const th = e.querySelector("tr[data-series] th"); const at = th.getBoundingClientRect().left - e.getBoundingClientRect().left; const moved = e.scrollLeft; e.scrollLeft = 0; return { at, moved }; });
  ck("week chart: the series names stay in place while the weeks scroll", held.moved === 400 && Math.abs(held.at) < 1, JSON.stringify(held));
  // the platform pages' element styles for tables must not box the figure
  const box = await region.evaluate((e) => { const t = getComputedStyle(e.querySelector("table")); const h = getComputedStyle(e.querySelector("th[data-week]")); return [t.borderTopWidth, t.backgroundColor, h.backgroundColor, h.borderBottomWidth]; });
  ck("week chart: the figure sits on the page, with no box or filled head", JSON.stringify(box) === JSON.stringify(["0px", "rgba(0, 0, 0, 0)", "rgba(0, 0, 0, 0)", "0px"]), JSON.stringify(box));
}

// The kit V2 ladder (TR-1271, argus-ui theme.css --agency-dark-*). Light keeps the values it had.
const LADDER = {
  light: { canvas: "rgb(240, 239, 226)", panel: "rgb(250, 249, 236)", popover: "rgb(250, 249, 236)", focus: "rgb(240, 239, 226)" },
  dark: { canvas: "rgb(43, 22, 0)", panel: "rgb(73, 36, 0)", popover: "rgb(120, 62, 6)", focus: "rgb(88, 47, 6)" },
};
async function checkLadder(page, theme) {
  await page.getByTestId("input").focus();
  await page.waitForTimeout(400);   // the fill transitions
  const got = await page.evaluate(() => {
    const bg = (e) => e && getComputedStyle(e).backgroundColor;
    const probe = document.createElement("div");
    probe.className = "bg-popover";
    document.body.append(probe);
    const out = { canvas: bg(document.body), panel: bg(document.querySelector("[data-testid=panel]")), popover: bg(probe),
                  focus: bg(document.activeElement) };
    probe.remove();
    return out;
  });
  await page.getByTestId("input").blur();
  for (const k of Object.keys(LADDER[theme])) {
    ck(`${theme}: the ${k === "focus" ? "focused field" : k} is ${LADDER[theme][k]}`, got[k] === LADDER[theme][k], JSON.stringify(got));
  }
}

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

const TESTIDS = ["btn-primary", "btn-secondary", "btn-quiet", "list-section", "panel", "tabs", "dialog-open", "empty-state", "empty-line", "stat",
  "progress", "initials", "status-label", "skeleton", "loading", "loading-blocks", "input", "textarea", "select", "search-select", "switch", "checkbox", "banner", "toast-open",
  "sparkline", "line-chart", "week-chart-bars", "week-chart-lines", "probe-input", "probe-textarea", "probe-select", "probe-switch", "probe-checkbox", "inert-link", "live-link",
  "collapse-toggle", "collapse-stack", "moving-list", "status-changing"];

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-gallery-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const env = { ...process.env, TARES_EDITION: "platform", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
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

    await checkWeekChart(page);

    // SearchSelect: typing narrows the rows, the first ("All ...") stays, Enter picks, Escape closes
    {
      const ss = id("search-select");
      ck("search select: the label names the button", (await page.getByRole("button", { name: "Project" }).count()) >= 1);
      await ss.click();
      const opts = async () => (await id("search-select-option").allInnerTexts()).map((t) => t.trim());
      ck("search select: the search box has focus when the list opens", await id("search-select-search").evaluate((e) => e === document.activeElement));
      ck("search select: every row shows before typing", (await opts()).length === 9, JSON.stringify(await opts()));
      await id("search-select-search").fill("STATE");
      ck("search select: typing keeps the rows that hold the words, any case, and the All row", JSON.stringify(await opts()) === JSON.stringify(["All projects", "state-store"]), JSON.stringify(await opts()));
      await id("search-select-search").fill("nothing-like-this");
      ck("search select: no match says so", (await id("search-select-none").count()) === 1 && JSON.stringify(await opts()) === JSON.stringify(["All projects"]));
      await id("search-select-search").fill("bill");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      ck("search select: arrow down and Enter pick a row, the list closes", (await ss.innerText()).trim() === "billing-export" && (await id("search-select-panel").count()) === 0, await ss.innerText());
      ck("search select: focus goes back to the button", await ss.evaluate((e) => e === document.activeElement));
      await ss.click();
      await page.keyboard.press("Escape");
      ck("search select: Escape closes without a change", (await id("search-select-panel").count()) === 0 && (await ss.innerText()).trim() === "billing-export");
      const name = async () => (await page.evaluate((el) => {
        const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
        return ids.map((i) => document.getElementById(i)?.textContent?.trim() ?? "").join(" ");
      }, await ss.elementHandle()));
      ck("search select: the button's name says the label and the value chosen", (await name()) === "Project billing-export", await name());

      // Enter right after typing picks the first match, not the All row kept on top
      await ss.click();
      await id("search-select-search").fill("state");
      await page.keyboard.press("Enter");
      ck("search select: typing then Enter picks the first match", (await ss.innerText()).trim() === "state-store", await ss.innerText());
      // Enter on open keeps the value chosen
      await ss.click();
      await page.keyboard.press("Enter");
      ck("search select: open then Enter keeps the value chosen", (await ss.innerText()).trim() === "state-store", await ss.innerText());
      // closing by the button forgets the search
      await ss.click();
      await id("search-select-search").fill("zzz");
      await ss.click();
      await ss.click();
      ck("search select: closed by its button, the search starts empty again", (await id("search-select-search").inputValue()) === "" && (await id("search-select-option").count()) === 9);
      // a press on the no-match line keeps the keys working
      await id("search-select-search").fill("zzz");
      await id("search-select-none").click();
      ck("search select: a press in the panel keeps focus in the search box", await id("search-select-search").evaluate((e) => e === document.activeElement));
      await page.keyboard.press("Escape");
      ck("search select: and Escape still closes", (await id("search-select-panel").count()) === 0);
      // Tab closes and moves on
      await ss.click();
      await page.keyboard.press("Tab");
      ck("search select: Tab closes the list", (await id("search-select-panel").count()) === 0);
      // a click outside closes without a change
      await ss.click();
      await page.mouse.click(5, 5);
      ck("search select: a click outside closes without a change", (await id("search-select-panel").count()) === 0 && (await ss.innerText()).trim() === "state-store");
    }

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
    await checkStatuses(page, "light");
    await checkStates(page);
    await checkNumerals(page, "light");
    await checkLadder(page, "light");
    await checkType(page, ck, "light");
    await page.getByTestId("theme-light").click();
    await shot("gallery-light.png");

    await page.evaluate(() => localStorage.setItem("tares-theme", "dark"));
    await open();
    ck("dark theme applies from storage", (await page.evaluate(() => document.documentElement.getAttribute("data-theme"))) === "dark");
    ck("the page background changes with the theme", (await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) !== "rgb(255, 255, 255)");
    await shot("gallery-dark.png");
    await checkStatuses(page, "dark");
    await checkNumerals(page, "dark");
    await checkLadder(page, "dark");

    await page.setViewportSize({ width: 390, height: 844 });
    await open();
    ck("no horizontal scroll at 390px", (await wide()) <= 0, `${await wide()}px`);
    ck("the line chart fits at 390px", await id("line-chart").evaluate((e) => e.getBoundingClientRect().right <= window.innerWidth));
    ck("the week chart fits at 390px and scrolls inside itself", await id("week-chart-bars").evaluate((e) => { const r = e.querySelector("[role=region]"); return e.getBoundingClientRect().right <= window.innerWidth && r.scrollWidth > r.clientWidth; }));
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
