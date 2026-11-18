/*
 * Browser e2e for TR-1431 to TR-1436: the console's motion, with motion on. Every other suite runs under
 * reduced motion (tests/browser.cjs); this one asks for "no-preference" and checks that things arrive and
 * leave instead of jumping, then that reduced motion takes all of it away. It drives /dev/components
 * (the Motion section and the kit) on the real daemon, serving the built console.
 * Run:  npm run build && node tests/motion.e2e.cjs     (from ui/, tares installed in ../.venv)
 * TARES_E2E_PORT sets the daemon port (default 8841; the MCP port is the next one).
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

const PORT = Number(process.env.TARES_E2E_PORT || 8841);
const BASE = `http://127.0.0.1:${PORT}`;

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

/** The animations running on an element now: name and duration in ms. Empty when it is gone. */
const running = (page, selector) => page.evaluate((sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  return e.getAnimations().map((a) => ({ name: a.animationName ?? a.transitionProperty ?? "waapi",
                                         ms: Number(a.effect?.getTiming().duration) || 0 }));
}, selector);

/** Click, then follow `watch` on every frame until `done` holds (or 2s): [{ top, there }] per frame. */
const followAfter = (page, click, watch, gone, want) => page.evaluate(async ([c, w, g, wanted]) => {
  const frames = [];
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  document.querySelector(c).click();
  const t0 = performance.now();
  for (;;) {
    await frame();
    const el = document.querySelector(w);
    // against the stack, not the viewport: scroll anchoring keeps a line above the fold still on screen
    frames.push({ top: el.getBoundingClientRect().top - el.parentElement.getBoundingClientRect().top, there: !!document.querySelector(g), t: performance.now() - t0 });
    if (performance.now() - t0 > 2000) break;
    // settled: in the state asked for, and still for three frames
    if (frames.length > 3 && frames.at(-1).there === wanted && frames.slice(-3).every((f) => f.there === wanted && f.top === frames.at(-1).top)) break;
  }
  return frames;
}, [click, watch, gone, want]);

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-motion-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const env = { ...process.env, TARES_EDITION: "platform", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, PYTHONPATH: REPO };   // the venv installs the main checkout; this runs the code under test
  for (const k of ["TARES_AUTH_TOKEN", "TARES_DB", "TARES_CATALOG", "TARESD_URL"]) delete env[k];
  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home], { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    if (!(await waitFor(`${BASE}/health`))) throw new Error(`the daemon did not answer on ${BASE}`);
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "no-preference" });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const id = (t) => page.getByTestId(t);
    const open = async (p = page) => { await p.goto(`${BASE}/dev/components`, { waitUntil: "networkidle" }); await p.getByTestId("dev-components").waitFor(); };
    await open();

    // TR-1431: the tokens are there for every component to name.
    const tokens = await page.evaluate(() => {
      const s = getComputedStyle(document.documentElement);
      return ["--dur-1", "--dur-2", "--dur-3", "--dur-4", "--dur-wait", "--ease-out", "--ease-in"].map((v) => s.getPropertyValue(v).trim());
    });
    ck("the motion tokens are defined", tokens.every(Boolean) && tokens[0] === ".12s" && tokens[4] === ".2s", tokens.join(" | "));

    // Nothing on the page moves when it first paints: the banner shown from the start, the rows, the status.
    const still = await page.evaluate(() => document.getAnimations()
      .filter((a) => a.playState === "running" && !/pulse/.test(a.animationName ?? ""))
      .map((a) => a.animationName ?? `${a.transitionProperty} on ${a.effect.target.dataset.testid ?? a.effect.target.tagName}`));
    ck("nothing but a skeleton's pulse moves on first paint", still.length === 0 || still.every((n) => n === "m-fade-in"), still.join(", "));

    // TR-1432: a dialog fades and rises in, and leaves on its own, shorter, before it is gone.
    await id("dialog-open").click();
    const inAnim = await running(page, "[role=dialog]");
    ck("a dialog arrives (m-arrive, 180ms)", inAnim?.some((a) => a.name === "m-arrive" && a.ms === 180), JSON.stringify(inAnim));
    const scrim = await page.evaluate(() => [...document.querySelectorAll(".motion-scrim")].map((e) => getComputedStyle(e).animationName));
    ck("its scrim fades in", scrim.includes("m-fade-in"), scrim.join(","));
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    const outAnim = await running(page, "[role=dialog]");
    ck("a closing dialog is still there, leaving (m-leave, 120ms)", outAnim?.some((a) => a.name === "m-leave" && a.ms === 120), JSON.stringify(outAnim));
    await id("dialog").waitFor({ state: "detached", timeout: 2000 });
    ck("then it is gone", (await id("dialog").count()) === 0);

    // A menu pops from its trigger; a toast rises in.
    await id("prim-dropdown").click();
    const menu = await page.evaluate(() => { const m = document.querySelector("[role=menu]"); return m && [getComputedStyle(m).animationName, m.dataset.side, getComputedStyle(m).getPropertyValue("--m-y").trim()]; });
    // it comes from its trigger: below it it starts 4px up, above it 4px down
    ck("a menu pops in from its side", menu && menu[0] === "m-arrive" && menu[2] === ({ bottom: "-4px", top: "4px" })[menu[1]], JSON.stringify(menu));
    await page.keyboard.press("Escape");
    await page.locator("[role=menu]").waitFor({ state: "detached", timeout: 2000 });
    await id("toast-open").click();
    const toast = page.locator("li[data-state=open]").first();
    await toast.waitFor();
    ck("a toast rises in", (await toast.evaluate((e) => getComputedStyle(e).animationName)) === "m-arrive");

    // TR-1434: a banner grows open and shrinks shut, and the line below it never jumps, the gap included.
    await id("collapse-stack").evaluate((e) => e.scrollIntoView({ block: "center" }));
    const below0 = await id("collapse-below").evaluate((e) => e.getBoundingClientRect().top - e.parentElement.getBoundingClientRect().top);
    const opening = await followAfter(page, "[data-testid=collapse-toggle]", "[data-testid=collapse-below]", "[data-testid=collapse-banner]", true);
    const openEnd = opening.at(-1).top;
    const gap = await id("collapse-stack").evaluate((e) => e.style.getPropertyValue("--m-gap"));
    ck("a banner shown later grows open", opening.at(-1).there && openEnd > below0 + 30, `${below0} -> ${openEnd}`);
    const steps = (fs_) => fs_.slice(1).map((f, i) => Math.abs(f.top - fs_[i].top));
    const firstStep = Math.abs(opening[0].top - below0);
    ck("the line below moves down over several frames, not at once", opening.filter((f) => f.top > below0 + 0.5 && f.top < openEnd - 0.5).length >= 3,
       opening.map((f) => f.top.toFixed(1)).join(" "));
    ck("the parent's gap opens with it (measured into --m-gap)", gap === "16px", `gap ${gap}`);
    ck("its first frame does not push the line down by the gap", firstStep < 16, `first step ${firstStep.toFixed(1)}px`);
    const closing = await followAfter(page, "[data-testid=collapse-toggle]", "[data-testid=collapse-below]", "[data-testid=collapse-banner]", false);
    const gone = closing.findIndex((f) => !f.there);
    const lastStep = gone > 0 ? Math.abs(closing[gone].top - closing[gone - 1].top) : NaN;
    ck("a banner going away stays while it shrinks", gone >= 2, `gone at frame ${gone}`);
    ck("the line below is home when it unmounts: no jump by the gap", lastStep <= 2, `last step ${lastStep}px; ${closing.map((f) => `${f.top.toFixed(1)}${f.there ? "" : "x"}`).join(" ")}`);
    ck("and it ends where it started", Math.abs(closing.at(-1).top - below0) <= 0.5, `${closing.at(-1).top} vs ${below0}`);
    ck("no step on the way is a jump", Math.max(...steps(opening), ...steps(closing)) < 40, `${Math.max(...steps(opening), ...steps(closing))}px`);

    // TR-1435: rows glide when they change places; a status that changes washes once.
    await id("moving-shuffle").click();
    const glide = await page.evaluate(() => [...document.querySelectorAll("[data-testid=moving-row]")].map((e) => e.getAnimations().length));
    ck("rows that change places glide (auto-animate)", glide.some((n) => n > 0), glide.join(","));
    ck("the status does not wash when it first shows", !(await id("status-changing").evaluate((e) => e.classList.contains("motion-wash"))));
    await id("status-next").click();
    const wash = await running(page, "[data-testid=status-changing]");
    ck("a status that changes washes (m-wash, 900ms)", wash?.some((a) => a.name === "m-wash" && a.ms === 900), JSON.stringify(wash));

    // TR-1433: a skeleton waits before it shows, so a fast read never flashes it.
    const wait = await page.locator(".motion-wait").first().evaluate((e) => { const s = getComputedStyle(e); return [s.animationName, s.animationDelay, s.animationFillMode]; });
    ck("a loading state waits 200ms, then fades in", wait[0] === "m-fade-in" && wait[1] === "0.2s" && wait[2] === "both", wait.join(" "));

    // TR-1436: the tab underline slides to the tab chosen; a button gives under the press.
    const tabs = id("tabs").locator("[role=tablist]").first();
    const second = tabs.locator("[role=tab]").nth(1);
    await second.click();
    const sliding = await tabs.evaluate((l) => { const line = l.querySelector("[data-tab-line]"); return [l.dataset.line, getComputedStyle(line).transitionProperty]; });
    ck("the tab line is placed by the list and slides", sliding[0] === "on" && /transform/.test(sliding[1]), sliding.join(" "));
    await page.waitForTimeout(400);
    const placed = await tabs.evaluate((l) => {
      const line = l.querySelector("[data-tab-line]").getBoundingClientRect();
      const tab = l.querySelector('[role=tab][data-state=active]').getBoundingClientRect();
      return [Math.abs(line.left - tab.left), Math.abs(line.width - tab.width), getComputedStyle(l.querySelector('[role=tab][data-state=active]')).borderBottomColor];
    });
    ck("then it sits under the chosen tab, as wide as it", placed[0] < 1 && placed[1] < 1, placed.join(" "));
    ck("and the tabs' own borders give way to it", placed[2] === "rgba(0, 0, 0, 0)", placed[2]);
    // the border goes in the same frame: a tab never transitions it, or placing the line fades on a slow first paint
    const tabTransitions = await tabs.locator("[role=tab]").evaluateAll((ts) => ts.map((t) => getComputedStyle(t).transitionProperty));
    ck("a tab transitions its colours, never its border", tabTransitions.every((t) => /color/.test(t) && !/border|all/.test(t)), tabTransitions.join(" | "));
    const press = await id("btn-primary").evaluate((b) => getComputedStyle(b).transitionProperty);
    ck("a button eases its press and its colours", /scale|all/.test(press) && /background-color|all/.test(press), press);

    // Reduced motion: the same page with every animation over at once.
    const calm = await browser.newPage({ viewport: { width: 1440, height: 900 } });   // browser.cjs: reduce
    await open(calm);
    await calm.getByTestId("dialog-open").click();
    const calmDialog = await calm.evaluate(() => getComputedStyle(document.querySelector("[role=dialog]")).animationDuration);
    ck("reduced motion: a dialog arrives at once", calmDialog === "0.001s", calmDialog);
    await calm.keyboard.press("Escape");
    await calm.getByTestId("collapse-toggle").click();
    await calm.getByTestId("collapse-banner").waitFor();
    const t = Date.now();
    await calm.getByTestId("collapse-toggle").click();
    await calm.getByTestId("collapse-banner").waitFor({ state: "detached", timeout: 2000 });
    ck("reduced motion: a banner leaves at once", Date.now() - t < 300, `${Date.now() - t}ms`);
    const calmWait = await calm.locator(".motion-wait").first().evaluate((e) => getComputedStyle(e).animationDelay);
    ck("reduced motion: a loading state does not wait", calmWait === "0s", calmWait);
    await calm.getByTestId("moving-shuffle").click();
    const calmGlide = await calm.evaluate(() => [...document.querySelectorAll("[data-testid=moving-row]")].reduce((n, e) => n + e.getAnimations().length, 0));
    ck("reduced motion: rows change places without gliding", calmGlide === 0, String(calmGlide));
    // transition-property defaults to all: a guard that gave transitions any duration would start one on every
    // style change of every element, and a colour read in the same tick would still be the old one.
    const calmPaint = await calm.evaluate(() => {
      const i = document.createElement("i"); document.body.appendChild(i);
      i.style.backgroundColor = "rgb(0, 0, 0)"; getComputedStyle(i).backgroundColor;
      i.style.backgroundColor = "rgb(255, 0, 0)";
      const out = { now: getComputedStyle(i).backgroundColor, running: i.getAnimations().length }; i.remove(); return out;
    });
    ck("reduced motion: a style change applies at once, with no transition started", calmPaint.now === "rgb(255, 0, 0)" && calmPaint.running === 0,
       JSON.stringify(calmPaint));

    ck("no page errors", errors.length === 0, errors.join(" | "));
  } catch (e) {
    fail++;
    console.log("  FAIL crashed: " + (e.stack || e));
  } finally {
    if (browser) await browser.close();
    daemon.kill("SIGTERM");
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
