/*
 * Browser e2e for TR-828: a hidden Start checklist can be shown again.
 *
 * Seeds a scratch factory cell with tests/seed-factory-start.py (GitHub connected, one access key that has been
 * used: 2 of 5 steps done), then starts one daemon on it with a root token, on TARES_E2E_PORT (default 8280), MCP
 * port +1, in a clean environment and a fresh data directory, and stops only it.
 * Sample (browser flag taresSamples=1): startHidden at 1440 and 390 wide: the quiet line and its button.
 * Live: the list is shown; Hide removes it and the line "The list "Get your factory going" is hidden." with a
 * Show it again button takes its place; Show it again calls the cell once with DELETE and brings the list back
 * with its five steps; a Show it again that fails keeps the line and says so; the line stays after a reload;
 * another key never sees the line. The words check runs in every state. Shots: factory-start-show-<state>-<width>.png.
 * Run:  node tests/factory-start-show.e2e.cjs     (the daemon serves ui/dist: build first)
 * Set SHOTS_DIR to keep screenshots. TARES_E2E_CHROME points Playwright at another Chrome binary.
 */
const http = require("http");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const { checkWords, expectOneCall } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8280);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR;
const ROOT = "root-token-start-show-e2e";
const LINE = "The list “Get your factory going” is hidden.";

let pass = 0, fail = 0;
const ck = (label, cond, detail = "") => {
  cond ? pass++ : fail++;
  console.log((cond ? "  ok   " : "  FAIL ") + label + (cond ? "" : `   ${detail}`));
};

