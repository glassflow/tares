/*
 * Browser e2e for TR-904: the guards that keep one missing timestamp or a page without a clipboard from
 * breaking the console.
 *
 * - The words for times (incidentTime.ts) print nothing for a missing or bad date, never "NaN".
 * - A service page whose chart has no start time says so instead of throwing (one missing timestamp
 *   used to blank the whole console).
 * - An incident page with a bad "since" or "opened" time shows its status line without "NaN".
 * - A copy button on a page served over plain http, where navigator.clipboard is undefined, throws
 *   nothing and does not say "copied"; with a clipboard it copies and says "copied".
 *
 * Seeds the incidents cell (tests/seed-sre-incidents.py), then runs the real daemon on it, serving the
 * built console. Responses are rewritten in the browser to carry the missing and bad times.
 * Run:  npm run build && node tests/sre-guards.e2e.cjs     (from ui/, tares installed in ../.venv)
 * TARES_E2E_PORT sets the daemon port (default 8879); the next port is its MCP endpoint.
 * TARES_E2E_CHROME points Playwright at another Chrome binary when its own download is missing.
 */
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");

const UI_DIR = path.resolve(__dirname, "..");
const REPO = path.resolve(UI_DIR, "..");
const { chromium } = require(path.join(UI_DIR, "node_modules", "playwright"));
const ts = require(path.join(UI_DIR, "node_modules", "typescript"));
const { checkWords } = require("./words.cjs");

const PORT = Number(process.env.TARES_E2E_PORT || 8879);
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

// incidentTime.ts, compiled and run here: pure functions, no page needed
function loadTimeWords() {
  const src = fs.readFileSync(path.join(UI_DIR, "src", "editions", "sre", "incidentTime.ts"), "utf8");
  const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(js, { module: mod, exports: mod.exports });
  return mod.exports;
}