async function waitFor(url, tries = 150) {
  for (; tries > 0; tries--) {
    const ok = await new Promise((res) => http.get(url, (r) => { r.resume(); res(true); }).on("error", () => res(false)));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

function cleanEnv(extra) {
  const keep = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

(async () => {
  const bin = path.join(REPO, ".venv/bin");
  const tares = fs.existsSync(path.join(bin, "tares")) ? path.join(bin, "tares") : "tares";
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-factory-start-show-e2e-"));
  const python = process.env.TARES_E2E_PYTHON || (fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3");
  const base = { TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1), TARES_UI_DIST: dist,
                 TARES_OTLP_GRPC_PORT: "off", TARES_EDITION: "factory", PYTHONPATH: REPO };

  const seeded = spawnSync(python, [path.join(__dirname, "seed-factory-start.py"), home, String(PORT), String(PORT + 1), dist, tares],
                           { cwd: REPO, encoding: "utf8", env: cleanEnv(base) });
  ck("seed ran", seeded.status === 0, (seeded.stderr || seeded.stdout || "").slice(-300));
  if (seeded.status !== 0) { console.log(`\n${pass} passed, ${fail} failed`); process.exit(1); }
  const seed = JSON.parse(seeded.stdout.trim().split("\n").pop());

  const daemon = spawn(tares, ["up", "--port", String(PORT), "--data-dir", home],
                       { cwd: REPO, stdio: "ignore", env: cleanEnv({ ...base, TARES_AUTH_TOKEN: ROOT }), detached: true });
  console.log(`  daemon: pid ${daemon.pid}, port ${PORT}`);
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});

    const api = async (method, p, token, body) => {
      const r = await fetch(BASE + p, { method, headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
                                        body: body ? JSON.stringify(body) : undefined });
      return { status: r.status, body: await r.json().catch(() => ({})) };
    };
    const other = await api("POST", "/api/keys", ROOT, { name: "desktop", scopes: ["read"] });
    ck("a second key was made", other.status < 300 && !!other.body.secret, JSON.stringify(other.body));

    const context = async (width, { token = seed.key_secret, samples = false } = {}) => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 900 } });
      await ctx.addInitScript(([t, flag]) => {
        try {
          window.localStorage.setItem("tares_token", t);
          if (flag) window.localStorage.setItem("taresSamples", "1");
        } catch { /* blocked */ }
      }, [token, samples]);
      const page = await ctx.newPage();
      const calls = [];
      page.on("request", (req) => {
        if (new URL(req.url()).pathname === "/api/home/start/hide") calls.push(req.method());
      });
      return { ctx, page, calls };
    };
    const shot = async (page, name, width) => {
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `factory-start-show-${name}-${width}.png`), fullPage: true });
    };
    const liveOpen = async (page) => {
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
    };
    const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const list = (page) => page.getByTestId("start-checklist");
    const line = (page) => page.getByTestId("start-hidden");
    const showBtn = (page) => page.getByTestId("start-show");

    // the sample: the quiet line, at both widths
    for (const width of [1440, 390]) {
      const { ctx, page } = await context(width, { samples: true });
      await page.goto(`${BASE}/?sample=startHidden`, { waitUntil: "load" });
      await page.getByTestId("factory-home").waitFor();
      await page.waitForLoadState("networkidle");
      await shot(page, "sample-hidden", width);
      ck(`sample @${width}: the line is there, the list is not`, (await line(page).count()) === 1 && (await list(page).count()) === 0);
      ck(`sample @${width}: the line says what is hidden`, (await line(page).innerText()).includes(LINE), await line(page).innerText());
      ck(`sample @${width}: a button named Show it again`, (await page.getByRole("button", { name: "Show it again" }).count()) === 1);
      ck(`sample @${width}: no horizontal scroll`, (await overflow(page)) <= 0, `${await overflow(page)}px over`);
      await checkWords(page, ck, `sample hidden @${width}`);
      await ctx.close();
    }

    // live: shown, hide, line, show again
    {
      const { ctx, page, calls } = await context(1440);
      const oneRead = expectOneCall(page, "/api/home");
      await liveOpen(page);
      await oneRead(ck, "live shown");
      ck("live: the list is shown and there is no line", (await list(page).count()) === 1 && (await line(page).count()) === 0);

      await list(page).getByRole("button", { name: "Hide this list" }).click();
      await line(page).waitFor();
      ck("live: after Hide the list is gone and the line is in its place", (await list(page).count()) === 0 && (await line(page).count()) === 1);
      ck("live: the line has the words and the button", (await line(page).innerText()).includes(LINE) && (await showBtn(page).innerText()).trim() === "Show it again",
         await line(page).innerText());
      await shot(page, "live-hidden", 1440);
      await checkWords(page, ck, "live hidden");

      // a Show it again that fails keeps the line and says so
      await page.route("**/api/home/start/hide", (r) => r.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "down" }) }));
      await showBtn(page).click();
      await page.getByTestId("start-show-error").waitFor();
      const msg = (await page.getByTestId("start-show-error").innerText()).trim();
      ck("live: a refused Show it again keeps the line and says so in plain words",
         msg === "Could not show the list. Try again in a moment." && (await line(page).count()) === 1 && (await list(page).count()) === 0, msg);
      ck("live: the failure line has role alert", (await page.getByRole("alert").filter({ hasText: "Could not show the list" }).count()) === 1);
      ck("live: and the button is usable again", await showBtn(page).isEnabled());
      await checkWords(page, ck, "live show failed");
      await page.unroute("**/api/home/start/hide");

      // the line stays after a reload
      await liveOpen(page);
      ck("live: after a reload the line is still there", (await line(page).count()) === 1 && (await list(page).count()) === 0);

      // Show it again for real
      calls.length = 0;
      await showBtn(page).click();
      await list(page).waitFor();
      ck("live: Show it again brings the list back with its five steps", (await page.getByTestId("start-step").count()) === 5
         && (await page.getByTestId("start-count").innerText()).trim() === "2 of 5 done");
      ck("live: and the line is gone", (await line(page).count()) === 0);
      await page.waitForLoadState("networkidle");
      ck("live: it called the cell once, with DELETE", calls.join() === "DELETE", calls.join());
      ck("live: the cell says the list is shown", (await api("GET", "/api/home", seed.key_secret)).body.start.show === true);
      await shot(page, "live-shown-again", 1440);
      await checkWords(page, ck, "live shown again");
      await ctx.close();
    }

    // another key hides nothing for this one, and never sees the line
    {
      const mine = await api("POST", "/api/home/start/hide", seed.key_secret);
      ck("api: hiding answers hidden", mine.status === 200 && mine.body.hidden === true && mine.body.show === false, JSON.stringify(mine.body));
      const { ctx, page } = await context(1440, { token: other.body.secret });
      await liveOpen(page);
      ck("another key: sees the list and no line", (await list(page).count()) === 1 && (await line(page).count()) === 0);
      await ctx.close();
      const back = await api("DELETE", "/api/home/start/hide", seed.key_secret);
      ck("api: showing answers shown", back.status === 200 && back.body.hidden === false && back.body.show === true, JSON.stringify(back.body));
    }
  } finally {
    if (browser) await browser.close();
    try { process.kill(-daemon.pid); } catch { try { daemon.kill(); } catch { /* gone */ } }
    fs.rmSync(home, { recursive: true, force: true });
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