(async () => {
  // ── the words for times ──────────────────────────────────────────────────
  const w = loadTimeWords();
  const now = Date.parse("2026-10-30T15:00:00Z");
  for (const bad of [NaN, Date.parse(""), Date.parse("not a date"), Date.parse(undefined)]) {
    ck("sinceWords of a bad time is empty", w.sinceWords(bad, now) === "", w.sinceWords(bad, now));
    ck("dateWords of a bad time is empty", w.dateWords(bad) === "", w.dateWords(bad));
    ck("stamp of a bad time is the far words", w.stamp(bad, now, "far") === "far", w.stamp(bad, now, "far"));
    ck("clock of a bad time is empty", w.clock(bad) === "");
    const spans = [w.spanWords(now - bad), w.ageWords(now - bad), w.durationWords(now - bad)];
    ck("a span from a bad time says no NaN", spans.every((s) => !/NaN/.test(s)), spans.join(" | "));
  }
  ck("sinceWords still says today's time", w.sinceWords(Date.parse("2026-10-30T14:05:00Z"), now) === "14:05");
  ck("sinceWords still says yesterday", w.sinceWords(Date.parse("2026-10-29T14:05:00Z"), now) === "yesterday, 14:05");
  ck("sinceWords still says an older date", w.sinceWords(Date.parse("2026-10-27T14:05:00Z"), now) === "27 Oct, 14:05");
  ck("dateWords still says the date", w.dateWords(Date.parse("2026-10-27T14:05:00Z")) === "27 Oct, 14:05");

  // ── the console ──────────────────────────────────────────────────────────
  const bin = path.join(REPO, ".venv/bin");
  const python = fs.existsSync(path.join(bin, "python")) ? path.join(bin, "python") : "python3";
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tares-sre-guards-e2e-"));
  const dist = process.env.TARES_UI_DIST || path.join(UI_DIR, "dist");
  const ids = JSON.parse(execFileSync(python, [path.join(__dirname, "seed-sre-incidents.py"), home], { cwd: REPO, encoding: "utf8" })
    .trim().split("\n").pop());
  ck("seeded the incidents", !!ids["open-a"] && !!ids["onit-b"] && !!ids["res-c"]);

  const env = { ...process.env, TARES_EDITION: "sre", TARES_HOME: home, TARES_PORT: String(PORT), TARES_MCP_PORT: String(PORT + 1),
                TARES_UI_DIST: dist, TARES_OTLP_GRPC_PORT: "off", PYTHONPATH: REPO };
  for (const k of ["TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET", "TARES_SLACK_CONNECT_URL", "TARES_AUTH_TOKEN", "TARES_DB",
    "TARES_CATALOG", "TARESD_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL",
    "TARES_FACTORY_MCP_URL", "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE"]) delete env[k];
  const daemon = spawn(python, ["-c", `import sys; from tares.cli import main; sys.argv = ["tares", "up", "--port", "${PORT}", "--data-dir", ${JSON.stringify(home)}]; main()`],
    { cwd: REPO, stdio: "ignore", env });
  let browser;
  try {
    ck("daemon up", await waitFor(`${BASE}/health`));
    browser = await chromium.launch(process.env.TARES_E2E_CHROME ? { executablePath: process.env.TARES_E2E_CHROME } : {});
    const newPage = async (init) => {
      const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: "UTC", locale: "en-GB" });
      if (init) await ctx.addInitScript(init);
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e)));
      return { ctx, page, errors };
    };
    const body = (page) => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
    // answer a read with the real JSON after `edit` has changed it
    const rewrite = (page, re, edit) => page.route(re, async (route) => {
      const res = await route.fetch();
      const json = await res.json();
      edit(json);
      await route.fulfill({ response: res, json });
    });

    // a service page: the real chart, then one without its start time
    {
      const { ctx, page, errors } = await newPage();
      await page.goto(`${BASE}/services/edge-proxy`, { waitUntil: "load" });
      await page.getByTestId("service-chart").waitFor();
      ck("a service with a chart start time draws the chart", (await page.getByTestId("service-chart-unplaced").count()) === 0 && errors.length === 0, errors.join("; "));
      await ctx.close();
    }
    for (const [label, edit] of [["missing", (j) => { delete j.chart.since; }], ["bad", (j) => { j.chart.since = "not a date"; }]]) {
      const { ctx, page, errors } = await newPage();
      await rewrite(page, /\/api\/sre\/services\/edge-proxy(\?|$)/, (j) => edit(j));
      await page.goto(`${BASE}/services/edge-proxy`, { waitUntil: "load" });
      await page.getByTestId("service-title").waitFor();
      await page.getByTestId("service-chart-unplaced").waitFor();
      ck(`a service whose chart start time is ${label} still shows the page`, (await page.getByTestId("service-title").innerText()).trim() === "edge-proxy");
      ck(`${label} start time: nothing threw`, errors.length === 0, errors.join("; "));
      ck(`${label} start time: no NaN on the page`, !/NaN/.test(await body(page)), await body(page));
      await checkWords(page, ck, `${label} chart start time`);
      await ctx.close();
    }

    // an incident page: a bad "since" on the one who took it, a bad opened time, a bad resolved time
    for (const [label, key, edit] of [
      ["took it", "onit-b", (j) => { j.who.since = "not a date"; }],
      ["opened", "open-a", (j) => { delete j.opened_at; }],
      ["resolved", "res-c", (j) => { j.resolved.at = ""; }],
    ]) {
      const { ctx, page, errors } = await newPage();
      await rewrite(page, new RegExp(`/api/sre/incidents/${ids[key]}(\\?|$)`), (j) => edit(j.incident ?? j));
      await page.goto(`${BASE}/incidents/${ids[key]}`, { waitUntil: "load" });
      await page.getByTestId("incident-title").waitFor();
      ck(`an incident with a bad ${label} time shows its page`, (await page.getByTestId("status-line").count()) === 1);
      ck(`bad ${label} time: nothing threw`, errors.length === 0, errors.join("; "));
      ck(`bad ${label} time: no NaN on the page`, !/NaN/.test(await body(page)), await body(page));
      await ctx.close();
    }

    // copy buttons without a clipboard (a cell served over plain http has none), then with one
    const noClipboard = () => { Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true }); };
    const withClipboard = () => {
      window.__copied = [];
      Object.defineProperty(navigator, "clipboard", { value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } }, configurable: true });
    };
    const copyPages = [
      ["Claude Code source page", "/sources/claude-code", async (page) => { const b = page.locator("button.copybtn").first(); await b.waitFor(); return b; }],
      ["catalog export", "/sources/export", async (page) => {
        await page.getByRole("button", { name: /^Export \d+ source/ }).click();
        const b = page.getByRole("button", { name: /^(Copy|Copied)/ });
        await b.waitFor();
        return b;
      }],
    ];
    for (const [label, route, find] of copyPages) {
      {
        const { ctx, page, errors } = await newPage(noClipboard);
        await page.goto(`${BASE}${route}`, { waitUntil: "load" });
        const btn = await find(page);
        await btn.click();
        await page.waitForTimeout(300);
        ck(`${label}: no clipboard, nothing threw`, errors.length === 0, errors.join("; "));
        ck(`${label}: no clipboard, the button does not say copied`, !/^copied/i.test((await btn.innerText()).trim()), await btn.innerText());
        await ctx.close();
      }
      {
        const { ctx, page, errors } = await newPage(withClipboard);
        await page.goto(`${BASE}${route}`, { waitUntil: "load" });
        const btn = await find(page);
        await btn.click();
        await page.getByRole("button", { name: /^copied/i }).first().waitFor({ timeout: 3000 }).catch(() => {});
        ck(`${label}: with a clipboard it copies`, (await page.evaluate(() => window.__copied.length)) === 1);
        ck(`${label}: with a clipboard it says copied`, /^copied/i.test((await btn.innerText()).trim()), await btn.innerText());
        ck(`${label}: with a clipboard nothing threw`, errors.length === 0, errors.join("; "));
        await ctx.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    daemon.kill();
  }
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
